"""Separate images share one meter grid, camera and scale per scenario."""

import math
import re
import textwrap

from telemetry import frame_peak, select_frames


def stress_color(value):
    fraction = max(0, min(100, value)) / 100
    return tuple(round(a + (b - a) * fraction) for a, b in zip((25, 160, 100), (225, 40, 45)))


def render_scene(path, obs, title, design=None, validation=None, sample=None):
    from PIL import Image, ImageDraw, ImageFont

    width, height = 820, 540
    image = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
        heading = ImageFont.truetype("arial.ttf", 20)
    except OSError:
        font = heading = ImageFont.load_default()
    left, right = obs["anchors"]["L"], obs["anchors"]["R"]
    xmin, xmax = math.floor(obs["start"][0]) - 1, math.ceil(obs["goal"][0]) + 1
    ymin, ymax = -1, obs["bounds"]["ymax"] + 1
    scale = min((width - 98) / (xmax - xmin), (height - 155) / (ymax - ymin))

    def point(pos):
        return width / 2 + (pos[0] - (xmin + xmax) / 2) * scale, 65 + (ymax - pos[1]) * scale

    def dot(pos, name, color="#17283e"):
        x, y = point(pos)
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color)
        draw.text((x + 5, y - 15), str(name), fill=color, font=font,
                  stroke_width=2, stroke_fill="#f8fafc")

    for x in range(xmin, xmax + 1):
        draw.line((point((x, ymin)), point((x, ymax))), fill="#dde7ef")
    for y in range(ymax + 1):
        draw.line((point((xmin, y)), point((xmax, y))), fill="#dde7ef")
    for a, b, y in ((xmin, left[0], left[1]), (right[0], xmax, right[1])):
        draw.rectangle((*point((a, y)), *point((b, ymin))), fill="#e3e8e6", outline="#768c82")
    draw.text((20, 14), f"{right[0] - left[0]:g} m gap | {title}", font=heading, fill="#17283e")
    draw.text((20, 40), "Coordinates in meters; same grid and scale in every frame", font=font, fill="#48637b")
    error = (validation or {}).get("error", "")
    positions = dict(obs["anchors"])
    rows = (design or {}).get("joints", [])
    # Malformed submitted rows remain the host's responsibility. Draw every drawable row.
    for row in rows:
        if isinstance(row, list) and len(row) == 3 and isinstance(row[0], str) and all(isinstance(x, (int, float)) and math.isfinite(x) for x in row[1:]):
            positions[row[0]] = row[1:]
    if sample:
        positions.update(sample["joints"])
    readings = {m["member"]: m for m in (sample or {}).get("members", [])}
    materials = {"road": "#343d4c", "wood": "#b77731", "steel": "#427cd0"}
    highlights, crosses = [], []
    for index, edge in enumerate((design or {}).get("edges", []), 1):
        if not isinstance(edge, list) or len(edge) != 3 or not all(isinstance(x, str) for x in edge):
            continue
        a, b, material = edge
        if a not in positions or b not in positions:
            continue
        reading = readings.get(index)
        rejected = f"{a}-{b}" in error or f"{b}-{a}" in error or "Cost " in error
        color = "#da1c90" if rejected else materials.get(material, "#888888")
        if reading:
            color = "#922995" if reading["broken"] else stress_color(reading.get("stress_pct") or 0)
        pa, pb = point(positions[a]), point(positions[b])
        draw.line((pa, pb), fill=color, width=4)
        if rejected:
            highlights.append((pa, pb))
        if reading and reading["broken"]:
            cx, cy = (pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2
            crosses.append((cx, cy))
    # Collinear submitted members can cover an earlier rejected segment.
    for pa, pb in highlights:
        draw.line((pa, pb), fill="#da1c90", width=6)
    for cx, cy in crosses:
        draw.line((cx - 7, cy - 7, cx + 7, cy + 7), fill="#f8fafc", width=6)
        draw.line((cx - 5, cy + 5, cx + 5, cy - 5), fill="#922995", width=3)
    for name, pos in positions.items():
        rejected = bool(re.search(r"Joint\s+" + re.escape('"' + name + '"'), error))
        dot(pos, name, "#da1c90" if rejected else "#17283e")
    dot(sample["car"] if sample else obs["start"], "Car", "#315fc4")
    dot(obs["goal"], "Flag", "#2d724d")
    for x in range(xmin, xmax + 1):
        px, py = point((x, ymin))
        draw.text((px, py + 4), str(x), anchor="mt", font=font, fill="#48637b")
    for y in range(ymax + 1):
        px, py = point((xmin, y))
        draw.text((px - 8, py), str(y), anchor="rm", font=font, fill="#48637b")
    if sample:
        for offset in range(101):
            draw.line((20 + offset * 2, height - 37, 20 + offset * 2, height - 26), fill=stress_color(offset))
        draw.text((230, height - 40), "0-100% stress; purple X = broken", font=font, fill="#17283e")
    else:
        draw.text((20, height - 20), "Road: charcoal   Wood: brown   Steel: blue   Rejected: pink", font=font, fill="#17283e")
    if validation:
        message = error or f"Placement valid. Cost ${validation['cost']}; no physics run."
        draw.multiline_text((20, height - 67), "\n".join(textwrap.wrap(message, 108)[:3]),
                            font=font, fill="#a01e61")
    image.save(path)


def render_frames(prefix, obs, design, trace):
    paths = []
    for index, sample in enumerate(select_frames(trace, obs)):
        path = prefix.with_name(prefix.name + f"-frame-{index + 1}.png")
        render_stress_frame(path, obs, design, sample)
        paths.append(path)
    return paths


def render_stress_frame(path, obs, design, sample):
    title = (f"Stress over time: t={sample['time_s']:.2f}s, car x={sample['car'][0]:.2f}m, "
             f"peak {frame_peak(sample):.1f}%")
    render_scene(path, obs, title, design=design, sample=sample)
