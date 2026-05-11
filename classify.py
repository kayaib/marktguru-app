#!/usr/bin/env python3
"""
AI Core classifier: classifies offers as food/non-food using SAP BTP AI Core
(OpenAI-compatible endpoint). Results are cached in classification_cache.json.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

CACHE_FILE = Path(__file__).parent / "classification_cache.json"
BATCH_SIZE = 50

AICORE_AUTH_URL  = "https://retail-ai-g1f2q3e8.authentication.eu10.hana.ondemand.com"
AICORE_API_URL   = "https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com"
AICORE_CLIENT_ID = os.environ.get("AICORE_CLIENT_ID", "")
AICORE_CLIENT_SECRET = os.environ.get("AICORE_CLIENT_SECRET", "")
AICORE_DEPLOYMENT_ID = os.environ.get("AICORE_DEPLOYMENT_ID", "")

_token_cache: dict = {}


def _get_token() -> str:
    if _token_cache.get("token") and time.time() < _token_cache.get("expires", 0) - 60:
        return _token_cache["token"]
    r = requests.post(
        f"{AICORE_AUTH_URL}/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(AICORE_CLIENT_ID, AICORE_CLIENT_SECRET),
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires"] = time.time() + data.get("expires_in", 3600)
    return _token_cache["token"]


def _classify_batch(offers: list[dict]) -> dict[str, bool]:
    """Classify a batch of offers. Returns {offer_id: is_food}."""
    token = _get_token()
    url = f"{AICORE_API_URL}/v2/inference/deployments/{AICORE_DEPLOYMENT_ID}/v1/chat/completions"

    lines = "\n".join(
        f"{i+1}. {o['title']}" + (f" ({o.get('description','')})" if o.get("description") and o["description"] != o["title"] else "")
        for i, o in enumerate(offers)
    )

    prompt = f"""You are a product classifier. Classify each product as "food" or "nonfood".

FOOD = products humans eat or drink: groceries, fruits, vegetables, meat, dairy, beverages, snacks, spices, cooking ingredients.
NONFOOD = everything else: electronics, furniture, garden equipment, clothing, toys, pet supplies (including pet food), cleaning products, cosmetics, tools, outdoor furniture, garden swings, hammocks, etc.

Important: pet food (dog food, cat food) is NONFOOD. Garden furniture (Hollywoodschaukel, Schaukel, Liegestuhl) is NONFOOD.

Respond with ONLY a JSON array of strings in the same order as the input. Use only "food" or "nonfood".
Example for 3 products: ["food","nonfood","food"]

Products to classify:
{lines}"""

    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": BATCH_SIZE * 12,
    }

    r = requests.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "AI-Resource-Group": "default",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    r.raise_for_status()

    content = r.json()["choices"][0]["message"]["content"].strip()
    # Extract JSON array from response
    start = content.find("[")
    end   = content.rfind("]") + 1
    labels = json.loads(content[start:end])

    return {
        str(offers[i]["id"]): (labels[i].strip().lower() == "food")
        for i in range(min(len(offers), len(labels)))
    }


def classify_offers(offers: list[dict]) -> dict[str, bool]:
    """
    Classify all offers, using cache for already-seen IDs.
    Returns {offer_id: is_food} for all offers.
    """
    if not AICORE_CLIENT_ID or not AICORE_CLIENT_SECRET or not AICORE_DEPLOYMENT_ID:
        print("  AI Core credentials not set — skipping LLM classification", file=sys.stderr)
        return {}

    # Load cache
    cache: dict[str, bool] = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    # Find uncached offers
    uncached = [o for o in offers if str(o["id"]) not in cache]
    if uncached:
        print(f"  Classifying {len(uncached)} new offers via AI Core…", file=sys.stderr)
        for i in range(0, len(uncached), BATCH_SIZE):
            batch = uncached[i:i + BATCH_SIZE]
            try:
                result = _classify_batch(batch)
                cache.update(result)
                print(f"  Classified batch {i//BATCH_SIZE + 1}/{(len(uncached)-1)//BATCH_SIZE + 1}", file=sys.stderr)
            except Exception as e:
                print(f"  Classification error (batch {i//BATCH_SIZE + 1}): {e}", file=sys.stderr)
        # Save updated cache
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  Cache saved ({len(cache)} entries)", file=sys.stderr)
    else:
        print(f"  All {len(offers)} offers already classified (cached)", file=sys.stderr)

    return cache


if __name__ == "__main__":
    # Quick connection test
    test_offers = [
        {"id": "test_1", "title": "Banane", "description": ""},
        {"id": "test_2", "title": "Alkaline Batterien AA", "description": ""},
        {"id": "test_3", "title": "Sonnenschirm", "description": ""},
        {"id": "test_4", "title": "Joghurt mild 500g", "description": ""},
        {"id": "test_5", "title": "Trimmer Haarschneidemaschine", "description": ""},
    ]
    print("Testing AI Core connection…")
    result = classify_offers(test_offers)
    if result:
        for o in test_offers:
            label = "Food" if result.get(str(o["id"])) else "Non-Food"
            print(f"  {label:10} {o['title']}")
    else:
        print("No result — check credentials.")
