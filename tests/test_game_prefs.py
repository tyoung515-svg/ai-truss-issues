import base64
import copy
import json
import os
import unittest
import uuid

from game_prefs import RegistryTree, protect_prefs, restore_tree
from tests import scratch_folder


def value(kind, data):
    return [kind, base64.b64encode(data).decode("ascii")]


class MemoryRegistry:
    key = r"Software\PB2BenchTest"

    def __init__(self, tree):
        self.tree = copy.deepcopy(tree)

    def snapshot(self):
        return copy.deepcopy(self.tree)

    def create_key(self, path):
        self.tree[path] = {}

    def delete_key(self, path):
        if any(child.startswith(path + "\\") for child in self.tree):
            raise OSError("Key still has subkeys")
        del self.tree[path]

    def delete_value(self, path, name):
        del self.tree[path][name]

    def set_value(self, path, name, data):
        self.tree[path][name] = data


class GamePrefsTests(unittest.TestCase):
    original = {"": {}, "Poly Bridge 2": {"": value(3, b"\xff\x00"), "wide": value(4, bytes(8))},
                "Empty": {}, "Removed": {"number": value(4, b"\x01\x00\x00\x00")}}

    def change(self, registry):
        registry.set_value("Poly Bridge 2", "wide", value(1, b"n\0\0\0"))
        registry.set_value("Poly Bridge 2", "new", value(0, b""))
        registry.delete_value("Poly Bridge 2", "")
        registry.delete_key("Removed")
        registry.create_key("Added")
        registry.create_key(r"Added\Nested")

    def test_restores_tree_after_interrupt(self):
        registry = MemoryRegistry(self.original)
        with scratch_folder() as folder:
            journal = folder / "prefs.json"
            with self.assertRaises(KeyboardInterrupt), protect_prefs(registry, journal):
                self.change(registry)
                raise KeyboardInterrupt()
            self.assertEqual(registry.snapshot(), self.original)
            self.assertFalse(journal.exists())

    def test_recovers_leftover_snapshot_before_next_launch(self):
        registry = MemoryRegistry(self.original)
        with scratch_folder() as folder:
            journal = folder / "prefs.json"
            journal.write_text(json.dumps({"key": registry.key, "tree": self.original}), encoding="utf-8")
            self.change(registry)
            with protect_prefs(registry, journal):
                self.assertEqual(registry.snapshot(), self.original)
                self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["tree"], self.original)
            self.assertFalse(journal.exists())

    def test_removes_new_registry_root_if_originally_missing(self):
        registry = MemoryRegistry({})
        with scratch_folder() as folder:
            with protect_prefs(registry, folder / "prefs.json"):
                registry.create_key("")
                registry.create_key("Child")
            self.assertEqual(registry.snapshot(), {})

    @unittest.skipUnless(os.name == "nt", "Windows registry required")
    def test_restores_temporary_windows_key_with_exact_types_and_bytes(self):
        registry = RegistryTree(r"Software\PB2BenchTest-" + uuid.uuid4().hex)
        try:
            registry.create_key("")
        except PermissionError as error:
            self.skipTest(f"Temporary HKCU key creation denied: {error}")
        try:
            for path, values in self.original.items():
                registry.create_key(path)
                for name, data in values.items():
                    registry.set_value(path, name, data)
            self.assertEqual(registry.snapshot(), self.original)
            with scratch_folder() as folder:
                journal = folder / "prefs.json"
                with protect_prefs(registry, journal):
                    self.change(registry)
                self.assertEqual(registry.snapshot(), self.original)
                self.assertFalse(journal.exists())
        finally:
            restore_tree(registry, {})
