"""Refresh README evidence only; never download weights or choose sampling values."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "model-cards" / "cache"


def collect(item):
    key, receipt = item
    url = receipt["url"]
    result = {"url": url, "revision": receipt["revision"]}
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read()
        (CACHE / (key.replace(":", "-") + ".md")).write_bytes(data)
        return key, {**result, "retrieved_on": datetime.now(timezone.utc).date().isoformat(),
                     "sha256": hashlib.sha256(data).hexdigest(), "status": "retrieved"}
    except (OSError, ValueError) as error:
        return key, {**result, "status": "blocked", "error": str(error)}


if __name__ == "__main__":
    pinned = json.loads((CACHE.parent / "receipts.json").read_text(encoding="utf-8"))
    items = [(key, receipt) for key, receipt in pinned.items() if receipt["status"] == "retrieved"]
    CACHE.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = dict(pool.map(collect, items))
    (CACHE / "receipts.json").write_text(
        json.dumps(receipts, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(receipts, indent=2))
