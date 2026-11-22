"""Probe cached models or run a resumable comparison against the isolated game."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bench import (CASES, DEFAULT_ATTEMPTS, PROMPT_VERSION, HostError, add_watch_options,
                   game_lock, main as bench_main, prompt_for, start_game, summarize, write_json)
from models import model_settings, parse_player, resolve_llama_files
from preflight import measure, worst_case_images, worst_case_prompt
from providers import ProviderError, llama_cpp_complete, request, strata_complete
from scenes import render_scene
from strata_runtime import request_strata_shutdown, resolve_strata_files, strata_launch_spec


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def launch(command, **kwargs):
    return subprocess.Popen([str(a) for a in command], creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)


def server_args(model, key, port, context):
    command = [model["executable"], "-m", model["weights"], "--alias", key, "--host", "127.0.0.1",
               "--port", str(port), "-c", str(context), "-np", "1", "-b", "512", "-ub", "256",
               "-ctk", "q4_0", "-ctv", "q4_0", "-ngl", "all", "--device", "CUDA0", "-fa", "on",
               "--fit", "off", "--jinja", "--reasoning-budget", str(model["reasoning_budget"]),
               "--no-webui", "--offline", "--no-context-shift", "-lv", "4"]
    if model.get("projector"):
        command.extend(["--mmproj", model["projector"], "--image-max-tokens", "512"])
    if model.get("draft"):
        command.extend(["--model-draft", model["draft"]])
    command.extend(model["extra"])
    return command


def free_port():
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        return reserve.getsockname()[1]


def wait_server(server, url, timeout, ready):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"server exited {server.returncode}")
        try:
            response, _ = request(url, timeout=2)
            if ready(response) and server.poll() is None:
                return
        except ProviderError:
            pass
        time.sleep(.4)
    raise TimeoutError("server startup timed out")


def run(key, selection, model, args, folder):
    folder.mkdir(parents=True)
    player = "llama:" + selection
    receipt = {"player": player, "model": model, "trials": [], "status": "starting"}
    write_json(folder / "runtime.json", receipt)
    server = meter = None
    try:
        port = free_port()
        with start_game(folder / "game", args if args.watch else None) as (host, game):
            receipt["game"] = game
            obs = host.call("observe", case="gap4")
            for context in args.contexts:
                trial = {"context_size": context, "status": "starting"}
                receipt["trials"].append(trial)
                log = folder / f"llama-{context}.log"
                gpu = folder / f"gpu-{context}.csv"
                command = server_args(model, key, port, context)
                trial["command"] = command
                print(f"LOAD {selection} context={context} game_pid={game['pid']}", flush=True)
                try:
                    with log.open("wb") as output, gpu.open("wb") as gpu_output:
                        meter = launch(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits", "-lms", "250"], stdout=gpu_output, stderr=subprocess.DEVNULL)
                        server = launch(command, cwd=Path(model["executable"]).parent, stdout=output, stderr=subprocess.STDOUT)
                        trial["server_pid"] = server.pid
                        trial["meter_pid"] = meter.pid
                        wait_server(server, f"http://127.0.0.1:{port}/health",
                                    model.get("startup", 180), lambda health: health.get("status") == "ok")
                        props, _ = request(f"http://127.0.0.1:{port}/props")
                        trial["props"] = props
                        if model["status"] == "blocked":
                            trial["probe_blocked"] = model["blocked_reason"]
                        else:
                            if args.preflight:
                                checks = trial["preflight"] = []
                                for case in CASES:
                                    case_obs = obs if case == "gap4" else host.call("observe", case=case)
                                    images = worst_case_images(folder, case_obs)
                                    prompt = worst_case_prompt(case_obs, DEFAULT_ATTEMPTS, prompt_for)
                                    check = measure(player, prompt, images, model, port, args.model_timeout)
                                    checks.append({**check, "case": case, "image_bound": len(images)})
                                    print(f"PREFLIGHT {selection} {case} " + json.dumps({
                                        k: v for k, v in check.items() if k not in {"receipt", "images"}}), flush=True)
                                if not all(check["fits"] for check in checks):
                                    raise ValueError("Worst-case review prompt plus output exceeds per-slot n_ctx")
                            else:
                                image = folder / "probe.png"
                                render_scene(image, obs, "Empty gap")
                                probe_settings = {**model, "max_output_tokens": 512}
                                reply, details = llama_cpp_complete(key, 'Read the gap title; reply with {"gap_m":4}.',
                                                                   model.get("timeout", 300), port, probe_settings, [image])
                                trial["probe"] = {"reply": reply, "receipt": details}
                        trial["status"] = "fit"
                        receipt["context_size"] = context
                        receipt["status"] = "fit" if model["status"] != "blocked" else "card_blocked"
                        if args.smoke and model["status"] != "blocked":
                            # Keep server alive; release this host before bench starts its own host.
                            receipt["smoke_port"] = port
                except Exception as error:
                    trial.update(status="failed", error=str(error))
                    stop(server)
                    server = None
                finally:
                    stop(meter)
                    meter = None
                    if gpu.exists():
                        samples = [int(m[1]) for m in re.findall(r"(\d+)\s*,\s*(\d+)", gpu.read_text())]
                        trial["gpu_peak_mib"] = max(samples, default=None)
                    if log.exists():
                        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
                        trial["log_tail"] = lines[-24:]
                        trial["context_truncations"] = sum(map(int, re.findall(r"truncated = (\d+)", "\n".join(lines))))
                        trial["runtime_excerpt"] = [s for s in lines if re.search(
                            r"CUDA0.*(?:model|KV) buffer|n_ctx\s*=|offloaded .* layers", s)]
                    write_json(folder / "runtime.json", receipt)
                    print(f"RESULT {selection} context={context} {trial['status']} peak_mib={trial.get('gpu_peak_mib')} {trial.get('error', '')}", flush=True)
                if trial["status"] == "fit":
                    break
        if "context_size" not in receipt:
            receipt.update(status="blocked", error="No tested context completed the load/probe")
        if args.save_contexts and "context_size" in receipt:
            entries = json.loads(args.registry.read_text(encoding="utf-8"))
            entries[key]["context_size"] = receipt["context_size"]
            write_json(args.registry, entries)
        if args.smoke and receipt.get("smoke_port"):
            command = [sys.executable, "-u", str(ROOT / "bench.py"), "run", "--player", player,
                       "--registry", str(args.registry.resolve()), "--llama-port", str(port), "--case", args.case,
                       "--attempts", str(args.attempts), "--model-timeout", str(args.model_timeout)]
            if args.blueprint:
                command.append("--blueprint")
            if args.watch:
                width, height = args.watch_size
                command.extend(["--watch", "--watch-size", f"{width}x{height}", "--watch-hold", str(args.watch_hold)])
                if args.watch_mute:
                    command.append("--watch-mute")
            receipt["smoke_command"] = command
            previous = set((ROOT / "runs").iterdir())
            receipt["smoke_exit_code"] = subprocess.call(command, cwd=ROOT)
            receipt["smoke_runs"] = [str(p) for p in sorted(set((ROOT / "runs").iterdir()) - previous)
                                     if (p / "manifest.json").is_file()]
    except Exception as error:
        receipt.update(status="blocked", error=str(error))
        print(f"BLOCKED {key}: {error}", flush=True)
    finally:
        stop(server)
        stop(meter)
        write_json(folder / "runtime.json", receipt)
    return receipt


def registry_player(selection, entries):
    transport, _, _ = parse_player(selection)
    if transport in {"llama", "ollama", "codex", "claude", "strata"}:
        return selection
    key = selection.partition("@")[0]
    return entries.get(key, {}).get("transport", "llama") + ":" + selection


def build_plan(registry, selections, preset, cases, attempts, repeats):
    entries = json.loads(registry.read_text(encoding="utf-8"))
    if preset:
        selections = [f"llama:{key}{level}" for key, entry in entries.items()
                      if entry["status"] == "ready" and entry.get("transport", "llama") == "llama"
                      for level in (("@low", "@xhigh") if key in {"qwen27", "flash"} else ("",))]
        selections += ["codex:gpt-6.2-astra@low", "codex:gpt-6.2-astra@xhigh"]
    players = []
    for selection in selections or ():
        player = registry_player(selection, entries)
        if player in {entry["player"] for entry in players}:
            raise ValueError(f"Duplicate player {player}")
        model = model_settings(player, registry)
        players.append({"player": player,
                        "level": model.get("reasoning_effort") or model.get("thinking_level"),
                        "context": model["context_size"], "output_cap": model["max_output_tokens"],
                        "estimated_turns": len(cases) * repeats * attempts * 2})
    if not players:
        raise ValueError("Select at least one --player or --plan v6-full")
    return players


@contextmanager
def strata_server(player, model, port, context, folder):
    _, key, _ = parse_player(player)
    stem = folder / f"strata-{uuid.uuid4().hex[:8]}"
    config_path = stem.with_suffix(".json")
    server_log = stem.with_suffix(".server.log")
    receipt_path = stem.with_suffix(".runtime.json")
    command, spawn = strata_launch_spec(model, key, port, context, config_path)
    receipt = {"player": player, "model": model, "context_size": context, "port": port,
               "command": command, "config": str(config_path), "server_log": str(server_log),
               "engine_log": str(config_path.with_suffix(".engine.log"))}
    server = None
    with server_log.open("wb") as output:
        try:
            server = launch(command, stdout=output, stderr=subprocess.STDOUT, **spawn)
            receipt["server_pid"] = server.pid
            write_json(receipt_path, receipt)
            print(f"START {player} context={context} pid={server.pid} log={server_log}", flush=True)
            url = f"http://127.0.0.1:{port}"
            wait_server(server, url + "/health", model.get("startup", 180),
                        lambda health: health.get("service") == "strata" and health.get("loaded"))
            props, _ = request(url + "/props")
            if (props.get("model_alias") != key or props.get("default_generation_settings", {}).get("n_ctx") != context
                    or not props.get("modalities", {}).get("vision")):
                raise ProviderError("Strata /props did not confirm the requested model, context and vision")
            receipt["props"] = props
            yield receipt_path
        finally:
            if server is not None:
                receipt["shutdown"] = request_strata_shutdown(server)
                receipt["server_exit_code"] = server.returncode
                print(f"STOP {player} pid={server.pid} exit={server.returncode} via {receipt['shutdown']}", flush=True)
            write_json(receipt_path, receipt)


@contextmanager
def full_server(player, registry, executable, folder):
    transport, key, _ = parse_player(player)
    if transport in {"codex", "claude"}:
        yield []
        return
    model = model_settings(player, registry)
    port = free_port()
    if transport == "strata":
        model = resolve_strata_files(key, model, registry)
        with strata_server(player, model, port, model["context_size"], folder):
            yield ["--strata-port", str(port)]
        return
    url = f"http://127.0.0.1:{port}"
    spawn = {}
    if transport == "llama":
        model = resolve_llama_files(key, model, registry, executable)
        command = server_args(model, key, port, model["context_size"])
        spawn["cwd"] = Path(model["executable"]).parent
        health_path = "/health"
    else:
        ollama = shutil.which("ollama")
        if not ollama:
            raise FileNotFoundError("Ollama CLI is required for " + player)
        command = [ollama, "serve"]
        spawn["env"] = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{port}",
                        "OLLAMA_CONTEXT_LENGTH": str(model["context_size"]),
                        "OLLAMA_NO_CLOUD": "1", "OLLAMA_NOPRUNE": "1"}
        health_path = "/api/tags"
    server = None
    log = folder / f"server-{uuid.uuid4().hex[:8]}.log"
    with log.open("wb") as output:
        try:
            server = launch(command, stdout=output, stderr=subprocess.STDOUT, **spawn)
            print(f"START {player} context={model['context_size']} pid={server.pid} log={log}", flush=True)
            wait_server(server, url + health_path, model.get("startup", 180),
                        lambda response: transport == "ollama" or response.get("status") == "ok")
            yield [f"--{transport}-port", str(port)]
        finally:
            stop(server)
            if server is not None:
                print(f"STOP {player} pid={server.pid} exit={server.returncode}", flush=True)


def finished_group(folder, player, case, repeat, attempts):
    # bench publishes its summary after the host has exited and preferences are restored.
    if not (folder / "summary.json").is_file():
        return None
    try:
        rows = [json.loads(line) for line in (folder / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
    except (FileNotFoundError, ValueError):
        return None
    scored = [row for row in rows if row.get("kind") == "attempt"]
    if any((row["player"], row["case"], row.get("repeat")) != (player, case, repeat) for row in rows):
        return None
    if [row["attempt"] for row in scored] != list(range(1, attempts + 1)):
        return None
    if any(row["outcome"]["status"] in {"provider_error", "host_error"} for row in scored):
        return None
    return rows


def full_run(folder, config):
    registry = Path(config["registry"])
    executable = Path(config["llama_server"]) if config["llama_server"] else None
    attempts = config["attempt_budget"]
    rows = {}
    pending = {}

    def update_summary():
        write_json(folder / "summary.json", summarize(row for trials in rows.values() for row in trials))

    for index, entry in enumerate(config["plan"], 1):
        player = entry["player"]
        player_dir = folder / f"player-{index:02d}"
        for case in config["cases"]:
            for repeat in range(1, config["repeats"] + 1):
                group = player_dir / f"{case}-repeat-{repeat}"
                saved = finished_group(group, player, case, repeat, attempts)
                rows[group] = saved or []
                if saved is None:
                    pending.setdefault((player, player_dir), []).append((case, repeat, group))
                else:
                    print(f"SKIP {player} {case} repeat={repeat} (finished)", flush=True)
    update_summary()
    failed = False
    for (player, player_dir), groups in pending.items():
        player_dir.mkdir(exist_ok=True)
        try:
            with full_server(player, registry, executable, player_dir) as port_flags:
                for case, repeat, group in groups:
                    if group.exists():
                        group.rename(group.with_name(group.name + "-partial-" + uuid.uuid4().hex[:8]))
                        print(f"RESTART {player} {case} repeat={repeat}", flush=True)
                    try:
                        status = bench_main(
                            ["run", "--player", player, "--case", case, "--attempts", str(attempts),
                             "--registry", str(registry), "--model-timeout", str(config["model_timeout"]),
                             *config["flags"], *port_flags],
                            run_dir=group, repeat_start=repeat, live_dir=folder,
                        )
                        saved = finished_group(group, player, case, repeat, attempts)
                        if status or saved is None:
                            failed = True
                            print(f"INCOMPLETE {player} {case} repeat={repeat}", flush=True)
                        else:
                            rows[group] = saved
                            update_summary()
                    except (HostError, OSError, ValueError) as error:
                        failed = True
                        print(f"INCOMPLETE {player} {case} repeat={repeat}: {error}", flush=True)
        except (ProviderError, RuntimeError, OSError, ValueError) as error:
            failed = True
            print(f"INCOMPLETE {player}: {error}", flush=True)
    subprocess.run([sys.executable, str(ROOT / "tools" / "graphic.py"), str(folder),
                    "--output", str(folder / "comparison.png")], check=True)
    return int(failed)


def full_main(argv):
    parser = argparse.ArgumentParser(description="Run a resumable comparison with cached players")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--player", "--model", action="append", metavar="MODEL[@LEVEL]")
    selection.add_argument("--plan", choices=["v6-full"])
    selection.add_argument("--resume", type=Path, metavar="RUN_DIR")
    parser.add_argument("--dry-plan", action="store_true")
    parser.add_argument("--registry", type=Path, default=ROOT / "models.json")
    parser.add_argument("--llama-server", type=Path)
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS, choices=range(1, 7))
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--model-timeout", type=int, default=900)
    parser.add_argument("--blueprint", action=argparse.BooleanOptionalAction, default=None,
                        help="Attach images (enabled by default for v6-full)")
    parser.add_argument("--output", type=Path, default=ROOT / "runs")
    add_watch_options(parser)
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.model_timeout < 1:
        parser.error("--repeats and --model-timeout must be positive")
    defaults = parser.parse_args([])
    if args.resume and any(value != getattr(defaults, name)
                           for name, value in vars(args).items() if name not in {"resume", "dry_plan"}):
        parser.error("--resume uses the saved run settings")
    try:
        if args.resume:
            folder = args.resume.resolve()
            config = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            if config.get("full_run") != 1 or config["prompt_version"] != PROMPT_VERSION:
                raise ValueError("Expected a full-run directory for the current prompt version")
        else:
            cases = list(dict.fromkeys(args.case or CASES))
            blueprint = bool(args.plan) if args.blueprint is None else args.blueprint
            flags = ["--blueprint"] if blueprint else []
            if args.watch:
                width, height = args.watch_size
                flags += ["--watch", "--watch-size", f"{width}x{height}", "--watch-hold", str(args.watch_hold)]
                if args.watch_mute:
                    flags.append("--watch-mute")
            config = {"full_run": 1, "prompt_version": PROMPT_VERSION,
                      "attempt_budget": args.attempts, "blueprint": blueprint,
                      "cases": cases, "repeats": args.repeats, "model_timeout": args.model_timeout,
                      "registry": str(args.registry.resolve()),
                      "registry_sha256": hashlib.sha256(args.registry.read_bytes()).hexdigest(),
                      "llama_server": str(args.llama_server.resolve()) if args.llama_server else None,
                      "flags": flags,
                      "plan": build_plan(args.registry, args.player, args.plan, cases, args.attempts, args.repeats)}
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            folder = args.output.resolve() / (stamp + "-full-" + uuid.uuid4().hex[:8])
        if args.dry_plan:
            print(f"Cases {', '.join(config['cases'])} | attempts {config['attempt_budget']} | "
                  f"repeats {config['repeats']} | blueprint {config['blueprint']}")
            for index, entry in enumerate(config["plan"], 1):
                level = entry["level"] or "default"
                output = entry["output_cap"] if entry["output_cap"] is not None else "CLI default"
                print(f"{index:2}. {entry['player']} | level {level} | context {entry['context']} | "
                      f"output cap {output} | estimated turns {entry['estimated_turns']}")
            return 0
        if hashlib.sha256(Path(config["registry"]).read_bytes()).hexdigest() != config["registry_sha256"]:
            raise ValueError("The registry changed since planning; restore it before resuming")
        import PIL  # The comparison graphic always needs Pillow.

        if not args.resume:
            folder.mkdir(parents=True)
            write_json(folder / "manifest.json", config)
        print(f"Full run directory {folder}", flush=True)
        with game_lock(folder / "run.lock", "Another launcher is using this run directory"):
            return full_run(folder, config)
    except KeyboardInterrupt:
        print("Stopped. Use --resume with the printed run directory.", flush=True)
        return 130
    except (HostError, OSError, ValueError, ImportError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


def probe_strata(player, model, args, folder):
    _, key, _ = parse_player(player)
    folder.mkdir(parents=True)
    context = model["context_size"]
    receipt = {"player": player, "context_size": context, "status": "starting"}
    gpu_path = folder / "gpu.csv"
    meter = None
    try:
        if context not in args.contexts:
            raise ValueError(f"{player} is pinned to {context} context")
        with gpu_path.open("wb") as output:
            meter = launch(["nvidia-smi", "--query-gpu=timestamp,memory.total,memory.used",
                            "--format=csv,noheader,nounits", "-lms", "250"],
                           stdout=output, stderr=subprocess.DEVNULL)
        port = free_port()
        with strata_server(player, model, port, context, folder) as runtime:
            receipt["server_runtime"] = str(runtime)
            if args.preflight or not args.smoke:
                with start_game(folder / "game", args if args.watch else None) as (host, game):
                    receipt["game"] = game
                    if args.preflight:
                        checks = receipt["preflight"] = []
                        for case in CASES:
                            obs = host.call("observe", case=case)
                            images = worst_case_images(folder, obs)
                            check = measure(player, worst_case_prompt(obs, DEFAULT_ATTEMPTS, prompt_for),
                                            images, model, port, args.model_timeout)
                            checks.append({**check, "case": case, "image_bound": len(images)})
                            print(f"PREFLIGHT {player} {case} " + json.dumps({
                                k: v for k, v in check.items() if k not in {"receipt", "images"}}), flush=True)
                        if not all(check["fits"] for check in checks):
                            raise ValueError("Worst-case review prompt plus output exceeds per-slot n_ctx")
                    else:
                        image = folder / "probe.png"
                        render_scene(image, host.call("observe", case="gap4"), "Empty gap")
                        reply, details = strata_complete(key, 'Read the gap title; reply with {"gap_m":4}.',
                                                         args.model_timeout, port,
                                                         {**model, "max_output_tokens": 512}, [image])
                        receipt["probe"] = {"reply": reply, "receipt": details}
            if args.smoke:
                flags = ["run", "--player", player, "--registry", str(args.registry.resolve()),
                         "--strata-port", str(port), "--case", args.case, "--attempts", str(args.attempts),
                         "--model-timeout", str(args.model_timeout)]
                if args.blueprint:
                    flags.append("--blueprint")
                if args.watch:
                    width, height = args.watch_size
                    flags += ["--watch", "--watch-size", f"{width}x{height}", "--watch-hold", str(args.watch_hold)]
                    if args.watch_mute:
                        flags.append("--watch-mute")
                smoke = folder / "smoke"
                receipt.update(smoke_run=str(smoke), smoke_command=[sys.executable, str(ROOT / "bench.py"), *flags])
                receipt["smoke_exit_code"] = bench_main(flags, run_dir=smoke)
        receipt["status"] = "fit"
    except (HostError, ProviderError, RuntimeError, OSError, ValueError) as error:
        receipt.update(status="blocked", error=str(error))
        print(f"BLOCKED {player}: {error}", flush=True)
    finally:
        stop(meter)
        if gpu_path.is_file():
            samples = [int(match[1]) for match in re.findall(r",\s*(\d+)\s*$", gpu_path.read_text(), re.M)]
            receipt["gpu_peak_mib"] = max(samples, default=None)
        write_json(folder / "runtime.json", receipt)
    return receipt


def probe_main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.epilog = "For comparison options, use --plan v6-full --help."
    parser.add_argument("--model", action="append", metavar="MODEL[@LEVEL]")
    parser.add_argument("--registry", type=Path, default=ROOT / "models.json")
    parser.add_argument("--llama-server", type=Path, help="Use this llama-server executable")
    parser.add_argument("--resolve-only", action="store_true", help="Print cached files and runtimes without launching them")
    parser.add_argument("--contexts", nargs="+", type=int, default=[131072, 98304, 65536, 32768, 16384, 8192])
    parser.add_argument("--save-contexts", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--preflight", action="store_true",
                        help="Measure the five-attempt image and review bound for both gaps instead of the image probe")
    parser.add_argument("--blueprint", action="store_true")
    parser.add_argument("--case", choices=["gap4", "gap8"], default="gap4")
    parser.add_argument("--attempts", type=int, default=1, choices=range(1, 7))
    parser.add_argument("--model-timeout", type=int, default=300)
    add_watch_options(parser)
    args = parser.parse_args(argv)
    entries = json.loads(args.registry.read_text(encoding="utf-8"))
    keys = args.model or [k for k, v in entries.items() if v.get("runtime_build")]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "runs" / (stamp + "-context-probes")
    if not args.resolve_only:
        print(f"Runtime receipts {folder}", flush=True)
    results = []
    for selection in keys:
        try:
            player = registry_player(selection, entries)
            transport, key, level = parse_player(player)
            if key not in entries or not entries[key].get("runtime_build"):
                raise ValueError(f"Unknown cached GGUF model: {key}")
            model = entries[key] if entries[key]["status"] == "blocked" and level is None else model_settings(
                player, args.registry)
        except ValueError as error:
            parser.error(str(error))
        try:
            if transport == "strata":
                model = resolve_strata_files(key, model, args.registry)
            else:
                model = resolve_llama_files(key, model, args.registry, args.llama_server)
        except (ValueError, OSError) as error:
            print(f"BLOCKED {selection}: {error}", flush=True)
            results.append({"status": "blocked"})
            continue
        if args.resolve_only:
            paths = {field: model.get(field) for field in ("weights", "projector", "executable")}
            if model.get("draft"):
                paths["draft"] = model["draft"]
            print(selection, json.dumps(paths))
        else:
            label = f"{key}@{level}" if level else key
            if transport == "strata":
                results.append(probe_strata(player, model, args, folder / label))
            else:
                results.append(run(key, label, model, args, folder / label))
    return int(any(r["status"] != "fit" or r.get("smoke_exit_code", 0) != 0 for r in results))


if __name__ == "__main__":
    argv = sys.argv[1:]
    full_flags = {"--player", "--plan", "--resume", "--dry-plan", "--repeats", "--output"}
    main = full_main if any(arg.split("=", 1)[0] in full_flags for arg in argv) else probe_main
    raise SystemExit(main(argv))
