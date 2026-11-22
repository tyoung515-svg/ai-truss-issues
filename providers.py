"""Text and image transports for the benchmark."""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from urllib.error import HTTPError


OLLAMA_PORT = 11434
STRATA_PORT = 18089
STRATA_CONTEXT_SLACK = 8
CODEX_MODELS = {"gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"}
CLAUDE_MODELS = {"claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
# Replaces Claude Code's coding-agent prompt; the benchmark prompt itself is the user message, as for every player.
CLAUDE_SYSTEM_PROMPT = "You are a benchmark player. Follow the user's instructions."
# Owner context that must never reach a player: CLAUDE.md, auto-memory, skills, plugins, hooks, MCP, tools, saved sessions.
CLAUDE_ISOLATION = ["--safe-mode", "--disable-slash-commands", "--tools", "", "--strict-mcp-config",
                    "--setting-sources", "", "--no-session-persistence"]


class ProviderError(Exception):
    pass


def request(url, payload=None, *, timeout=12):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content = response.read()
            return json.loads(content) if content else None, response.headers
    except HTTPError as error:
        detail = error.read(2048).decode(errors="replace").strip()
        raise ProviderError(f"HTTP {error.code}: {detail}") from error
    except (OSError, ValueError) as error:
        raise ProviderError(str(error)) from error


def ollama_models(port=OLLAMA_PORT):
    result, _ = request(f"http://127.0.0.1:{port}/api/tags")
    return result["models"]


def ollama_complete(model, prompt, timeout, settings, images=(), seed=101, keep_alive=0, port=OLLAMA_PORT):
    url = f"http://127.0.0.1:{port}"
    message = {"role": "user", "content": prompt}
    if images:
        shown, _ = request(url + "/api/show", {"model": model})
        if "vision" not in shown.get("capabilities", []):
            raise ProviderError(f"{model} does not advertise vision; cannot send --blueprint")
        message["images"] = [base64.b64encode(path.read_bytes()).decode("ascii") for path in images]
    options = {**settings["sampling"], "seed": seed,
               "num_predict": settings["max_output_tokens"], "num_ctx": settings["context_size"]}
    result, _ = request(url + "/api/chat", {
        "model": model, "stream": False, "format": "json",
        "messages": [message], "options": options,
        "keep_alive": keep_alive, "think": settings["thinking"], "truncate": False,
    }, timeout=timeout)
    return result["message"]["content"], {
        **{field: result[field] for field in ("model", "prompt_eval_count", "eval_count", "load_duration", "done_reason") if field in result},
        "finish_reason": result.get("done_reason"), "settings_sent": options,
        "thinking_sent": settings["thinking"], "thinking_level_sent": None,
        "images": [str(p) for p in images]}


def llama_cpp_options(settings, seed=101):
    kwargs = {"enable_thinking": settings["thinking"], "conversational_mode": False}
    if settings["thinking"] and settings.get("thinking_level"):
        kwargs["reasoning_effort"] = settings["thinking_level"]
    return {
        **settings["sampling"], "seed": seed, "max_tokens": settings["max_output_tokens"],
        "reasoning_budget_tokens": settings.get("reasoning_budget", -1),
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": kwargs,
        "truncate": False,
    }


def llama_cpp_details(model, port, settings):
    result, _ = request(f"http://127.0.0.1:{port}/v1/models")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ProviderError("llama.cpp returned an invalid model catalog")
    for entry in result["data"]:
        if entry.get("id") == model:
            props, _ = request(f"http://127.0.0.1:{port}/props")
            return {"transport": "llama.cpp", "model": model, "port": port,
                    "model_metadata": entry.get("meta"), "server_props": props,
                    "options": llama_cpp_options(settings)}
    raise ProviderError(f"llama.cpp is not serving the requested model: {model}")


def png_data_url(path):
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def llama_cpp_payload(model, prompt, settings, images=(), seed=101):
    content = prompt
    if images:
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": png_data_url(path)}} for path in images]
    return {
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": content}],
        **llama_cpp_options(settings, seed),
    }


def llama_cpp_complete(model, prompt, timeout, port, settings, images=(), seed=101):
    payload = llama_cpp_payload(model, prompt, settings, images, seed)
    result, _ = request(f"http://127.0.0.1:{port}/v1/chat/completions", payload, timeout=timeout)
    try:
        if result["model"] != model:
            raise ProviderError(f"llama.cpp returned model {result['model']} for requested model {model}")
        choice = result["choices"][0]
        message = choice["message"]
        if message.get("tool_calls"):
            raise ProviderError("llama.cpp returned a tool call instead of a design")
        reply = message.get("content") or ""
        if not isinstance(reply, str):
            raise ProviderError("llama.cpp returned non-text content")
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise ProviderError("llama.cpp returned an invalid completion response") from error
    usage = result.get("usage") or {}
    reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    reasoning_token_source = "completion usage" if reasoning_tokens is not None else None
    if reasoning_tokens is None and message.get("reasoning_content"):
        try:
            counted, _ = request(f"http://127.0.0.1:{port}/tokenize",
                                 {"content": message["reasoning_content"], "add_special": False})
            reasoning_tokens = len(counted["tokens"])
            reasoning_token_source = "server tokenizer on returned reasoning_content"
        except (ProviderError, KeyError, TypeError):
            pass
    return reply, {"model": model, "finish_reason": choice.get("finish_reason"),
                   "usage": result.get("usage"), "timings": result.get("timings"),
                   "reasoning_tokens": reasoning_tokens, "reasoning_token_source": reasoning_token_source,
                   "thinking_level_sent": payload["chat_template_kwargs"].get("reasoning_effort"),
                   "truncated": result.get("truncated"), "settings_sent": llama_cpp_options(settings, seed),
                   "images": [str(p) for p in images]}


def strata_options(settings, seed=101):
    sampling = dict(settings["sampling"])
    if "repeat_penalty" in sampling:
        sampling["repetition_penalty"] = sampling.pop("repeat_penalty")
    effort = settings["thinking_level"] if settings["thinking"] else "none"
    return {**sampling, "seed": seed, "max_output_tokens": settings["max_output_tokens"],
            "reasoning_budget_tokens": settings["reasoning_budget"], "reasoning": {"effort": effort},
            "text": {"format": {"type": "json_object"}}, "stream": False, "store": False}


def strata_payload(model, prompt, settings, images, seed=101):
    content = [{"type": "input_text", "text": prompt}]
    content.extend({"type": "input_image", "image_url": png_data_url(path)} for path in images)
    return {"model": model, "input": [{"role": "user", "content": content}], **strata_options(settings, seed)}


def strata_details(model, port, settings):
    url = f"http://127.0.0.1:{port}"
    health, _ = request(url + "/health")
    props, _ = request(url + "/props")
    if health.get("service") != "strata" or not health.get("loaded") or props.get("model_alias") != model:
        raise ProviderError(f"Strata is not serving the requested model: {model}")
    catalog, _ = request(url + "/v1/models")
    entry = next((entry for entry in catalog.get("data", []) if entry.get("id") == model), None)
    if entry is None or entry.get("status", {}).get("value") != "loaded":
        raise ProviderError(f"Strata /v1/models does not list {model} as loaded")
    if not props.get("modalities", {}).get("vision") or "image" not in entry.get("architecture", {}).get("input_modalities", []):
        raise ProviderError("Strata did not load the vision projector")
    return {"transport": "Strata", "model": model, "port": port, "server_props": props,
            "model_metadata": entry, "options": strata_options(settings)}


def strata_count(model, prompt, settings, images, port, timeout):
    url = f"http://127.0.0.1:{port}"
    props, _ = request(url + "/props")
    n_ctx = props.get("default_generation_settings", {}).get("n_ctx")
    if type(n_ctx) is not int or n_ctx <= STRATA_CONTEXT_SLACK:
        raise ProviderError("Strata /props did not report a usable per-slot n_ctx")
    # Strict context validation happens after the template and vision token expansion, before inference.
    # A request for the entire slot as output always exceeds the limit by at least the engine's slack.
    payload = strata_payload(model, prompt, settings, images)
    try:
        request(url + "/v1/responses", {**payload, "max_output_tokens": n_ctx}, timeout=timeout)
    except ProviderError as error:
        message = str(error)
        if not message.startswith("HTTP 400: "):
            raise
        try:
            detail = json.loads(message.removeprefix("HTTP 400: "))["error"]
        except (KeyError, TypeError, ValueError):
            raise error from None
        if not isinstance(detail, dict) or detail.get("code") != "context_length_exceeded":
            raise
        count = re.search(r"prompt \((\d+) tokens\) (?:\+ max tokens \(\d+\) exceeds|leaves no room to answer in)"
                          r" the context \((\d+)\)", str(detail.get("message")))
        if count and int(count[2]) == n_ctx:
            return int(count[1]), n_ctx, {"context_rejection": message}
        raise
    raise ProviderError("Strata accepted an overflowing request. Start it with --no-fit-max-tokens.")


def strata_complete(model, prompt, timeout, port, settings, images=(), seed=101):
    options = strata_options(settings, seed)
    result, _ = request(f"http://127.0.0.1:{port}/v1/responses",
                        strata_payload(model, prompt, settings, images, seed), timeout=timeout)
    try:
        if result["model"] != model:
            raise ProviderError(f"Strata returned model {result['model']} for requested model {model}")
        if result["status"] == "completed":
            finish = "stop"
        elif result["status"] == "incomplete" and result["incomplete_details"]["reason"] == "max_output_tokens":
            finish = "length"
        else:
            raise ProviderError(f"Strata returned unexpected response status: {result['status']}")
        parts = []
        for item in result["output"]:
            if item["type"] == "reasoning":
                continue
            if item["type"] != "message" or item["role"] != "assistant":
                raise ProviderError(f"Strata returned an unexpected output item: {item['type']}")
            for part in item["content"]:
                if part["type"] != "output_text":
                    raise ProviderError(f"Strata returned non-text content: {part['type']}")
                parts.append(part["text"])
        usage = result["usage"]
        reasoning_tokens = usage["output_tokens_details"]["reasoning_tokens"]
        normalized_usage = {"prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"],
                            "total_tokens": usage["total_tokens"],
                            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens}}
        if type(reasoning_tokens) is not int or not 0 <= reasoning_tokens <= usage["output_tokens"]:
            raise ProviderError("Strata returned an invalid reasoning token count")
        reply = "".join(parts)
    except (KeyError, TypeError, AttributeError) as error:
        raise ProviderError("Strata returned an invalid Responses completion") from error
    return reply, {"model": model, "finish_reason": finish, "usage": normalized_usage, "strata_usage": usage,
                   "timings": result.get("timings"), "reasoning_tokens": reasoning_tokens,
                   "reasoning_token_source": "Strata Responses engine usage",
                   "thinking_level_sent": options["reasoning"]["effort"], "truncated": False,
                   "settings_sent": options, "images": [str(path) for path in images]}


