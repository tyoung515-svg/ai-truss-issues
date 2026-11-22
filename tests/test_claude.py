from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from models import model_settings, parse_player
from preflight import measure
from providers import CLAUDE_ISOLATION, ProviderError, claude_complete, claude_message


INIT = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "slash_commands": [], "skills": [],
        "memory_paths": None, "apiKeySource": "none", "cwd": "offline", "plugins": [{"name": "cc-plugin-telemetry"}],
        "claude_code_version": "2.1.330"}
ANSWER = {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "..."},
                                                       {"type": "text", "text": "{}"}]}}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "{}", "stop_reason": "end_turn",
          "session_id": "s", "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100,
                                       "cache_read_input_tokens": 40, "output_tokens": 30,
                                       "output_tokens_details": {"thinking_tokens": 20}},
          "modelUsage": {"claude-haiku-5-5": {"contextWindow": 1000000}}}


def run(events, returncode=0):
    return SimpleNamespace(returncode=returncode, stdout="\n".join(map(json.dumps, events)), stderr="")


class ClaudeTransportTests(unittest.TestCase):
    def call(self, player, events, images=()):
        settings = model_settings(player)
        with patch("providers.tempfile.TemporaryDirectory", return_value=nullcontext("offline")), patch(
                "providers.shutil.which", return_value="claude"), patch(
                "providers.subprocess.run", return_value=run(events)) as runner:
            reply, receipt = claude_complete(parse_player(player)[1], "prompt", 10, settings, images)
        return reply, receipt, runner.call_args

    def test_levels_isolation_flags_and_usage(self):
        for level in ("low", "xhigh"):
            with self.subTest(level=level):
                reply, receipt, call = self.call(f"claude:claude-haiku-5-5@{level}", [INIT, ANSWER, RESULT])
                command = call.args[0]
                self.assertEqual(reply, "{}")
                self.assertEqual(command[command.index("--effort") + 1], level)
                self.assertEqual(command[command.index("--model") + 1], "claude-haiku-5-5")
                for flag in ("--safe-mode", "--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence"):
                    self.assertIn(flag, command)
                self.assertEqual(command[command.index("--tools") + 1], "")
                self.assertEqual(command[command.index("--setting-sources") + 1], "")
                self.assertEqual(call.kwargs["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
                self.assertNotIn("ANTHROPIC_API_KEY", call.kwargs["env"])
                self.assertEqual(receipt["thinking_level_sent"], level)
                self.assertEqual(receipt["usage"], {"input_tokens": 145, "cached_input_tokens": 40,
                                                    "output_tokens": 30, "reasoning_output_tokens": 20})
                self.assertEqual(receipt["isolation"]["flags"], CLAUDE_ISOLATION)
        with self.assertRaises(ValueError):
            model_settings("claude:claude-haiku-5-5@high")

    def test_rejects_tools_leaks_and_api_keys(self):
        tool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}
        cases = {
            "tool use": [INIT, tool, RESULT],
            "tool result": [INIT, {"type": "user", "message": {"content": []}}, RESULT],
            "mcp": [{**INIT, "mcp_servers": [{"name": "owner-server"}]}, ANSWER, RESULT],
            "memory": [{**INIT, "memory_paths": {"auto": "x"}}, ANSWER, RESULT],
            "api key": [{**INIT, "apiKeySource": "ANTHROPIC_API_KEY"}, ANSWER, RESULT],
            "no init": [ANSWER, RESULT],
            "error result": [INIT, {**RESULT, "is_error": True, "subtype": "error_during_execution"}],
        }
        for name, events in cases.items():
            with self.subTest(case=name), self.assertRaises(ProviderError):
                self.call("claude:claude-haiku-5-5@low", events)

    def test_images_follow_text_and_preflight_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "a.png"
            image.write_bytes(b"png")
            message = json.loads(claude_message("prompt", [image]))
            content = message["message"]["content"]
            self.assertEqual([block["type"] for block in content], ["text", "image"])
            self.assertEqual(content[1]["source"]["media_type"], "image/png")
        result = measure("claude:claude-haiku-5-5@low", "prompt", [], {}, 0, 10)
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
