import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from bench import caption_text, player_name, result_caption, scoreboard_text, select_replay_rows
from models import resolve_llama_files


ENTRIES = {"gemma": {"name": "Gemma 4 12B IT"}, "flash": {"name": "Qwen 3.8 Flash Next"}}


def attempt(number, player="llama:gemma", case="gap4", cost=4716, breaks=0, status="passed"):
    return {"kind": "attempt", "attempt": number, "player": player, "case": case,
            "design": {"joints": [], "edges": []},
            "outcome": {"status": status, "cost": cost, "broken_members": breaks}}


class WatchTests(unittest.TestCase):
    def test_caption_uses_display_name_level_and_plain_text(self):
        name = player_name("llama:flash@high", ENTRIES)
        self.assertEqual(caption_text(name, "gap8", 3, 5, "thinking…", replay=True, repeat=2),
                         "Replay · Qwen 3.8 Flash Next@high · 8 m gap · try 3 of 5 · repeat 2\nthinking…\n")
        self.assertEqual(result_caption(attempt(1)["outcome"]), "PASSED $4,716, 0 breaks")
        self.assertEqual(result_caption(attempt(2, breaks=4, status="failed")["outcome"]), "FAILED, 4 breaks")
        self.assertEqual(result_caption({"status": "invalid_design"}), "INVALID DESIGN")

    def test_scoreboard_only_counts_current_players_clean_passes(self):
        rows = [attempt(1), attempt(2, cost=3900), attempt(3, cost=1200, breaks=1),
                attempt(4, cost=1000, status="failed"), attempt(1, player="llama:flash@high", cost=2000),
                {**attempt(2, case="gap8"), "kind": "review"}]
        self.assertEqual(scoreboard_text(ENTRIES["gemma"]["name"], "llama:gemma", ("gap4", "gap8"), rows),
                         "Gemma 4 12B IT\n4 m gap · 2 clean passes · best $3,900\n"
                         "8 m gap · 0 clean passes · no clean pass yet\n")

    def test_replay_selection_uses_final_design_and_preserves_repeats(self):
        first = attempt(1, case="gap8")
        second = attempt(1, case="gap8", breaks=4, status="failed")
        second["repeat"] = 2
        legacy = attempt(3)
        del legacy["kind"]
        rows = [{**first, "kind": "review"}, first, {**second, "design": None}, second,
                attempt(1, player="llama:flash@high", case="gap8"), legacy, attempt(0)]
        self.assertEqual(select_replay_rows(rows, ENTRIES, "gap8", 1, "gemma 4 12b it"), [first, second])
        self.assertEqual(select_replay_rows(rows, ENTRIES, attempt=3, player="llama:gemma"), [legacy])
        self.assertEqual(select_replay_rows(rows, ENTRIES, player="Qwen 3.8 Flash Next@high"), [rows[4]])
        self.assertEqual(select_replay_rows(rows, ENTRIES, "gap8", 3), [])

    def test_registry_overrides_and_missing_download_commands(self):
        model = {"repository": "test/model", "revision": "pinned", "filename": "split/w-00001-of-00002.gguf",
                 "projector_filename": "projector.gguf", "runtime_build": "build"}
        root = Path.cwd() / "test-model-files"
        registry = root / "models.json"
        hub = root / "hf" / "hub"
        projector = hub / "models--test--model" / "snapshots" / "pinned" / "projector.gguf"
        env_server, cli_server = root / "env.exe", root / "cli.exe"
        weights = root / "w-00001-of-00002.gguf"
        present = {root / "models.local.json", weights, projector, env_server, cli_server}
        overrides = json.dumps({"test": {"local_path": weights.name}})
        with patch.dict(os.environ, {"HF_HUB_CACHE": "", "HF_HOME": str(root / "hf"),
                                     "PB2_LLAMA_SERVER": str(env_server)}), patch.object(
                Path, "is_file", lambda path: path in present), patch.object(Path, "read_text", return_value=overrides):
            with self.assertRaisesRegex(FileNotFoundError, "hf download test/model split/w-00002-of-00002.gguf --revision pinned"):
                resolve_llama_files("test", model, registry)
            present.add(root / "w-00002-of-00002.gguf")
            resolved = resolve_llama_files("test", model, registry, cli_server)
            self.assertEqual(Path(resolved["weights"]), weights)
            self.assertEqual(Path(resolved["projector"]), projector)
            self.assertEqual(Path(resolved["executable"]), cli_server)
            self.assertEqual(Path(resolve_llama_files("test", model, registry)["executable"]), env_server)


if __name__ == "__main__":
    unittest.main()