def codex_details(model):
    if model not in CODEX_MODELS:
        raise ProviderError(f"Unsupported Codex benchmark model: {model}")
    exe = shutil.which("codex")
    if exe is None:
        raise ProviderError("Codex CLI is unavailable")
    return {"transport": "codex-cli", "requested_model": model}


def codex_complete(model, prompt, timeout, settings, images=(), seed=101):
    with tempfile.TemporaryDirectory(prefix="pb2-codex-") as folder:
        cmd = [
            shutil.which("codex"), "--ask-for-approval", "never", "exec", "--model", model,
            "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
            "--sandbox", "read-only", "--color", "never", "--cd", folder,
            "-c", "model_reasoning_effort=" + settings["reasoning_effort"],
        ]
        for path in images:
            cmd.extend(["-i", str(path.resolve())])
        cmd.append("-")
        try:
            result = subprocess.run(cmd, cwd=folder, input=prompt, encoding="utf-8",
                                    errors="replace", capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderError(f"Codex CLI: {error}") from error
    if result.returncode:
        raise ProviderError(f"Codex CLI exited {result.returncode}: {(result.stderr or result.stdout)[-1600:]}")
    reply = usage = thread_id = None
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError as error:
            raise ProviderError("Codex CLI wrote a non-JSON event") from error
        if event["type"] == "thread.started":
            thread_id = event.get("thread_id")
        if event["type"] == "turn.completed":
            usage = event.get("usage")
        if event["type"] in {"error", "turn.failed"}:
            raise ProviderError(str(event.get("message") or event.get("error") or event))
        if event["type"].startswith("item."):
            item = event["item"]
            if item["type"] not in {"agent_message", "reasoning"}:
                raise ProviderError(f"Codex CLI attempted an unexpected action: {item['type']}")
            if event["type"] == "item.completed" and item["type"] == "agent_message":
                reply = item["text"]
    if reply is None or usage is None:
        raise ProviderError("Codex CLI did not report a completed answer")
    return reply, {"requested_model": model, "thread_id": thread_id, "usage": usage,
                   "thinking_level_sent": settings["reasoning_effort"],
                   "settings_sent": {"model_reasoning_effort": settings["reasoning_effort"]},
                   "seed": None, "seed_note": "Codex CLI does not expose a sampling seed",
                   "images": [str(p) for p in images], "command": cmd,
                   "finish_reason": "turn.completed", "truncated": None}


def claude_details(model):
    if model not in CLAUDE_MODELS:
        raise ProviderError(f"Unsupported Claude benchmark model: {model}")
    if shutil.which("claude") is None:
        raise ProviderError("Claude Code CLI is unavailable")
    return {"transport": "claude-code-cli", "requested_model": model}


def claude_message(prompt, images=()):
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(path.read_bytes()).decode("ascii")}}
        for path in images]
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}})


