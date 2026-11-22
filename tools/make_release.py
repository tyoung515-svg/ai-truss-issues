"""Export and scan a tracked source snapshot for public release."""

import argparse
from io import BytesIO
from ipaddress import IPv4Address, IPv4Network
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
from uuid import uuid4
import zlib


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_FILES = {"pilot.md"}
EXCLUDED_DIRS = ("briefs/", "runs/", "model-cards/cache/")
EXCLUDED_SUFFIXES = (".local.json",)
ALLOWLIST = frozenset()  # Exact (relative path, check name, matched text) entries.
USER_PATH = re.compile(r"[a-z]:[\\/]+Users[\\/]+|/(?:Users|home)/", re.IGNORECASE)
EMAIL = re.compile(r"[\w.!#$%&'*+/=?^_`{|}~-]+@(?:[a-z0-9-]+\.)+[a-z]{2,63}\b", re.IGNORECASE)
IPV4 = re.compile(r"(?<![\d.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?!\d|\.[0-9])")
PRIVATE_NETWORKS = tuple(
    IPv4Network((IPv4Address(bytes((first, second, 0, 0))), prefix))
    for first, second, prefix in ((10, 0, 8), (172, 16, 12), (192, 168, 16), (100, 64, 10))
)  # RFC 1918 private addresses and RFC 6598 shared address space.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def excluded(name):
    name = name.casefold()
    return (name in EXCLUDED_FILES or name.startswith(EXCLUDED_DIRS) or name.endswith(EXCLUDED_SUFFIXES))


def private_ipv4(value):
    try:
        address = IPv4Address(value)
    except ValueError:
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


def private_patterns(repo):
    config = repo / "release.local.json"
    if not config.exists():
        return []
    data = json.loads(config.read_text(encoding="utf-8-sig"))
    patterns = data.get("private_patterns") if isinstance(data, dict) else None
    if not isinstance(patterns, list) or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
        raise ValueError("release.local.json must contain a private_patterns list of non-empty regex strings")
    return [re.compile(pattern) for pattern in patterns]


def scan_text(name, text, extra_patterns, location=""):
    checks = [("absolute user path", USER_PATH), ("email address", EMAIL), ("private IPv4", IPV4)]
    checks.extend((f"private pattern #{i}", pattern) for i, pattern in enumerate(extra_patterns, 1))
    for line_number, line in enumerate(text.splitlines(), 1):
        for label, pattern in checks:
            for match in pattern.finditer(line):
                value = match.group()
                if pattern is IPV4 and not private_ipv4(value):
                    continue
                if (name, label, value) not in ALLOWLIST:
                    yield f"{name}:{line_number}:{match.start() + 1}: {label}{location}"


def png_metadata(data):
    """Yield text chunks (including compressed ones) and other non-pixel metadata."""
    offset = len(PNG_SIGNATURE)
    index = 0
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        stop = offset + 12 + size
        if stop > len(data):
            break
        payload = data[offset + 8:stop - 4]
        offset = stop
        index += 1
        if kind == b"IEND":
            if offset < len(data):
                yield " (PNG trailing data)", data[offset:].decode("utf-8", errors="replace")
            return
        if kind in {b"IDAT", b"fdAT", b"PLTE"}:
            continue
        encoding = "latin-1" if kind in {b"tEXt", b"zTXt"} else "utf-8"
        if kind in {b"zTXt", b"iCCP"}:
            keyword, compressed = payload.split(b"\0", 1)
            payload = keyword + b"\0" + zlib.decompress(compressed[1:])
        elif kind == b"iTXt":
            keyword, info = payload.split(b"\0", 1)
            language, translation, value = info[2:].split(b"\0", 2)
            payload = keyword.decode("latin-1").encode() + b"\0" + language + b"\0" + translation + b"\0"
            payload += zlib.decompress(value) if info[0] else value
        yield f" (PNG {kind.decode('ascii', errors='replace')} chunk {index})", payload.decode(encoding, errors="replace")
    raise ValueError("incomplete PNG chunk data")


