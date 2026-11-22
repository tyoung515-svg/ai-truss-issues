"""Measure the final review prompt with the transport's actual template and images."""

import json
import re

from models import parse_player
from providers import (STRATA_CONTEXT_SLACK, ProviderError, llama_cpp_complete, llama_cpp_payload,
                       ollama_complete, request, strata_count)
from scenes import render_scene


def worst_case_images(folder, obs):
    images = [folder / f"preflight-{obs['case']}-{i}.png" for i in range(9)]
    for path in images:
        render_scene(path, obs, "Preflight image at full frame resolution")
    return images


def review_prompt(prompt, reply, validation):
    return (prompt + "\n\nOne free placement review. No physics has run. Inspect the proposed-design image "
            "and the host validation. Reply with exactly {\"confirm\":true} or a complete revised "
            "design with joints and edges. Do not use tools.\n"
            "Your original reply (quoted JSON string): " + json.dumps(reply) + "\n"
            "Host placement validation: " + json.dumps(validation, separators=(",", ":")))


def worst_case_prompt(obs, attempts, builder):
    names = [f"J{i:023d}" for i in range(48)]
    design = {"joints": [[name, 1.123456789012345, 5.123456789012345] for name in names],
              "edges": [[names[i % 48], names[(i + 1) % 48], "steel"] for i in range(96)]}
    result = {"status": "failed", "cost": 15000, "broken_members": 96, "max_stress_pct": 100,
              "simulated_seconds": 99.999, "vehicle_position": [8.123456789, -8.123456789],
              "member_feedback": [f"member {i} steel {names[i % 48]}-{names[(i+1) % 48]}: "
                                  "peak 100% at t=99.999 s, car x=99.99; above 50% from car x=0.00 to 99.99; broken"
                                  for i in range(1, 97)]}
    prompt = builder(obs, [{"design": design, "result": result} for _ in range(attempts - 1)], attempts)
    error = ('Joint "J123" overlaps another joint. Edge L-J123 exceeds maximum material length. ' * 8)[:600]
    return review_prompt(prompt, json.dumps(design), {"status": "invalid_design", "error": error})


def preflight_math(prompt_tokens, max_output_tokens, n_ctx):
    if min(prompt_tokens, max_output_tokens, n_ctx) < 0:
        raise ValueError("Negative preflight token count")
    return {"prompt_tokens": prompt_tokens, "max_output_tokens": max_output_tokens,
            "required_tokens": prompt_tokens + max_output_tokens, "n_ctx": n_ctx,
            "fits": prompt_tokens + max_output_tokens <= n_ctx}


def measure(player, prompt, images, settings, port, timeout):
    transport, model, _ = parse_player(player)
    if transport in {"codex", "claude"}:
        return {"status": "unavailable", "reason": f"{transport.title()} CLI exposes no tokenizer or dry request; no extra paid calls",
                "images": [str(p) for p in images], "fits": None}
    reserve = 0
    context_slack = 0
    if transport == "strata":
        count, n_ctx, receipt = strata_count(model, prompt, settings, images, port, timeout)
        context_slack = STRATA_CONTEXT_SLACK
        method = "Strata Responses strict context check with actual template, thinking and image embeddings"
    elif transport == "llama":
        base_url = f"http://127.0.0.1:{port}"
        props, _ = request(base_url + "/props")
        n_ctx = props.get("default_generation_settings", {}).get("n_ctx")
        if not n_ctx:
            raise ProviderError("Server /props did not report per-slot n_ctx; cannot preflight")
        try:
            receipt, _ = request(base_url + "/v1/chat/completions/input_tokens",
                                 llama_cpp_payload(model, prompt, settings, images), timeout=timeout)
        except ProviderError as error:
            if not str(error).startswith("HTTP 404:"):
                raise
            dry = {**settings, "max_output_tokens": 1, "thinking": False}
            _, receipt = llama_cpp_complete(model, prompt, timeout, port, dry, images)
            count = (receipt.get("usage") or {}).get("prompt_tokens")
            reserve = 64 if settings.get("thinking") else 0
            method = "one-token dry chat with thinking disabled"
        else:
            count = receipt.get("input_tokens")
            method = "llama.cpp input_tokens with actual chat template, thinking and images"
    elif transport == "ollama":
        method = "Ollama one-token dry chat with actual thinking setting"
        try:
            _, receipt = ollama_complete(model, prompt, timeout, {**settings, "max_output_tokens": 1},
                                         images, keep_alive="1m", port=port)
        except ProviderError as error:
            overflow = re.search(r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)",
                                 str(error))
            if not overflow:
                raise
            count, n_ctx = map(int, overflow.groups())
            receipt = {"error": str(error)}
            method = "Ollama reported prompt and context counts in overflow response"
        else:
            count = receipt.get("prompt_eval_count")
            loaded, _ = request(f"http://127.0.0.1:{port}/api/ps")
            live = next((m for m in loaded.get("models", []) if m.get("name") == model or m.get("model") == model), {})
            n_ctx = live.get("context_length")
            if not n_ctx:
                raise ProviderError("Ollama /api/ps did not report context_length; cannot preflight")
    else:
        raise ProviderError(f"No preflight for {transport}")
    if count is None:
        raise ProviderError("Server did not report prompt usage including images")
    check = preflight_math(count + reserve, settings["max_output_tokens"], n_ctx - context_slack)
    if context_slack:
        check.update(n_ctx=n_ctx, context_slack_tokens=context_slack)
    return {"status": "measured", **check,
            "measured_prompt_tokens": count, "thinking_template_reserve": reserve,
            "method": method,
            "images": [str(p) for p in images], "receipt": receipt}
