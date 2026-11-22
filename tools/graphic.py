"""Render comparable v6 summary rows without depending on a machine-local launcher."""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("comparison.png"))
    args = parser.parse_args()
    rows, conditions = [], set()
    for folder in args.runs:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        conditions.add((manifest["prompt_version"], manifest["attempt_budget"], manifest["blueprint"]))
        rows.extend(json.loads((folder / "summary.json").read_text(encoding="utf-8")))
    if len(conditions) != 1:
        parser.error("Use matching prompt versions, attempt budgets and image modes")
    image = Image.new("RGB", (1100, 110 + 42 * len(rows)), "#f8fafc")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    draw.text((24, 20), "Poly Bridge 2 - clean crossings and cheapest verified cost", font=font, fill="#17283e")
    for index, row in enumerate(rows):
        cost = row["best_pass"]["cost"] if row["best_pass"] else "--"
        label = row.get("summary_label") or row["player"]
        text = f"{label}   {row['case']}   {row['clean_passes']}/{row['attempts']} clean   ${cost}   repeats {row.get('repeats', [1])}"
        draw.text((24, 75 + 42 * index), text, font=font, fill="#17283e")
    image.save(args.output)
    print(args.output)
