from contextlib import redirect_stdout
from io import BytesIO, StringIO
import json
import re
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from tests import scratch_folder
from tools import make_release as release


def dotted(*octets):
    return ".".join(map(str, octets))


class MakeReleaseTests(unittest.TestCase):
    def test_scanner_reports_every_hit_with_line_and_column(self):
        paths = ["Z:" + "\\Users\\" + "Fixture", "/" + "Users" + "/Example", "/" + "home" + "/Example"]
        addresses = [dotted(10, 5, 0, 3), dotted(172, 31, 4, 9), dotted(192, 168, 5, 2),
                     dotted(100, 64, 0, 2), dotted(100, 127, 255, 254)]
        email = "fixture" + "@" + "example.invalid"
        public = [dotted(127, 0, 0, 1), dotted(172, 32, 0, 9), dotted(100, 128, 2, 3),
                  dotted(192, 168, 255, 999)]
        lines = [" ".join(paths), " ".join(addresses), email, "example-tag and example-tag", " ".join(public)]
        hits = list(release.scan_file("sample.txt", "\n".join(lines).encode("utf-16"), [re.compile("example-tag")]))
        self.assertCountEqual([hit.split(":")[1] for hit in hits], ["1"] * 3 + ["2"] * 5 + ["3"] + ["4"] * 2)
        self.assertIn("sample.txt:3:1: email address", hits)
        self.assertTrue(all(email not in hit for hit in hits))
        with patch.object(release, "ALLOWLIST", {("sample.txt", "email address", email)}):
            self.assertFalse(list(release.scan_file("sample.txt", email.encode(), [])))
            self.assertTrue(list(release.scan_file("other.txt", email.encode(), [])))

    def test_scanner_reads_png_text_and_compressed_metadata(self):
        info = PngImagePlugin.PngInfo()
        info.add_text("fixture plain", "first line\n" + dotted(10, 6, 0, 1))
        info.add_text("fixture Latin-1", "fixture marker", zip=True)
        info.add_itxt("fixture UTF-8", "fixture" + "@" + "example.invalid", zip=True)
        encoded = BytesIO()
        Image.new("RGB", (1, 1)).save(encoded, format="PNG", pnginfo=info)
        hits = list(release.scan_file("sample.png", encoded.getvalue(), [re.compile("fixture marker")]))
        self.assertEqual(len(hits), 3)
        self.assertTrue(hits[0].startswith("sample.png:2:1: private IPv4 (PNG tEXt"))
        self.assertIn("private pattern #1 (PNG zTXt", hits[1])
        self.assertIn("email address (PNG iTXt", hits[2])

    def test_scanner_reads_jpeg_metadata_but_not_image_data(self):
        encoded = BytesIO()
        Image.new("RGB", (8, 8)).save(encoded, format="JPEG", comment=b"fixture marker in comment")
        data = encoded.getvalue()
        hits = list(release.scan_file("sample.jpg", data, [re.compile("fixture marker")]))
        self.assertEqual(len(hits), 1)
        self.assertIn("private pattern #1 (JPEG COM segment)", hits[0])
        # The same bytes inside the compressed image data are noise, not text.
        end = data.rindex(b"\xff\xd9")
        noisy = data[:end] + b"fixture marker" + data[end:]
        self.assertEqual(len(list(release.scan_file("sample.jpg", noisy, [re.compile("fixture marker")]))), 1)

    def test_export_uses_git_tree_and_explicit_exclusions(self):
        with scratch_folder() as folder, redirect_stdout(StringIO()) as output:
            repo, destination = folder / "repo", folder / "public"
            repo.mkdir()
            release.git(repo, "init", "--quiet")
            fixture = "sample" + "@" + "example.invalid"
            for filename in ("PILOT.md", "briefs/a.txt", "runs/old/a.txt", "model-cards/cache/card.md",
                             "nested/model.local.json", "release.local.json"):
                path = repo / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(fixture, encoding="utf-8")
            source = repo / "public.txt"
            source.write_text("archived content", encoding="utf-8")
            (repo / "PILOT-notes.md").write_text("public notes", encoding="utf-8")
            release.git(repo, "add", "--all")
            tree = release.git(repo, "write-tree").strip().decode()
            source.write_text("uncommitted content", encoding="utf-8")
            (repo / "untracked.txt").write_text(fixture, encoding="utf-8")
            config = repo / "release.local.json"
            config.write_text(json.dumps({"private_patterns": ["example-regex-\\d+"]}), encoding="utf-8")

            self.assertEqual(release.make_release(repo, tree, destination, False), 0)
            self.assertIn("Scan passed: no findings.", output.getvalue())
            self.assertEqual(sorted(path.name for path in destination.iterdir()), ["PILOT-notes.md", "public.txt"])
            self.assertEqual((destination / "public.txt").read_text(encoding="utf-8"), "archived content")
            with self.assertRaisesRegex(ValueError, "empty directory"):
                release.make_release(repo, tree, destination, False)
            stale = destination / "stale.txt"
            stale.touch()

            source.write_text("example-regex-42", encoding="utf-8")
            release.git(repo, "add", "public.txt")
            dirty_tree = release.git(repo, "write-tree").strip().decode()
            self.assertEqual(release.make_release(repo, dirty_tree, destination, True), 1)
            self.assertIn("public.txt:1:1: private pattern #1", output.getvalue())
            self.assertTrue(stale.is_file())
            self.assertEqual(release.make_release(repo, tree, destination, True), 0)
            self.assertFalse(stale.exists())
