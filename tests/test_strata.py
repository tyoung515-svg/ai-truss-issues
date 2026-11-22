import unittest
from unittest.mock import patch

from models import model_settings
from preflight import measure
from providers import ProviderError, strata_complete, strata_options
from tests import scratch_folder


class StrataTests(unittest.TestCase):
    def test_flash_sends_supported_levels_budget_and_card_sampling(self):
        for level in ("low", "high"):
            options = strata_options(model_settings(f"strata:flash-strata@{level}"), seed=123)
            self.assertEqual(options["reasoning"], {"effort": level})
            self.assertEqual(options["max_output_tokens"], 32768)
            self.assertEqual(options["reasoning_budget_tokens"], 16384)
            self.assertEqual(options["seed"], 123)
            self.assertEqual((options["temperature"], options["top_p"], options["top_k"]), (1, .95, 20))
        with self.assertRaises(ValueError):
            model_settings("strata:flash-strata@xhigh")

    def test_preflight_counts_expanded_images_and_leaves_engine_slack(self):
        settings = model_settings("strata:flash-strata@low")
        props = {"n_ctx": None, "default_generation_settings": {"n_ctx": 131072}}
        with scratch_folder() as folder:
            image = folder / "preflight.png"
            image.write_bytes(b"test-image")
            for prompt_tokens, fits in ((98296, True), (98297, False)):
                refusal = ProviderError(
                    'HTTP 400: {"error":{"code":"context_length_exceeded","message":"request exceeds the context '
                    f'window: prompt ({prompt_tokens} tokens) + max tokens (131072) exceeds the context (131072)."}}}}'
                )
                with patch("providers.request", side_effect=[(props, {}), refusal]) as request:
                    receipt = measure("strata:flash-strata@low", "prompt", [image], settings, 18089, 10)
                url, payload = request.call_args.args
                self.assertTrue(url.endswith("/v1/responses"))
                self.assertEqual(payload["reasoning"]["effort"], "low")
                self.assertEqual(payload["input"][0]["content"][1]["image_url"], "data:image/png;base64,dGVzdC1pbWFnZQ==")
                self.assertEqual(payload["max_output_tokens"], 131072)
                self.assertEqual((receipt["prompt_tokens"], receipt["n_ctx"], receipt["context_slack_tokens"]),
                                 (prompt_tokens, 131072, 8))
                self.assertEqual(receipt["fits"], fits)

    def test_preflight_propagates_an_image_failure(self):
        settings = model_settings("strata:flash-strata@low")
        props = {"default_generation_settings": {"n_ctx": 131072}}
        error = ProviderError("HTTP 400: this server was started without an image encoder")
        with patch("providers.request", side_effect=[(props, {}), error]), self.assertRaisesRegex(
                ProviderError, "without an image encoder"):
            measure("strata:flash-strata@low", "prompt", [], settings, 18089, 10)

    def test_completion_uses_engine_reasoning_count_and_normalizes_length(self):
        settings = model_settings("strata:flash-strata@high")
        response = {"model": "flash", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "reasoning"}, {"type": "message", "role": "assistant",
                                                      "content": [{"type": "output_text", "text": "{}"}]}],
                    "usage": {"input_tokens": 370, "output_tokens": 200, "total_tokens": 570,
                              "input_tokens_details": {"cached_tokens": 2},
                              "output_tokens_details": {"reasoning_tokens": 137}},
                    "timings": {"predicted_per_second": 94, "draft_n": 150, "draft_n_accepted": 120}}
        with patch("providers.request", return_value=(response, {})) as request:
            text, receipt = strata_complete("flash", "prompt", 10, 18089, settings, seed=42)
        self.assertEqual(text, "{}")
        self.assertEqual(receipt["reasoning_tokens"], 137)
        self.assertEqual(receipt["timings"]["draft_n_accepted"], 120)
        self.assertEqual(receipt["finish_reason"], "length")
        self.assertEqual(receipt["thinking_level_sent"], "high")
        self.assertEqual(receipt["usage"]["prompt_tokens"], 370)
        self.assertEqual(request.call_args.args[1]["seed"], 42)
