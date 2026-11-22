from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import patch

from bench import CASES, write_json
from models import REGISTRY, model_settings, parse_player, resolve_llama_files
from tests import scratch_folder
from tools import score


class FullRunTests(unittest.TestCase):
    def test_full_plan_expands_levels_and_covers_every_ready_registry_key(self):
        entries = json.loads(REGISTRY.read_text(encoding="utf-8"))
        plan = score.build_plan(REGISTRY, None, "v6-full", CASES, 5, 2)
        expected = {
            key: ["low", "xhigh"] if key in {"flash", "qwen27"} else [None]
            for key, entry in entries.items() if entry["status"] == "ready" and entry.get("transport", "llama") == "llama"
        }
        players = [("llama", key, level) for key, levels in expected.items() for level in levels]
        players += [("codex", "gpt-6.2-astra", "low"), ("codex", "gpt-6.2-astra", "xhigh")]
        self.assertEqual(players, [parse_player(row["player"]) for row in plan])
        self.assertTrue(all(row["estimated_turns"] == 40 for row in plan))
        gemma = next(row for row in plan if row["player"] == "llama:gemma")
        astra = next(row for row in plan if row["player"] == "codex:gpt-6.2-astra@xhigh")
        self.assertEqual((gemma["context"], gemma["output_cap"]), (131072, 32768))
        self.assertEqual((astra["level"], astra["output_cap"]), ("xhigh", None))

    def test_dry_plan_defaults_to_blueprints_for_full_preset(self):
        for args, expected in [(["--plan", "v6-full"], True),
                               (["--plan", "v6-full", "--no-blueprint"], False),
                               (["--player", "gemma"], False)]:
            with self.subTest(args=args), redirect_stdout(StringIO()) as output:
                self.assertEqual(score.full_main(["--dry-plan", *args]), 0)
                self.assertIn(f"blueprint {expected}", output.getvalue().splitlines()[0])

    def test_explicit_registry_keys_and_levels(self):
        plan = score.build_plan(REGISTRY, ["qwen27@low", "qwen3:4b", "codex:gpt-6.2-sol", "flash@low",
                                          "strata:flash-strata@high"], None,
                                ["gap4"], 6, 1)
        self.assertEqual([row["player"] for row in plan],
                         ["llama:qwen27@low", "ollama:qwen3:4b", "codex:gpt-6.2-sol",
                          "llama:flash@low", "strata:flash-strata@high"])
        self.assertTrue(all(row["estimated_turns"] == 12 for row in plan))
        with self.assertRaisesRegex(ValueError, "Duplicate player"):
            score.build_plan(REGISTRY, ["gemma", "llama:gemma"], None, CASES, 5, 1)

    def test_gemma26_launches_with_pinned_draft_and_projector(self):
        settings = model_settings("llama:gemma26")
        with scratch_folder() as folder, patch.dict("os.environ", {"HF_HUB_CACHE": str(folder)}):
            snapshot = folder / ("models--" + settings["repository"].replace("/", "--")) / "snapshots" / settings["revision"]
            snapshot.mkdir(parents=True)
            for field in ("filename", "projector_filename"):
                (snapshot / settings[field]).touch()
            executable = folder / "llama-server.exe"
            executable.touch()
            registry = folder / "models.json"
            draft = snapshot / settings["draft_filename"]
            with self.assertRaises(FileNotFoundError) as error:
                resolve_llama_files("gemma26", settings, registry, executable)
            self.assertIn(f"{draft.name} --revision {settings['revision']}", str(error.exception))
            draft.touch()
            resolved = resolve_llama_files("gemma26", settings, registry, executable)
            command = score.server_args(resolved, "gemma26", 18088, settings["context_size"])
            for flag, value in {"--model-draft": str(draft), "--mmproj": str(snapshot / settings["projector_filename"]),
                                "--spec-type": "draft-mtp", "--spec-draft-n-max": "4", "--n-cpu-moe": "12",
                                "-c": "131072", "--reasoning-budget": "16384"}.items():
                self.assertEqual(command[command.index(flag) + 1], value)

    def test_resume_skips_finished_group_and_restarts_partial_group(self):
        player = "codex:gpt-6.2-astra@low"
        config = {"registry": str(REGISTRY), "llama_server": None, "attempt_budget": 2, "cases": ["gap4"],
                  "repeats": 2, "model_timeout": 10, "flags": [], "plan": [{"player": player}]}

        def write_attempts(group, repeat, statuses, summary=True):
            group.mkdir(parents=True)
            rows = [{"kind": "attempt", "player": player, "case": "gap4", "repeat": repeat,
                     "attempt": attempt, "outcome": {"status": status, "cost": 1,
                                                      "broken_members": 0, "max_stress_pct": 1}}
                    for attempt, status in enumerate(statuses, 1)]
            (group / "attempts.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows),
                                                  encoding="utf-8")
            if summary:
                write_json(group / "summary.json", [])

        with scratch_folder() as folder:
            player_dir = folder / "player-01"
            partial = player_dir / "gap4-repeat-1"
            finished = player_dir / "gap4-repeat-2"
            write_attempts(partial, 1, ["invalid_design"], summary=False)
            write_attempts(finished, 2, ["passed", "invalid_design"])

            def run_bench(argv, *, run_dir, repeat_start, live_dir):
                self.assertEqual((run_dir, repeat_start, live_dir), (partial, 1, folder))
                self.assertFalse(run_dir.exists())
                write_attempts(run_dir, repeat_start, ["passed", "invalid_design"])
                return 0

            with patch.object(score, "bench_main", side_effect=run_bench) as bench:
                with patch.object(score.subprocess, "run") as graphic:
                    self.assertEqual(score.full_run(folder, config), 0)
                bench.assert_called_once()
                self.assertEqual(graphic.call_args.args[0][-1], str(folder / "comparison.png"))
            self.assertEqual(len(list(player_dir.glob("gap4-repeat-1-partial-*"))), 1)
            [result] = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual((result["repeats"], result["attempts"], result["first_pass_repeat"]), ([1, 2], 4, 1))
