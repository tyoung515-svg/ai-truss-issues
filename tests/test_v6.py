import copy
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from bench import parse_design, play, player_details, prompt_for, summarize
from models import REGISTRY, model_settings, parse_player
from preflight import measure, preflight_math, review_prompt, worst_case_prompt
from providers import ProviderError, codex_complete, llama_cpp_complete, llama_cpp_options, ollama_complete
from scenes import render_scene
from telemetry import frame_peak, select_frames, trace_feedback
from tests import scratch_folder


FIXTURE = json.loads((Path(__file__).parent / "fixtures/reference-gap8.json").read_text(encoding="utf-8"))


class V6Tests(unittest.TestCase):
    def test_parse_player_levels_and_ollama_tag(self):
        self.assertEqual(parse_player("codex:gpt-6.2-astra@low"), ("codex", "gpt-6.2-astra", "low"))
        self.assertEqual(parse_player("ollama:qwen3:4b"), ("ollama", "qwen3:4b", None))
        for bad in ("llama:flash@", "llama:@low", "llama:flash@low@xhigh", "llama:gemma@low",
                    "codex:gpt-6.2-astra@high"):
            with self.subTest(player=bad), self.assertRaises(ValueError):
                model_settings(bad)

    def test_unknown_transport_reports_unknown_player(self):
        with self.assertRaisesRegex(ProviderError, "Unknown player fixture:gemma"):
            player_details("fixture:gemma", SimpleNamespace())

    def test_codex_effort_flag_and_player_identity(self):
        self.assertEqual(model_settings("codex:gpt-6.1-sol")["reasoning_effort"], "medium")
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
            {"type": "turn.completed", "usage": {"output_tokens": 1}},
        ]
        result = SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)))
        rows = []
        for level in ("low", "xhigh"):
            player = f"codex:gpt-6.2-astra@{level}"
            settings = model_settings(player)
            with patch("providers.tempfile.TemporaryDirectory", return_value=nullcontext("offline")), patch(
                    "providers.shutil.which", return_value="codex"), patch(
                    "providers.subprocess.run", return_value=result) as run:
                _, receipt = codex_complete(parse_player(player)[1], "prompt", 10, settings)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-c") + 1], f"model_reasoning_effort={level}")
            self.assertEqual(receipt["thinking_level_sent"], level)
            rows.append({"player": player, "case": "gap8", "attempt": 1, "outcome": FIXTURE["result"]})
        self.assertEqual([row["player"] for row in summarize(rows)], [row["player"] for row in rows])

    def test_qwen38_levels_map_to_chat_template(self):
        for suffix, sent in (("", "xhigh"), ("@low", "low"), ("@medium", "medium"),
                             ("@high", "xhigh"), ("@xhigh", "xhigh")):
            with self.subTest(suffix=suffix):
                settings = model_settings(f"llama:qwen27{suffix}")
                kwargs = llama_cpp_options(settings)["chat_template_kwargs"]
                self.assertTrue(kwargs["enable_thinking"])
                self.assertEqual(kwargs["reasoning_effort"], sent)
                self.assertNotIn("reasoning_effort", llama_cpp_options(
                    {**settings, "thinking": False})["chat_template_kwargs"])

    def test_local_output_budgets(self):
        entries = json.loads(REGISTRY.read_text(encoding="utf-8"))
        for name, entry in entries.items():
            transport = entry.get("transport", "llama")
            if transport in {"llama", "ollama", "strata"}:
                with self.subTest(model=name):
                    settings = model_settings(f"{transport}:{name}")
                    self.assertEqual(settings["max_output_tokens"], 32768)
                    self.assertEqual(settings.get("reasoning_budget"), 16384 if transport != "ollama" else None)
        self.assertEqual(model_settings("ollama:qwen3:4b")["context_size"], 131072)

    def test_prompt_and_review_preserve_evidence(self):
        obs, design = FIXTURE["observation"], FIXTURE["design"]
        prompt = prompt_for(obs, [], 2)
        reply = json.dumps(design)
        review = review_prompt(prompt, reply, {"status": "valid", "cost": 5276})
        self.assertTrue(review.startswith(prompt))
        self.assertIn(json.dumps(reply), review)
        self.assertIn('{"confirm":true}', review)
        self.assertIn('"cost":5276', review)
        self.assertEqual(parse_design(reply), design)
        with self.assertRaises(ValueError):
            parse_design('{"confirm":true}')
        worst = worst_case_prompt(obs, 2, prompt_for)
        self.assertIn("Attempt 2 of 2", worst)
        self.assertIn("overlaps another joint", worst)

    def test_recorded_feedback(self):
        lines = trace_feedback(FIXTURE["trace"], FIXTURE["result"])
        self.assertTrue(lines)
        self.assertTrue(all("peak " in line and "car x=" in line for line in lines))
        self.assertTrue(any("44.41%" in line for line in lines))

    def test_frame_selection_covers_span_and_peak(self):
        frames = select_frames(FIXTURE["trace"], FIXTURE["observation"])
        self.assertLessEqual(len(frames), 7)
        self.assertTrue(all(b["time_s"] - a["time_s"] >= .2 for a, b in zip(frames, frames[1:])))
        peak = max(FIXTURE["trace"]["samples"], key=frame_peak)
        self.assertIn(peak, frames)
        self.assertLess(abs(frames[0]["car"][0]), .3)
        self.assertLess(abs(frames[-1]["car"][0] - 8), .3)

    def test_first_break_is_preserved_when_near_peak(self):
        trace = copy.deepcopy(FIXTURE["trace"])
        trace["samples"][20]["members"][0]["broken"] = True
        frames = select_frames(trace, FIXTURE["observation"])
        self.assertIn(trace["samples"][20], frames)

    def test_preflight_exact_boundary_and_overflow(self):
        self.assertTrue(preflight_math(30000, 2768, 32768)["fits"])
        self.assertFalse(preflight_math(30001, 2768, 32768)["fits"])
        with self.assertRaises(ValueError):
            preflight_math(-1, 1, 32768)

    def test_preflight_uses_live_context_instead_of_registry(self):
        settings = model_settings("llama:qwen27@low")
        with patch("preflight.request", side_effect=[
            ({"default_generation_settings": {"n_ctx": 32768}}, {}),
            ({"input_tokens": 100}, {}),
        ]) as req:
            result = measure("llama:qwen27@low", "prompt", [], settings, 18088, 10)
        self.assertFalse(result["fits"])
        self.assertEqual(result["n_ctx"], 32768)
        self.assertEqual(result["required_tokens"], 32868)
        self.assertEqual(req.call_args.args[1]["chat_template_kwargs"]["reasoning_effort"], "low")

    def test_preflight_dry_fallback_keeps_thinking_reserve(self):
        settings = model_settings("llama:qwen27")
        with patch("preflight.request", side_effect=[
            ({"default_generation_settings": {"n_ctx": 32768}}, {}),
            ProviderError("HTTP 404: Not Found"),
        ]), patch(
                "preflight.llama_cpp_complete", return_value=("", {"usage": {"prompt_tokens": 100}})):
            result = measure("llama:qwen27", "prompt", [], settings, 18088, 10)
        self.assertFalse(result["fits"])
        self.assertEqual(result["required_tokens"], 32932)

    def test_ollama_overflow_preserves_reported_counts(self):
        error = ProviderError("HTTP 400: request (81037 tokens) exceeds the available context size (32768 tokens)")
        with patch("preflight.ollama_complete", side_effect=error) as complete:
            result = measure("ollama:qwen3:4b", "prompt", [], model_settings("ollama:qwen3:4b"), 18088, 10)
        self.assertTrue(complete.call_args.args[3]["thinking"])
        self.assertFalse(result["fits"])
        self.assertEqual(result["required_tokens"], 113805)
        self.assertEqual(result["n_ctx"], 32768)

    def test_review_rows_do_not_count_as_attempts(self):
        rows = [{"kind": "review", "player": "x", "case": "gap8", "attempt": 1,
                 "outcome": {"status": "confirmed"}}]
        rows += [{"kind": "attempt", "repeat": i, "player": "x", "case": "gap8", "attempt": 1,
                  "outcome": FIXTURE["result"]} for i in (1, 2)]
        summary = summarize(rows)[0]
        self.assertEqual(summary["attempts"], 2)
        self.assertEqual(summary["clean_passes"], 2)
        self.assertEqual(summary["repeats"], [1, 2])

    def test_bad_review_simulates_original_and_records_error(self):
        design = FIXTURE["design"]
        result = {k: v for k, v in FIXTURE["result"].items() if k != "trace_file"}
        calls, rows = [], []

        class Host:
            def call(self, action, **payload):
                calls.append((action, payload))
                return {"status": "valid", "cost": 5276} if action == "validate" else result

        args = SimpleNamespace(attempts=1, blueprint=False, seed=101)
        with patch("bench.complete", side_effect=[(json.dumps(design), {}), ("not json", {})]), patch("bench.render_scene"):
            play(Host(), "llama:test", FIXTURE["observation"], args, rows.append,
                 Path(__file__).resolve().parents[1] / "runs")
        self.assertEqual([call[0] for call in calls], ["validate", "simulate"])
        self.assertEqual(calls[1][1]["design"], design)
        self.assertEqual(rows[0]["kind"], "review")
        self.assertEqual(rows[0]["outcome"]["status"], "error")
        self.assertTrue(rows[1]["review_error"])

    def test_multiple_images_reach_llama_and_ollama(self):
        settings = {"sampling": {"temperature": 1}, "context_size": 32768,
                    "thinking": True, "max_output_tokens": 8192}
        with scratch_folder() as folder:
            path = folder / "test-proposed.png"
            render_scene(path, FIXTURE["observation"], "Proposed design", FIXTURE["design"])
            response = {"model": "test", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
            with patch("providers.request", return_value=(response, {})) as req:
                _, receipt = llama_cpp_complete("test", "prompt", 10, 18088, settings, [path, path], 102)
                payload = req.call_args.args[1]
                self.assertEqual(len(payload["messages"][0]["content"]), 3)
                self.assertEqual(receipt["settings_sent"]["seed"], 102)
            response = {"message": {"content": "{}"}, "done_reason": "stop"}
            with patch("providers.request", side_effect=[({"capabilities": ["vision"]}, {}), (response, {})]) as req:
                ollama_complete("test", "prompt", 10, settings, [path, path], 103)
                payload = req.call_args.args[1]
                self.assertEqual(len(payload["messages"][0]["images"]), 2)
                self.assertEqual(payload["options"]["num_ctx"], 32768)

    def test_gemma26_receipt_keeps_thinking_settings_and_draft_counters(self):
        settings = model_settings("llama:gemma26")
        timings = {"predicted_per_second": 102.8, "draft_n": 100, "draft_n_accepted": 71}
        response = {"model": "gemma26", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "timings": timings}
        with patch("providers.request", return_value=(response, {})):
            _, receipt = llama_cpp_complete("gemma26", "prompt", 10, 18088, settings)
        self.assertTrue(receipt["settings_sent"]["chat_template_kwargs"]["enable_thinking"])
        self.assertIsNone(receipt["thinking_level_sent"])
        self.assertEqual(receipt["timings"], timings)
        [summary] = summarize([{"player": "llama:gemma26", "summary_label": settings["summary_label"],
                                "case": "gap8", "attempt": 1, "outcome": FIXTURE["result"]}])
        self.assertEqual(summary["player"], "llama:gemma26")
        self.assertIn("MTP, llama.cpp b12590", summary["summary_label"])


if __name__ == "__main__":
    unittest.main()
