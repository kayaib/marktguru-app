#!/usr/bin/env python3
"""
Build script: fetches marktguru.de data and generates docs/index.html.
Run locally or via GitHub Actions.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from combined_crawler import fetch_all
from classify import classify_offers

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "marktguru_template.html")
OUTPUT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "index.html")


def main() -> None:
    leaflets, offers = fetch_all()

    # Classify food/non-food via AI Core (cached)
    classification = classify_offers(offers)
    if classification:
        for o in offers:
            o["is_food"] = classification.get(str(o["id"]))  # True/False/None if unknown

    data_json = json.dumps(
        {"leaflets": leaflets, "offers": offers},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()

    html = html.replace("__DATA_PLACEHOLDER__", data_json)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Built docs/index.html — {len(leaflets)} leaflets, {len(offers)} offers ({ts})")


if __name__ == "__main__":
    main()