def claude_env():
    env = {key: value for key, value in os.environ.items()
           if key not in {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                          "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"}}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def check_claude_init(event):
    leaks = {key: event.get(key) for key in ("tools", "mcp_servers", "slash_commands", "skills") if event.get(key)}
    if event.get("memory_paths"):
        leaks["memory_paths"] = event["memory_paths"]
    if leaks:
        raise ProviderError("Claude CLI session is not isolated: " + json.dumps(leaks)[:600])
    if event.get("apiKeySource") != "none":
        raise ProviderError(f"Claude CLI would bill an API key ({event.get('apiKeySource')}); only the subscription login is approved")


def claude_complete(model, prompt, timeout, settings, images=(), seed=101):
    effort = settings["reasoning_effort"]
    with tempfile.TemporaryDirectory(prefix="pb2-claude-") as folder:
        cmd = [shutil.which("claude"), "--print", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--model", model, "--effort", effort, "--system-prompt", CLAUDE_SYSTEM_PROMPT,
               *CLAUDE_ISOLATION]
        try:
            result = subprocess.run(cmd, cwd=folder, input=claude_message(prompt, images) + chr(10), encoding="utf-8",
                                    errors="replace", capture_output=True, timeout=timeout, check=False,
                                    env=claude_env())
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderError(f"Claude CLI: {error}") from error
    init = final = rate_limit = None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as error:
            raise ProviderError("Claude CLI wrote a non-JSON event") from error
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            check_claude_init(event)
            init = event
        elif kind == "assistant":
            for block in event["message"]["content"]:
                if block.get("type") not in {"text", "thinking", "redacted_thinking"}:
                    raise ProviderError(f"Claude CLI attempted an unexpected action: {block.get('type')}")
        elif kind == "user":
            raise ProviderError("Claude CLI returned a tool result")
        elif kind == "rate_limit_event":
            rate_limit = event
        elif kind == "result":
            final = event
    if result.returncode or final is None or final.get("is_error") or final.get("subtype") != "success":
        detail = json.dumps(final)[:1600] if final else (result.stderr or result.stdout)[-1600:]
        raise ProviderError(f"Claude CLI exited {result.returncode}: {detail}")
    if init is None:
        raise ProviderError("Claude CLI did not report its session settings; cannot confirm isolation")
    raw = final.get("usage") or {}
    cached = raw.get("cache_read_input_tokens") or 0
    usage = {"input_tokens": (raw.get("input_tokens") or 0) + (raw.get("cache_creation_input_tokens") or 0) + cached,
             "cached_input_tokens": cached, "output_tokens": raw.get("output_tokens"),
             "reasoning_output_tokens": (raw.get("output_tokens_details") or {}).get("thinking_tokens")}
    served = sorted((final.get("modelUsage") or {}).keys())
    return final.get("result") or "", {
        "requested_model": model, "served_models": served, "model_usage": final.get("modelUsage"),
        "session_id": final.get("session_id"),
        "usage": usage, "raw_usage": raw, "thinking_level_sent": effort,
        "settings_sent": {"effort": effort, "system_prompt": CLAUDE_SYSTEM_PROMPT},
        "isolation": {"flags": CLAUDE_ISOLATION, "auto_memory": "disabled", "api_key_source": init.get("apiKeySource"),
                      "cwd": init.get("cwd"), "plugins": [p.get("name") for p in init.get("plugins") or []],
                      "note": "Claude Code still adds its environment block and account note (~725 tokens)"},
        "cli_version": init.get("claude_code_version"), "rate_limit": rate_limit,
        "estimated_list_cost_usd": final.get("total_cost_usd"),
        "seed": None, "seed_note": "Claude Code CLI does not expose a sampling seed",
        "images": [str(p) for p in images], "command": cmd,
        "finish_reason": final.get("stop_reason"), "truncated": final.get("stop_reason") == "max_tokens"}
