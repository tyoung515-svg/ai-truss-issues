"""Run the same bridge-design tasks against Ollama, llama.cpp and Codex models."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from game_prefs import PREFS_KEY, RegistryTree, protect_prefs, require_game_closed
from providers import (
    OLLAMA_PORT, STRATA_PORT, ProviderError, claude_complete, claude_details, codex_complete, codex_details,
    llama_cpp_complete, llama_cpp_details, ollama_complete, ollama_models,
    strata_complete, strata_details,
)
from models import REGISTRY, model_settings, parse_player
from preflight import measure, review_prompt, worst_case_images, worst_case_prompt
from scenes import render_scene, render_frames, render_stress_frame
from telemetry import trace_feedback


ROOT = Path(__file__).resolve().parent
CASES = ("gap4", "gap8")
DEFAULT_OUTPUT = ROOT / "runs"
DEFAULT_ATTEMPTS = 5
PROMPT_VERSION = 6


class HostError(Exception):
    pass


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


class GameHost:
    def __init__(self, folder):
        self.folder = folder
        self.process = None
        self.simulation_timeout = 35

    def wait_json(self, path, timeout):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as error:
                    raise HostError(f"Invalid game response at {path}: {error}") from error
            if self.process and self.process.poll() is not None:
                raise HostError(f"Game exited ({self.process.returncode}). See {self.folder / 'unity.log'}")
            time.sleep(0.12)
        raise HostError(f"Game did not write {path.name}. See {self.folder / 'unity.log'}")

    def call(self, action, **payload):
        request_id = uuid.uuid4().hex
        write_json(self.folder / f"{request_id}.request.json", {"action": action, **payload})
        result = self.wait_json(self.folder / f"{request_id}.result.json",
                                self.simulation_timeout if action == "simulate" else 10)
        trace = self.folder / f"{request_id}.trace.json"
        if trace.exists():
            result["trace_file"] = trace.name
        if result["status"] == "host_error":
            raise HostError(result["error"])
        return result


@contextmanager
def game_lock(lock_path, busy="Another benchmark already has the game open"):
    import msvcrt

    with lock_path.open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise HostError(busy) from error
        yield


@contextmanager
def start_game(folder, watch=None):
    if os.name != "nt":
        raise HostError("The Poly Bridge 2 host runs on the Windows game PC")
    install_path = Path(os.environ["LOCALAPPDATA"]) / "PolyBridgeBenchmark" / "installation.json"
    try:
        install = json.loads(install_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise HostError("Set up the game host with .\\setup.ps1 first") from error
    with game_lock(install_path.with_name("game.lock")), protect_prefs(
        RegistryTree(PREFS_KEY), install_path.with_name("player-prefs.json"), require_game_closed
    ), launch_game(folder, install, watch) as game:
        yield game


@contextmanager
def launch_game(folder, install, watch=None):
    folder.mkdir()
    host = GameHost(folder)
    game = Path(install["game"])
    env = {**os.environ, "PB2_BENCH_SESSION": str(folder)}
    env.pop("PB2_BENCH_WATCH", None)
    info = None
    width, height = (960, 540)
    if watch:
        width, height = watch.watch_size
        env["PB2_BENCH_WATCH"] = "mute" if watch.watch_mute else "sound"
    else:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    env["PB2_BENCH_SIZE"] = f"{width}x{height}"
    try:
        host.process = subprocess.Popen(
            [str(game / "Poly Bridge 2.exe"), "-screen-fullscreen", "0", "-screen-width", str(width),
             "-screen-height", str(height), "-logFile", str(folder / "unity.log")],
            cwd=game, env=env, startupinfo=info,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ready = host.wait_json(folder / "ready.json", 45)
        if ready.get("error"):
            raise HostError(ready["error"])
        if ready.get("pid") != host.process.pid or ready.get("protocol") != 1:
            raise HostError("Game host handshake failed")
        display = ready.get("display")
        if display != {"width": width, "height": height, "mode": "Windowed"}:
            raise HostError(f"Expected a {width}x{height} window, game reported {display}. "
                            "Rebuild the game host with .\\setup.ps1.")
        if watch:
            if "watch" not in ready:
                raise HostError("Rebuild the game host with .\\setup.ps1 for watch mode")
            host.simulation_timeout = ready["watch"]["simulation_wall_limit"] + 13
            print(f"Watch window {ready['watch']['title']} ({width}x{height}, {display['mode']})", flush=True)
        yield host, {**install, **ready}
    finally:
        if host.process is not None and host.process.poll() is None:
            try:
                host.call("quit")
                host.process.wait(timeout=5)
            except (HostError, OSError, subprocess.TimeoutExpired):
                pass
            finally:
                if host.process.poll() is None:
                    host.process.kill()
                    host.process.wait()


def is_clean_pass(result):
    return result["status"] == "passed" and result["broken_members"] == 0


def pass_order(result):
    return result["broken_members"] != 0, result["cost"], result["broken_members"], result["max_stress_pct"]


def player_name(player, entries):
    if player == "reference":
        return "Reference"
    _, name, level = parse_player(player)
    display = entries.get(name, {}).get("name", name or player)
    return f"{display}@{level}" if level else display


def caption_text(name, case, attempt, total, phase, replay=False, repeat=1):
    title = f"{name} · {case.removeprefix('gap')} m gap · try {attempt} of {total}"
    if repeat > 1:
        title += f" · repeat {repeat}"
    return ("Replay · " if replay else "") + title + "\n" + phase + "\n"


def result_caption(outcome):
    status = outcome["status"].replace("_", " ").upper()
    if "cost" not in outcome:
        return status
    breaks = outcome["broken_members"]
    cost = f" ${outcome['cost']:,}" if outcome["status"] == "passed" else ""
    return f"{status}{cost}, {breaks} {'break' if breaks == 1 else 'breaks'}"


def scoreboard_text(name, player, cases, rows, replay=False):
    lines = [("Replay · " if replay else "") + name]
    for case in cases:
        clean = [row["outcome"] for row in rows if row.get("kind") != "review"
                 and row["player"] == player and row["case"] == case and is_clean_pass(row["outcome"])]
        best = f"best ${min(result['cost'] for result in clean):,}" if clean else "no clean pass yet"
        lines.append(f"{case.removeprefix('gap')} m gap · {len(clean)} clean {'pass' if len(clean) == 1 else 'passes'} · {best}")
    return "\n".join(lines) + "\n"


def write_live(path, content):
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_bytes(content)
        # The reader can briefly hold the destination open on Windows.
        limit = time.monotonic() + 2
        while True:
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if time.monotonic() >= limit:
                    raise
                time.sleep(.05)
    finally:
        temporary.unlink(missing_ok=True)


class LiveView:
    def __init__(self, folder, registry, cases, rows, replay=False):
        self.folder = folder / "live"
        self.folder.mkdir(exist_ok=True)
        self.entries = json.loads(registry.read_text(encoding="utf-8"))
        self.cases = cases
        self.rows = rows
        self.replay = replay

    def publish(self, player, case, attempt, total, phase, image=None, repeat=1):
        name = player_name(player, self.entries)
        if image is not None:
            write_live(self.folder / "model-view.png", image.read_bytes())
        board = scoreboard_text(name, player, self.cases, self.rows, self.replay)
        write_live(self.folder / "scoreboard.txt", board.encode("utf-8"))
        caption = caption_text(name, case, attempt, total, phase, self.replay, repeat)
        write_live(self.folder / "caption.txt", caption.encode("utf-8"))


def prompt_for(observation, past, attempts):
    task = {key: value for key, value in observation.items() if key not in {"status"}}
    incumbent = min(
        ((number, item["result"]) for number, item in enumerate(past, 1) if is_clean_pass(item["result"])),
        key=lambda candidate: pass_order(candidate[1]), default=None,
    )
    if incumbent is None:
        objective = (
            "Establish a stable bridge first. Use the available budget to get the car across with zero broken members. "
            "Prioritize a clean crossing over reducing cost until the game confirms one."
        )
    else:
        number, result = incumbent
        objective = (
            f"Attempt {number} is the cheapest verified clean crossing at ${result['cost']:,}. "
            "Use it as your starting point and try to lower the cost while keeping the crossing clean. "
            "The benchmark keeps the cheapest verified clean design, so a failed experiment leaves that design available."
        )
    lines = [
        "Design a bridge for Poly Bridge 2. The actual game will evaluate your complete design.",
        "Work from this prompt alone. Do not call any external tools.",
        "Get the car from the left bank to the victory flag within budget, without broken members.",
        objective,
        "Use horizontal x and vertical y in meters. The scenario already has two fixed anchors named L and R.",
        'Use the literal strings "L" and "R" as edge endpoints. List only new joints in joints.',
        "Keep new joints within the build bounds and clear of both anchors and other joints.",
        "The car start and victory flag are on solid banks. Your road only needs to connect L to R.",
        "Only road is drivable. Each segment must fit its material's maximum Euclidean length.",
        "The road surface should connect the banks. Use structural members to support the road.",
        "Return exactly one JSON object with keys joints and edges and no surrounding prose.",
        "Each joint row is [id,x,y]. Use a JSON string for id and unquoted JSON numbers for x and y.",
        "Each edge row is [from_id,to_id,material]. All three fields are JSON strings.",
        "Include only joints and edges needed for the design. Use distinct IDs of 1–24 characters.",
        'Format example for a different 2 m gap: {"joints":[["J1",1,5]],"edges":[["L","J1","road"],["J1","R","road"]]}',
        "The game host allows at most 48 new joints and 96 edges.",
        "Do not add overlapping joints or multiple edges connecting the same pair of joints.",
        f"Attempt {len(past) + 1} of {attempts}. Submit the full design each attempt.",
        "Scenario: " + json.dumps(task, ensure_ascii=True, separators=(",", ":")),
    ]
    if past:
        lines.append(
            "Member numbers are 1-based edge row numbers. The game reports peak stress by member and "
            "break coordinates in world meters. Use these to revise the design."
        )
        lines.append("Previous designs and game feedback: " + json.dumps(past, separators=(",", ":")))
    return "\n".join(lines)


def parse_design(reply):
    reply = reply.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", reply, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        reply = fenced[1]
    design = json.loads(reply, parse_constant=reject_constant)
    if not isinstance(design, dict) or set(design) != {"joints", "edges"}:
        raise ValueError("Return an object containing exactly joints and edges")
    if not isinstance(design["joints"], list) or not isinstance(design["edges"], list):
        raise ValueError("joints and edges must both be lists")
    return design


def reject_constant(value):
    raise ValueError(f"Invalid JSON number {value}")


def reference_design(observation):
    span = int(observation["anchors"]["R"][0])
    y = observation["road_y"]
    deck = ["L"] + [f"D{x}" for x in range(2, span, 2)] + ["R"]
    bottom = [f"B{x}" for x in range(1, span, 2)]
    joints = [[name, x, y] for name, x in zip(deck[1:-1], range(2, span, 2))]
    joints += [[name, x, y - 1.5] for name, x in zip(bottom, range(1, span, 2))]
    edges = [[a, b, "road"] for a, b in zip(deck, deck[1:])]
    for a, name, b in zip(deck, bottom, deck[1:]):
        edges.extend([[a, name, "wood"], [name, b, "wood"]])
    edges += [[a, b, "wood"] for a, b in zip(bottom, bottom[1:])]
    return {"joints": joints, "edges": edges}


def player_details(player, args):
    if player == "reference":
        return {"transport": "deterministic reference design"}
    transport, name, _ = parse_player(player)
    if transport not in {"claude", "codex", "llama", "ollama", "strata"}:
        raise ProviderError(f"Unknown player {player}")
    settings = model_settings(player, args.registry, args.codex_effort)
    if transport == "codex":
        return {**codex_details(name), "settings": settings}
    if transport == "claude":
        return {**claude_details(name), "settings": settings}
    if transport == "llama":
        return {**llama_cpp_details(name, args.llama_port, settings), "settings": settings}
    if transport == "strata":
        return {**strata_details(name, args.strata_port, settings), "settings": settings}
    for model in ollama_models(args.ollama_port):
        if model["name"] == name:
            return {"transport": transport, "model": name, "digest": model.get("digest"),
                    "settings": settings}
    raise ProviderError(f"Ollama model is not installed: {name}")


def complete(player, prompt, args, images=(), seed=101):
    transport, name, _ = parse_player(player)
    settings = model_settings(player, args.registry, args.codex_effort)
    preflight = None
    if transport in {"llama", "ollama", "strata"}:
        port = getattr(args, f"{transport}_port")
        preflight = measure(player, prompt, images, settings, port, args.model_timeout)
        if not preflight["fits"]:
            raise ProviderError("Actual turn plus output exceeds context: " + json.dumps(preflight))
    if transport == "ollama":
        reply, receipt = ollama_complete(name, prompt, args.model_timeout, settings, images, seed,
                                         port=args.ollama_port)
        return reply, {**receipt, "preflight": preflight}
    if transport == "llama":
        reply, receipt = llama_cpp_complete(name, prompt, args.model_timeout, args.llama_port, settings, images, seed)
        return reply, {**receipt, "preflight": preflight}
    if transport == "strata":
        reply, receipt = strata_complete(name, prompt, args.model_timeout, args.strata_port, settings, images, seed)
        return reply, {**receipt, "preflight": preflight}
    if transport == "codex":
        try:
            return codex_complete(name, prompt, args.model_timeout, settings, images, seed)
        except OSError as error:
            raise ProviderError(f"Codex CLI: {error}") from error
    if transport == "claude":
        try:
            return claude_complete(name, prompt, args.model_timeout, settings, images, seed)
        except OSError as error:
            raise ProviderError(f"Claude CLI: {error}") from error
    raise ProviderError(f"No completion adapter for {player}")


def play(host, player, obs, args, record, run_dir, repeat=1, live=None):
    attempts = 1 if player == "reference" else args.attempts
    past = []
    frames = []
    empty = run_dir / f"{obs['case']}-empty.png"
    if args.blueprint:
        render_scene(empty, obs, "Empty gap")
    for attempt in range(1, attempts + 1):
        prompt = None if player == "reference" else prompt_for(obs, past, attempts)
        reply = design = receipt = None
        review_error = None
        seed = args.seed + (repeat - 1) * len(CASES) * args.attempts + CASES.index(obs["case"]) * args.attempts + attempt - 1
        images = [empty, *frames] if args.blueprint else []
        model_seconds = None
        live_frame = None
        try:
            if player == "reference":
                design = reference_design(obs)
            else:
                if live:
                    live.publish(player, obs["case"], attempt, attempts, "thinking…", images[-1] if images else None, repeat)
                started = time.monotonic()
                try:
                    reply, receipt = complete(player, prompt, args, images, seed)
                finally:
                    model_seconds = round(time.monotonic() - started, 3)
                design = parse_design(reply)
                validation = host.call("validate", case=obs["case"], design=design)
                proposed = run_dir / f"{obs['case']}-review-{uuid.uuid4().hex[:8]}.png"
                render_scene(proposed, obs, "Proposed design before simulation", design, validation)
                review_images = [*images, proposed] if args.blueprint else []
                if live:
                    live.publish(player, obs["case"], attempt, attempts, "checking its drawing…",
                                 review_images[-1] if review_images else None, repeat)
                review = review_prompt(prompt, reply, validation)
                review_reply = review_receipt = review_error = None
                reviewed = "error"
                started = time.monotonic()
                try:
                    review_reply, review_receipt = complete(player, review, args, review_images, seed)
                    confirmation = json.loads(review_reply)
                    if isinstance(confirmation, dict) and set(confirmation) == {"confirm"} and confirmation["confirm"] is True:
                        reviewed = "confirmed"
                    else:
                        revised = parse_design(review_reply)
                        design = revised
                        reviewed = "revised"
                except (ValueError, ProviderError) as error:
                    review_error = str(error)
                record({"kind": "review", "player": player, "case": obs["case"], "repeat": repeat,
                        "attempt": attempt, "seed": seed, "prompt": review, "reply": review_reply,
                        "images": [p.name for p in review_images], "rendered_design_image": proposed.name,
                        "validation": validation, "provider_receipt": review_receipt,
                        "model_seconds": round(time.monotonic() - started, 3),
                        "review_error": review_error, "outcome": {"status": reviewed},
                        "hit_length_limit": (review_receipt or {}).get("finish_reason") in {"length", "limit"},
                        "turn_mode": "fresh turn quoting original prompt and reply for every transport"})
            if live:
                live.publish(player, obs["case"], attempt, attempts, "driving…", repeat=repeat)
            started = time.monotonic()
            outcome = host.call("simulate", case=obs["case"], design=design)
            simulation_seconds = round(time.monotonic() - started, 3)
            if "trace_file" in outcome:
                trace_path = run_dir / "game" / outcome["trace_file"]
                trace = json.loads(trace_path.read_text(encoding="utf-8"))
                outcome["member_feedback"] = trace_feedback(trace, outcome)
                frames = render_frames(run_dir / f"{obs['case']}-{uuid.uuid4().hex[:8]}", obs, design, trace)
                if live and trace["samples"]:
                    live_frame = run_dir / "game" / (trace_path.stem + ".png")
                    render_stress_frame(live_frame, obs, design, trace["samples"][-1])
        except ProviderError as error:
            outcome = {"status": "provider_error", "error": str(error)}
            simulation_seconds = None
        except ValueError as error:
            outcome = {"status": "invalid_response", "error": str(error)}
            simulation_seconds = None
        except HostError as error:
            outcome = {"status": "host_error", "error": str(error)}
            simulation_seconds = None
        record({
            "kind": "attempt", "repeat": repeat, "seed": seed,
            "player": player, "case": obs["case"], "attempt": attempt, "prompt": prompt, "reply": reply,
            "images": [p.name for p in images],
            "trace_file": ("game/" + outcome["trace_file"]) if "trace_file" in outcome else None,
            "stress_frames": [p.name for p in frames],
            "design": design, "provider_receipt": receipt, "model_seconds": model_seconds,
            "review_error": review_error,
            "hit_length_limit": (receipt or {}).get("finish_reason") in {"length", "limit"},
            "simulation_wall_seconds": simulation_seconds, "outcome": outcome,
        })
        detail = f" cost={outcome['cost']} breaks={outcome['broken_members']}" if "cost" in outcome else ""
        print(f"{player} {obs['case']} attempt {attempt}/{attempts} {outcome['status']}{detail}", flush=True)
        if live:
            live.publish(player, obs["case"], attempt, attempts, result_caption(outcome), live_frame, repeat)
            if "broken_members" in outcome:
                time.sleep(args.watch_hold)
                host.call("release")
        if outcome["status"] in {"provider_error", "host_error"}:
            break
        feedback = {key: value for key, value in outcome.items()
                    if key not in {"case", "members", "break_events", "trace_file"}}
        past.append({"design": design, "result": feedback})


def summarize(rows):
    groups = {}
    for row in rows:
        if row.get("kind") == "review":
            continue
        groups.setdefault((row["player"], row["case"]), []).append(row)
    output = []
    for (player, case), trials in groups.items():
        passed = [row for row in trials if row["outcome"]["status"] == "passed"]
        clean = [row for row in passed if is_clean_pass(row["outcome"])]
        best = min(passed, key=lambda row: pass_order(row["outcome"]), default=None)
        output.append({
            "player": player, "case": case, "attempts": sum(row["attempt"] > 0 for row in trials),
            "repeats": sorted({row.get("repeat", 1) for row in trials}),
            "repeat_results": [{"repeat": number, "clean_passes": sum(is_clean_pass(r["outcome"]) for r in trials if r.get("repeat", 1) == number)}
                               for number in sorted({r.get("repeat", 1) for r in trials})],
            "passes": len(passed), "clean_passes": len(clean),
            "first_pass_attempt": passed[0]["attempt"] if passed else None,
            "first_pass_repeat": passed[0].get("repeat", 1) if passed else None,
            "first_clean_pass_attempt": clean[0]["attempt"] if clean else None,
            "first_clean_pass_repeat": clean[0].get("repeat", 1) if clean else None,
            "best_pass_attempt": best["attempt"] if best else None,
            "best_pass_repeat": best.get("repeat", 1) if best else None,
            "best_pass": best["outcome"] if best else None,
            "statuses": [row["outcome"]["status"] for row in trials],
        })
        if label := trials[0].get("summary_label"):
            output[-1]["summary_label"] = label
    return output


def run(args, run_dir=None, repeat_start=1, live_dir=None):
    if run_dir is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        run_dir = (args.output / run_id).resolve()
    run_id = run_dir.name
    run_dir.mkdir(parents=True)
    print(f"Run directory {run_dir}", flush=True)
    rows = []
    players = {}
    cases = args.case or CASES
    manifest = {"run_id": run_id, "prompt_version": PROMPT_VERSION,
                "attempt_budget": args.attempts, "repeats": args.repeats, "seed": args.seed,
                "model_timeout_seconds": args.model_timeout, "players": players,
                "requested_players": args.player, "blueprint": args.blueprint,
                "preflight": [], "requests": []}
    write_json(run_dir / "manifest.json", manifest)
    with (run_dir / "attempts.jsonl").open("w", encoding="utf-8", newline="\n") as log:
        def record(row):
            if label := players.get(row["player"], {}).get("settings", {}).get("summary_label"):
                row["summary_label"] = label
            if row.get("kind") in {"attempt", "review"}:
                row["thinking_level_sent"] = (row.get("provider_receipt") or {}).get("thinking_level_sent")
            rows.append(row)
            log.write(json.dumps(row, allow_nan=False) + "\n")
            log.flush()
            if row.get("kind") in {"attempt", "review"}:
                manifest["requests"].append({key: row.get(key) for key in (
                    "kind", "player", "case", "repeat", "attempt", "seed", "images",
                    "thinking_level_sent", "provider_receipt")})
                write_json(run_dir / "manifest.json", manifest)

        for player in args.player:
            try:
                players[player] = player_details(player, args)
            except (ProviderError, ValueError) as error:
                for case in cases:
                    record({"player": player, "case": case, "attempt": 0,
                            "outcome": {"status": "provider_error", "error": str(error)}})
                print(f"{player} unavailable: {error}", flush=True)
        if players:
            with start_game(run_dir / "game", args if args.watch else None) as (host, game):
                observations = {case: host.call("observe", case=case) for case in cases}
                manifest.update(game=game, cases=observations)
                live = LiveView(live_dir or run_dir, args.registry, cases, rows) if args.watch else None
                if live:
                    manifest["watch_hold_seconds"] = args.watch_hold
                blocked = set()
                for case, obs in observations.items():
                    images = worst_case_images(run_dir, obs) if args.blueprint and any(
                        player != "reference" for player in players) else []
                    for player in players:
                        if player != "reference":
                            try:
                                transport, _, _ = parse_player(player)
                                port = getattr(args, f"{transport}_port", None)
                                check = measure(player, worst_case_prompt(obs, args.attempts, prompt_for), images,
                                                players[player]["settings"], port, args.model_timeout)
                                check.update(player=player, case=case, image_bound=len(images))
                                manifest["preflight"].append(check)
                                print("Preflight " + json.dumps({k: v for k, v in check.items() if k not in {"receipt", "images"}}), flush=True)
                                if check["fits"] is False:
                                    raise ProviderError("Worst-case review prompt plus output exceeds per-slot n_ctx")
                            except ProviderError as error:
                                manifest["preflight"].append({"player": player, "case": case, "status": "refused", "error": str(error)})
                                record({"player": player, "case": case, "attempt": 0,
                                        "outcome": {"status": "provider_error", "error": str(error)}})
                                blocked.add((player, case))
                        write_json(run_dir / "manifest.json", manifest)
                for case, obs in observations.items():
                    for player in players:
                        if (player, case) not in blocked:
                            for repeat in range(repeat_start, repeat_start + args.repeats):
                                play(host, player, obs, args, record, run_dir, repeat, live)
        summary = summarize(rows)
        write_json(run_dir / "summary.json", summary)
    write_json(run_dir / "manifest.json", manifest)
    return int(any(row["outcome"]["status"] in {"provider_error", "host_error"} for row in rows))


def select_replay_rows(rows, entries, case=None, attempt=None, player=None):
    return [row for row in rows
            if row.get("kind", "attempt") == "attempt" and row.get("attempt", 0) > 0
            and isinstance(row.get("design"), dict) and "outcome" in row
            and (case is None or row["case"] == case)
            and (attempt is None or row["attempt"] == attempt)
            and (player is None or player.casefold() in {
                row["player"].casefold(), player_name(row["player"], entries).casefold()})]


def replay(args):
    run_dir = args.run_dir.resolve()
    entries = json.loads(args.registry.read_text(encoding="utf-8"))
    with (run_dir / "attempts.jsonl").open(encoding="utf-8-sig") as source:
        rows = select_replay_rows((json.loads(line) for line in source if line.strip()), entries,
                                  args.case, args.attempt, args.player)
    if not rows:
        raise ValueError("No saved designs match the replay selection")
    manifest = run_dir / "manifest.json"
    budget = json.loads(manifest.read_text(encoding="utf-8-sig")).get("attempt_budget", 0) if manifest.exists() else 0
    cases = tuple(dict.fromkeys(row["case"] for row in rows))
    folder = run_dir / "replays" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    print(f"Replay directory {folder}", flush=True)
    played = []
    differs = False
    with start_game(folder / "game", args) as (host, game):
        live = LiveView(run_dir, args.registry, cases, played, replay=True)
        observations = {case: host.call("observe", case=case) for case in cases}
        for index, row in enumerate(rows, 1):
            player, case, attempt = row["player"], row["case"], row["attempt"]
            repeat = row.get("repeat", 1)
            total = 1 if player == "reference" else max(attempt, budget)
            obs, design = observations[case], row["design"]
            validation = host.call("validate", case=case, design=design)
            proposed = folder / f"{index}-proposed.png"
            render_scene(proposed, obs, "Proposed design before simulation", design, validation)
            live.publish(player, case, attempt, total, "showing the saved design…", proposed, repeat)
            time.sleep(3)
            live.publish(player, case, attempt, total, "driving…", repeat=repeat)
            outcome = host.call("simulate", case=case, design=design)
            final_frame = None
            if "trace_file" in outcome:
                trace = json.loads((folder / "game" / outcome["trace_file"]).read_text(encoding="utf-8"))
                if trace["samples"]:
                    final_frame = folder / f"{index}-stress.png"
                    render_stress_frame(final_frame, obs, design, trace["samples"][-1])
            original = row["outcome"]
            differences = {field: {"original": original.get(field), "replay": outcome.get(field)}
                           for field in ("status", "cost", "broken_members", "max_stress_pct")
                           if original.get(field) != outcome.get(field)}
            differs |= bool(differences)
            played.append({"player": player, "case": case, "attempt": attempt, "repeat": repeat,
                           "outcome": outcome, "differences": differences})
            live.publish(player, case, attempt, total, result_caption(outcome), final_frame, repeat)
            comparison = "matches original" if not differences else "DIFF " + json.dumps(differences)
            print(f"Replay {player} {case} repeat {repeat} attempt {attempt}: {outcome['status']} "
                  f"cost={outcome.get('cost')} breaks={outcome.get('broken_members')} "
                  f"peak={outcome.get('max_stress_pct')} ({comparison})", flush=True)
            if "broken_members" in outcome:
                time.sleep(args.watch_hold)
                host.call("release")
        write_json(folder / "results.json", {"game": game, "attempts": played})
    return int(differs)


def watch_size(value):
    parsed = re.fullmatch(r"([1-9][0-9]*)[xX]([1-9][0-9]*)", value)
    if not parsed:
        raise argparse.ArgumentTypeError("expected positive dimensions WxH, for example 1920x1080")
    return tuple(map(int, parsed.groups()))


def watch_hold(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("expected a finite number of seconds at least zero")
    return seconds


def add_watch_options(parser, replay=False):
    parser.add_argument("--watch", action="store_true", default=replay, help="Show the simulation in a window for recording")
    parser.add_argument("--watch-size", type=watch_size, default=(1920, 1080), metavar="WxH")
    parser.add_argument("--watch-mute", action="store_true", help="Mute game audio in watch mode")
    parser.add_argument("--watch-hold", type=watch_hold, default=8, metavar="SECONDS")


def models():
    try:
        print("Ollama", [model["name"] for model in ollama_models()])
    except ProviderError as error:
        print(f"Ollama unavailable: {error}")
        return 1
    return 0


def main(argv=None, *, run_dir=None, repeat_start=1, live_dir=None):
    parser = argparse.ArgumentParser(description=__doc__)
    cli = parser.add_subparsers(dest="action", required=True)
    cli.add_parser("models", help="List installed Ollama models")
    runner = cli.add_parser("run", help="Launch the game and evaluate selected players")
    runner.add_argument("--player", action="append", required=True,
                        metavar="reference|ollama:MODEL|llama:MODEL[@LEVEL]|strata:MODEL[@LEVEL]|codex:MODEL[@LEVEL]|claude:MODEL[@LEVEL]")
    runner.add_argument("--case", action="append", choices=CASES, help="Select a case. Defaults to both.")
    runner.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS, choices=range(1, 7))
    runner.add_argument("--model-timeout", type=int, default=180)
    runner.add_argument("--registry", type=Path, default=REGISTRY)
    runner.add_argument("--codex-effort", choices=("low", "medium", "high", "xhigh", "max"))
    runner.add_argument("--repeats", type=int, default=1)
    runner.add_argument("--seed", type=int, default=101)
    runner.add_argument("--ollama-think", action="store_true", help="Enable thinking on Ollama models that support it")
    runner.add_argument("--llama-port", type=int, default=18088, help="Local llama.cpp server port")
    runner.add_argument("--strata-port", type=int, default=STRATA_PORT, help="Local Strata server port with strict context checks")
    runner.add_argument("--ollama-port", type=int, default=OLLAMA_PORT, help="Local Ollama server port")
    runner.add_argument("--llama-think", action="store_true", help="Enable thinking for the llama.cpp player")
    runner.add_argument("--blueprint", action="store_true", help="Attach blueprint and feedback images to vision models")
    runner.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    add_watch_options(runner)
    playback = cli.add_parser("replay", help="Show saved designs from a run without making model calls")
    playback.add_argument("run_dir", type=Path)
    playback.add_argument("--case", choices=CASES)
    playback.add_argument("--attempt", type=int)
    playback.add_argument("--player", metavar="NAME", help="Registry display name or player ID")
    playback.add_argument("--registry", type=Path, default=REGISTRY)
    add_watch_options(playback, replay=True)
    args = parser.parse_args(argv)
    if args.action == "models":
        return models()
    if args.action == "replay":
        try:
            return replay(args)
        except (HostError, OSError, ValueError, ImportError) as error:
            parser.exit(1, f"{error}\n")
    if args.model_timeout <= 0:
        parser.error("--model-timeout must be positive")
    if not 1 <= args.llama_port <= 65535:
        parser.error("--llama-port must be between 1 and 65535")
    if not 1 <= args.strata_port <= 65535:
        parser.error("--strata-port must be between 1 and 65535")
    if not 1 <= args.ollama_port <= 65535:
        parser.error("--ollama-port must be between 1 and 65535")
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.blueprint or args.watch:
        try:
            import PIL
        except ImportError:
            parser.error("--blueprint and --watch require Pillow")
    try:
        return run(args, run_dir, repeat_start, live_dir)
    except (HostError, OSError) as error:
        if run_dir is not None:
            raise
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