def jpeg_metadata(data):
    """Yield APPn and COM segments; the compressed image data after SOS is not text."""
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            raise ValueError("malformed JPEG segment marker")
        marker = data[offset + 1]
        if marker == 0xFF:  # fill byte
            offset += 1
            continue
        if marker in {0xD9, 0xDA}:  # EOI, SOS
            return
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # standalone markers
            offset += 2
            continue
        size = int.from_bytes(data[offset + 2:offset + 4], "big")
        if size < 2 or offset + 2 + size > len(data):
            raise ValueError("incomplete JPEG segment")
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:
            label = "COM" if marker == 0xFE else f"APP{marker - 0xE0}"
            yield f" (JPEG {label} segment)", data[offset + 4:offset + 2 + size].decode("utf-8", errors="replace")
        offset += 2 + size
    raise ValueError("JPEG ended before image data")


def scan_file(name, data, extra_patterns):
    yield from scan_text(name, name, extra_patterns, " (file name)")
    if data.startswith(PNG_SIGNATURE):
        for location, value in png_metadata(data):
            yield from scan_text(name, value, extra_patterns, location)
    elif data.startswith(b"\xff\xd8"):
        for location, value in jpeg_metadata(data):
            yield from scan_text(name, value, extra_patterns, location)
    else:
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        yield from scan_text(name, data.decode(encoding, errors="replace"), extra_patterns)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def export(repo, ref, folder):
    object_id = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{object}}")
    object_id = object_id.strip().decode("ascii")
    files = []
    with tarfile.open(fileobj=BytesIO(git(repo, "archive", "--format=tar", object_id))) as archive:
        for member in archive:
            if excluded(member.name):
                continue
            relative = PurePosixPath(member.name)
            path = folder.joinpath(*relative.parts)
            if (relative.is_absolute() or ".." in relative.parts or ":" in member.name or "\\" in member.name
                    or any(part.casefold() == ".git" for part in relative.parts)
                    or folder not in path.resolve().parents):
                raise ValueError(f"Unsafe archive path: {member.name}")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"Unsupported archive entry: {member.name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open("xb") as destination:
                shutil.copyfileobj(source, destination)
            files.append((relative.as_posix(), member.size))
    return sorted(files)


def make_release(repo, ref, out, force):
    if out.is_symlink():
        raise ValueError("Output must be a directory, not a symbolic link")
    out = out.resolve()
    if out == repo or out in repo.parents or any(part.casefold() == ".git" for part in out.parts):
        raise ValueError("Output would overwrite the source repository")
    if out.is_relative_to(repo):
        if git(repo, "--literal-pathspecs", "ls-files", "-z", "--", out.relative_to(repo).as_posix()):
            raise ValueError("Output would overwrite tracked source files")
    if out.exists() and (not out.is_dir() or (any(out.iterdir()) and not force)):
        raise ValueError("Output must be an empty directory or use --force to replace its contents")
    patterns = private_patterns(repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    scratch = out.parent / (".release-" + uuid4().hex)
    scratch.mkdir()
    stage, backup = scratch / "export", scratch / "previous"
    stage.mkdir()
    installed = False
    try:
        files = export(repo, ref, stage)
        hits = 0
        for name, _ in files:
            try:
                for hit in scan_file(name, (stage / name).read_bytes(), patterns):
                    print(hit)
                    hits += 1
            except (ValueError, IndexError, zlib.error) as error:
                print(f"{name}:1: cannot scan metadata: {error}")
                hits += 1
        print(f"Exported {len(files)} files, {sum(size for _, size in files):,} bytes")
        print("Largest files:")
        for name, size in sorted(files, key=lambda file: -file[1])[:5]:
            print(f"  {size:>10,} bytes  {name}")
        if hits:
            print(f"Scan failed: {hits} finding(s).")
            return 1
        if out.exists():
            if not force and any(out.iterdir()):
                raise ValueError("Output became non-empty during export")
            out.rename(backup)
        try:
            stage.rename(out)
        except OSError:
            if backup.exists():
                try:
                    backup.rename(out)
                except OSError as error:
                    raise OSError(f"Previous output retained at {backup}: {error}") from error
            raise
        installed = True
        print("Scan passed: no findings.")
        return 0
    finally:
        if installed or not backup.exists():
            shutil.rmtree(scratch)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog='Add local regex strings in release.local.json under the "private_patterns" key.',
    )
    parser.add_argument("--ref", default="HEAD", help="Git ref to archive (default: HEAD)")
    parser.add_argument("--out", type=Path, required=True, help="Release destination")
    parser.add_argument("--force", action="store_true", help="Replace an existing non-empty destination")
    args = parser.parse_args(argv)
    try:
        return make_release(ROOT, args.ref, args.out, args.force)
    except (OSError, ValueError, re.error, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"Release failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
