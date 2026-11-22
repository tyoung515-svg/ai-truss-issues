"""Draw the scenario and the most recent simulated design as a labeled PNG."""

import math


def render_blueprint(path, observation, history):
    from PIL import Image, ImageDraw, ImageFont

    width, height = 820, 500
    image = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(image)

    def font(size):
        try:
            return ImageFont.truetype("arial.ttf", size)
        except OSError:
            return ImageFont.load_default(size=size)

    label, small, heading = font(16), font(13), font(25)
    ink, muted = "#17283e", "#48637b"
    left, right = observation["anchors"]["L"], observation["anchors"]["R"]
    spawn, goal = observation["start"], observation["goal"]
    xmin = math.floor(spawn[0]) - 1
    xmax = math.ceil(goal[0]) + 1
    ymin, ymax = -1, observation["bounds"]["ymax"] + 1
    scale = min((width - 98) / (xmax - xmin), (height - 104) / (ymax - ymin))

    def point(position):
        x, y = position
        return width / 2 + (x - (xmin + xmax) / 2) * scale, 57 + (ymax - y) * scale

    def dot(position, radius, fill):
        x, y = point(position)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)

    for x in range(xmin, xmax + 1):
        draw.line((point((x, ymin)), point((x, ymax))), fill="#dde7ef")
        if x % 2 == 0:
            px, py = point((x, ymin))
            draw.text((px, py + 5), str(x), fill=muted, font=small, anchor="mt")
    for y in range(0, ymax):
        draw.line((point((xmin, y)), point((xmax, y))), fill="#dde7ef")
        if y % 2 == 0 or y == 5:
            px, py = point((xmin, y))
            draw.text((px - 7, py), str(y), fill=muted, font=small, anchor="rm")
    for bank_start, bank_end, bank_y in ((xmin, left[0], left[1]), (right[0], xmax, right[1])):
        draw.rectangle((*point((bank_start, bank_y)), *point((bank_end, ymin))),
                       fill="#e3e8e6", outline="#768c82")
    draw.text((26, 18), f"{right[0] - left[0]:g} m gap", fill=ink, font=heading)
    draw.text((width - 26, 23), "Coordinates in meters", fill=muted, font=label, anchor="rt")

    last_simulated = next(
        (index for index in range(len(history) - 1, -1, -1) if "members" in history[index]["result"]), None
    )
    if last_simulated is not None:
        simulated = history[last_simulated]
        design, result = simulated["design"], simulated["result"]
        positions = {**observation["anchors"], **{name: [x, y] for name, x, y in design["joints"]}}
        for member in sorted(result["members"], key=lambda entry: entry["peak_stress_pct"] or 0):
            stress = member["peak_stress_pct"]
            color = "#627686"
            if stress is not None:
                color = "#21858b" if stress < 50 else "#c4781b" if stress < 90 else "#c83d43"
            draw.line((point(positions[member["from"]]), point(positions[member["to"]])),
                      fill=color, width=4)
        for name, x, y in design["joints"]:
            dot((x, y), 3, ink)
            if len(design["joints"]) <= 18:
                px, py = point((x, y))
                draw.text((px + 6, py - 7), name, fill=ink, font=small, anchor="lb",
                          stroke_width=2, stroke_fill="#f8fafc")
        for event in result["break_events"]:
            for position in event["points"]:
                if xmin <= position[0] <= xmax and ymin <= position[1] <= ymax:
                    x, y = point(position)
                    draw.line((x - 5, y - 5, x + 5, y + 5), fill="#922995", width=3)
                    draw.line((x - 5, y + 5, x + 5, y - 5), fill="#922995", width=3)
        draw.text((26, height - 26), f"Attempt {last_simulated + 1} peak stress", fill=ink, font=small)
        for x, color, description in ((214, "#21858b", "<50%"), (282, "#c4781b", "50–89%"),
                                      (376, "#c83d43", "90%+"), (460, "#922995", "Break point")):
            draw.rectangle((x, height - 23, x + 10, height - 13), fill=color)
            draw.text((x + 16, height - 26), description, fill=ink, font=small)

    for name, position, color in (("L", left, ink), ("R", right, ink),
                                  ("Car start", spawn, "#315fc4"), ("Victory flag", goal, "#2d724d")):
        dot(position, 6, color)
        x, y = point(position)
        draw.text((x, y - 11), name, fill=color, font=label, anchor="mb",
                  stroke_width=2, stroke_fill="#f8fafc")
    image.save(path)
