"""Launch the installed Strata Windows bundle using a run-owned config."""

import json
import os
from pathlib import Path
import secrets
import subprocess

from models import resolve_gguf_files


def resolve_strata_files(name, settings, registry):
    model = resolve_gguf_files(name, settings, registry)
    version = model["runtime_build"]
    # The Strata installer puts the Terminal and engines under STRATA_DATA_DIR, default %LOCALAPPDATA%\Strata.
    install = Path(os.environ.get("STRATA_DATA_DIR") or Path(os.environ["LOCALAPPDATA"]) / "Strata")
    bundle = install / "terminal" / version
    component = install / "components" / "engine" / f"{version}-windows-x86_64-cuda-cuda13"
    data = Path(os.environ.get("PB2_STRATA_DATA") or Path.home() / "Strata-data").resolve()
    pack = data / "packs" / model["strata_pack"]
    mtp = data / "mtp" / "rt"
    tokenizer = pack / "tokenizer"
    files = {
        "executable": bundle / "python" / "python.exe",
        "engine": component / "strata.exe",
        "vision": component / "strata-vision.exe",
        "expert_profile": bundle / "data" / "expert-profile.bin",
    }
    required = [*files.values(), bundle / "serve" / "server.py", bundle / "VERSION", bundle / "COMMIT",
                component / "BUILD.json", pack / "dense.bin", pack / "native_experts.txt",
                mtp / "dense.bin", mtp / "experts.bin", mtp / "draft_vocab.bin",
                *(tokenizer / part for part in ("vocab.json", "merges.txt", "token_type.json", "chat_template.jinja"))]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing Strata runtime or prepared model files:\n" + "\n".join(missing))
    installed_version = (bundle / "VERSION").read_text(encoding="utf-8").strip()
    build = json.loads((component / "BUILD.json").read_text(encoding="utf-8"))
    if installed_version != version or build["version"] != version:
        raise ValueError(f"Strata bundle and CUDA engine must both be version {version}")
    return {**model, **{key: str(path) for key, path in files.items()}, "bundle": str(bundle),
            "pack": str(pack), "mtp": str(mtp), "tokenizer": str(tokenizer), "engine_build": build,
            "server_commit": (bundle / "COMMIT").read_text(encoding="utf-8").strip()}


def strata_launch_spec(model, key, port, context, config_path):
    config_path = config_path.resolve()
    config = {
        "exe": model["engine"],
        "cwd": str(config_path.parent),
        "model_name": key,
        "tokenizer": model["tokenizer"],
        "log": str(config_path.with_suffix(".engine.log")),
        "gpu": 0,
        "backend": "cuda",
        "drafting": "mtp",
        "args": [
            "--pack", model["pack"], "--native", model["weights"], "--expert-profile", model["expert_profile"],
            "--expert-cache", "auto", "--prefill", "auto", "--spec", "4", "--spec-min-p", "0.5",
            "--mtp", model["mtp"], "--max-context", str(context), "--kv", "q4_0", "--kv-resident", "32768",
            "--vram-reserve-mib", str(model["vram_reserve_mib"]), "--vision",
        ],
        "env": {"STRATA_HC_Q8": "1", "STRATA_ROUTE_TAIL_SKIP": "0"},
        "vision": {"exe": model["vision"], "mmproj": model["projector"], "model": model["weights"], "gpu": False,
                   "max_tokens": 512, "threads": 16},
        "fit_max_tokens": False,
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    command = [model["executable"], "-B", "-u", "-m", "serve.server", "--engine", "strata",
               "--config", str(config_path), "--host", "127.0.0.1", "--port", str(port), "--offline",
               "--no-fit-max-tokens", "--owner-fd", "0", "--shutdown-timeout", "5"]
    env = {**os.environ, "PYTHONPATH": model["bundle"], "STRATA_NO_PRESET": "1",
           "STRATA_OWNER_ATTEST_KEY": secrets.token_hex(32)}
    spawn = {"cwd": config_path.parent, "env": env,
             "stdin": subprocess.PIPE}
    return command, spawn


def request_strata_shutdown(process):
    """Closing the owner pipe tells Strata to drain and quit its contained children."""
    process.stdin.close()
    try:
        process.wait(timeout=35)
        return "owner pipe"
    except subprocess.TimeoutExpired:
        # The bundled Windows server keeps its engine and reader in kill-on-close jobs.
        process.kill()
        process.wait()
        return "forced"
