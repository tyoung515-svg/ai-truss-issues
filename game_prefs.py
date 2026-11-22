"""Journal the copied Unity player's shared Windows registry settings."""

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import json
import os


PREFS_KEY = r"Software\Dry Cactus"


class RegistryTree:
    def __init__(self, key):
        import winreg

        self.reg = winreg
        self.key = key
        native = ctypes.WinDLL("advapi32")
        data = ctypes.POINTER(ctypes.c_ubyte)
        size = ctypes.POINTER(w.DWORD)
        self.enum_value = native.RegEnumValueW
        self.enum_value.argtypes = [w.HKEY, w.DWORD, w.LPWSTR, size, size, size, data, size]
        self.query_value = native.RegQueryValueExW
        self.query_value.argtypes = [w.HKEY, w.LPCWSTR, size, size, data, size]
        self.write_value = native.RegSetValueExW
        self.write_value.argtypes = [w.HKEY, w.LPCWSTR, w.DWORD, w.DWORD, data, w.DWORD]

    def path(self, subkey):
        return self.key + ("\\" + subkey if subkey else "")

    def open(self, subkey, access):
        return self.reg.OpenKey(self.reg.HKEY_CURRENT_USER, self.path(subkey), 0, access)

    @staticmethod
    def check(result):
        if result:
            raise ctypes.WinError(result)

    def snapshot(self):
        tree = {}
        name = ctypes.create_unicode_buffer(16384)

        def visit(path, handle):
            subkeys, count, _ = self.reg.QueryInfoKey(handle)
            values = tree[path] = {}
            for index in range(count):
                length = w.DWORD(len(name))
                kind, size = w.DWORD(), w.DWORD()
                self.check(self.enum_value(int(handle), index, name, ctypes.byref(length), None,
                                           None, None, ctypes.byref(size)))
                # winreg decodes values such as DWORDs. Unity also stores oversized
                # DWORD payloads, so keep the exact bytes returned by the registry.
                while True:
                    raw = (ctypes.c_ubyte * max(1, size.value))()
                    result = self.query_value(int(handle), name, None, ctypes.byref(kind),
                                              raw, ctypes.byref(size))
                    if result != 234:  # ERROR_MORE_DATA
                        self.check(result)
                        break
                values[name.value] = [kind.value, base64.b64encode(bytes(raw)[:size.value]).decode("ascii")]
            for index in range(subkeys):
                child = self.reg.EnumKey(handle, index)
                relpath = path + "\\" + child if path else child
                with self.open(relpath, self.reg.KEY_READ) as nested:
                    visit(relpath, nested)

        try:
            root = self.open("", self.reg.KEY_READ)
        except FileNotFoundError:
            return tree
        with root:
            visit("", root)
        return tree

    def create_key(self, path):
        with self.reg.CreateKey(self.reg.HKEY_CURRENT_USER, self.path(path)):
            pass

    def delete_key(self, path):
        self.reg.DeleteKey(self.reg.HKEY_CURRENT_USER, self.path(path))

    def delete_value(self, path, name):
        with self.open(path, self.reg.KEY_SET_VALUE) as key:
            self.reg.DeleteValue(key, name)

    def set_value(self, path, name, value):
        kind, encoded = value
        raw = base64.b64decode(encoded, validate=True)
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        with self.open(path, self.reg.KEY_SET_VALUE) as key:
            self.check(self.write_value(int(key), name, 0, kind, buffer, len(raw)))


def restore_tree(registry, before):
    current = registry.snapshot()
    for path in sorted(current.keys() - before.keys(), reverse=True):
        registry.delete_key(path)
    for path, values in sorted(before.items()):
        if path not in current:
            registry.create_key(path)
        previous = current.get(path, {})
        for name in previous.keys() - values.keys():
            registry.delete_value(path, name)
        for name, value in values.items():
            if previous.get(name) != value:
                registry.set_value(path, name, value)
    if registry.snapshot() != before:
        raise OSError(f"Registry verification failed for HKCU\\{registry.key}")


@contextmanager
def protect_prefs(registry, journal, require_closed=lambda: None):
    def recover():
        require_closed()
        state = json.loads(journal.read_text(encoding="utf-8"))
        if state["key"] != registry.key:
            raise ValueError(f"Unexpected registry scope in {journal}")
        restore_tree(registry, state["tree"])
        journal.unlink()

    require_closed()
    if journal.exists():
        recover()
    temporary = journal.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump({"key": registry.key, "tree": registry.snapshot()}, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(journal)
    try:
        yield
    finally:
        recover()


def require_game_closed():
    """Avoid restoring while the Steam game or an orphaned copy can still write."""
    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
            ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
            ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
            ("exe", w.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel.CreateToolhelp32Snapshot
    snapshot.argtypes = [w.DWORD, w.DWORD]
    snapshot.restype = w.HANDLE
    handle = snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        first, next_process = kernel.Process32FirstW, kernel.Process32NextW
        for function in (first, next_process):
            function.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
        found = first(handle, ctypes.byref(entry))
        while found:
            if entry.exe.casefold() == "poly bridge 2.exe":
                raise OSError(f"Close Poly Bridge 2 (PID {entry.pid}) before using the benchmark")
            found = next_process(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle(handle)
