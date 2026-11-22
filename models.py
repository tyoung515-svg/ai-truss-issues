"""Pinned local model settings. Missing card values stay unset, never guessed."""

import copy
import json
import os
from pathlib import Path
import re

REGISTRY = Path(__file__).with_name("models.json")


def resolve_gguf_files(name, entry, registry):
    local_file = Path(registry).with_name("models.local.json")
    local = json.loads(local_file.read_text(encoding="utf-8")).get(name, {}) if local_file.is_file() else {}
    hub = os.environ.get("HF_HUB_CACHE")
    if not hub:
        home = os.environ.get("HF_HOME")
        hub = Path(home) / "hub" if home else Path(os.environ.get("USERPROFILE") or Path.home()) / ".cache/huggingface/hub"
    repository, revision = entry["repository"], entry["revision"]
    snapshot = Path(hub) / ("models--" + repository.replace("/", "--")) / "snapshots" / revision
    resolved = dict(entry)
    missing = []
    for field, filename, override in (
        ("weights", entry["filename"], "local_path"),
        ("projector", entry.get("projector_filename"), "projector_local_path"),
        ("draft", entry.get("draft_filename"), "draft_local_path"),
    ):
        if not filename:
            continue
        path = Path(local[override]).expanduser() if local.get(override) else snapshot / filename
        if local.get(override) and not path.is_absolute():
            path = local_file.parent / path
        resolved[field] = str(path.absolute())
        files = [(path, filename)]
        split = re.fullmatch(r"(.*)-00001-of-(\d{5})\.gguf", filename)
        if split:
            for index in range(2, int(split[2]) + 1):
                shard = f"{split[1]}-{index:05d}-of-{split[2]}.gguf"
                files.append((path.with_name(Path(shard).name), shard))
        for candidate, download in files:
            if not candidate.is_file():
                missing.append(f"Missing {candidate}\n  hf download {repository} {download} --revision {revision}")
    if missing:
        raise FileNotFoundError("\n".join(missing))
    return resolved


def resolve_llama_files(name, entry, registry=REGISTRY, llama_server=None):
    resolved = resolve_gguf_files(name, entry, registry)
    executable = llama_server or os.environ.get("PB2_LLAMA_SERVER")
    if executable:
        executable = Path(executable).expanduser()
    else:
        build = Path(os.environ["LOCALAPPDATA"]) / "PolyBridgeBenchmark" / "tools" / entry["runtime_build"]
        executable = build / "llama-server.exe"
        if not executable.is_file() and entry.get("runtime_subdir"):
            executable = build / entry["runtime_subdir"] / executable.name
    if not executable.is_file():
        raise FileNotFoundError(f"Missing {executable}. Set --llama-server or PB2_LLAMA_SERVER for this build.")
    resolved["executable"] = str(executable.resolve())
    return resolved


def parse_player(player):
    transport, _, model = player.partition(":")
    name, suffix, level = model.partition("@")
    if suffix and (not name or not level or "@" in level):
        raise ValueError(f"Expected {transport}:MODEL@LEVEL, got {player}")
    return transport, name, level or None


def model_settings(player, registry=REGISTRY, effort=None):
    transport, name, level = parse_player(player)
    if player == "reference":
        return {}
    entries = json.loads(Path(registry).read_text(encoding="utf-8"))
    if name not in entries:
        raise ValueError(f"No settings for {player}; add its card evidence to {registry}")
    entry = copy.deepcopy(entries[name])
    if transport != entry.get("transport", "llama"):
        raise ValueError(f"{player}: registry entry uses a different transport")
    if entry.get("status") != "ready":
        raise ValueError(entry.get("blocked_reason", f"{name}: settings not ready"))
    if transport in {"codex", "claude"}:
        if level is not None and level not in {"low", "xhigh"}:
            raise ValueError(f"{player}: explicit {transport.title()} levels are low and xhigh")
        entry["reasoning_effort"] = level or effort or entry["reasoning_effort"]
    elif transport in {"llama", "strata"} and entry.get("thinking_levels"):
        if level is not None and level not in entry["thinking_levels"]:
            raise ValueError(f"{player}: choose from {', '.join(entry['thinking_levels'])}")
        if level is not None:
            entry["thinking_level"] = entry["thinking_levels"][level]
    elif level is not None:
        raise ValueError(f"{player}: this player does not accept a thinking level")
    return entry
