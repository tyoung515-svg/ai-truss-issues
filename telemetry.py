"""Compact trace feedback and frame selection, independent of the game runtime."""


def frame_peak(sample):
    return max(sample.get("event_stress_pct") or 0,
               max((m.get("stress_pct") or 0 for m in sample["members"]), default=0))


def select_frames(trace, observation):
    samples = sorted(trace["samples"], key=lambda s: s["time_s"])
    if not samples:
        return []
    left, right = observation["anchors"]["L"][0], observation["anchors"]["R"][0]
    # First closest sample handles a car that falls before it reaches a target.
    targets = [min(samples, key=lambda s: abs(s["car"][0] - (left + f * (right - left))))
               for f in (0, .25, .5, .75, 1)]
    peak = max(samples, key=frame_peak)
    broken = next((s for s in samples if any(m["broken"] for m in s["members"])), None)
    # Keep the important event when it is close to a crossing frame.
    selected = []
    for sample in ([broken] if broken else []) + [peak] + targets:
        if all(abs(sample["time_s"] - old["time_s"]) >= .2 for old in selected):
            selected.append(sample)
    return sorted(selected, key=lambda s: s["time_s"])


def trace_feedback(trace, result):
    lines = []
    samples = trace["samples"]
    for member in result.get("members", []):
        peak = member.get("peak_stress_pct")
        if not member["broken"] and (peak or 0) < 30:
            continue
        readings = [(s, m) for s in samples for m in s["members"] if m["member"] == member["member"]]
        if not readings:
            continue
        near, _ = min(readings, key=lambda sm: abs(sm[0]["time_s"] - (member["peak_at_s"] or 0)))
        line = (f"member {member['member']} {member['material']} {member['from']}-{member['to']}: "
                f"peak {peak}% at t={member['peak_at_s']} s, car x={near['car'][0]:.2f}")
        above = [s for s, m in readings if (m.get("stress_pct") or 0) > 50]
        if above:
            line += f"; above 50% from car x={above[0]['car'][0]:.2f} to {above[-1]['car'][0]:.2f}"
        if member["broken"]:
            line += "; broken"
        lines.append(line)
    return lines
