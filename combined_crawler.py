#!/usr/bin/env python3
"""
Combined crawler: marktguru.de (primary) + kaufda.de (gap-filler).

Strategy:
  - Always use marktguru as the primary source (full offer data, images, prices)
  - Add kaufda offers only for retailers that are absent from marktguru results
  - Deduplicate by (retailer, title) to avoid showing the same offer twice
"""

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# Minimum offers a marktguru retailer must have to be considered "covered".
# Retailers below this threshold get topped up from kaufda.
_MG_MIN_OFFERS = 30

# Retailers to exclude from all sources (opticians, travel agencies, banks etc.)
_EXCLUDED_RETAILERS = {
    "Opti-MegaStore", "RAN Tankstelle", "PENNY Reisen", "RIW Touristik",
    "Volksbank Raiffeisenbank", "Schöffel-LOWA", "Kochlöffel",
    "Bosch Car Service", "ElectronicPartner", "Tuinmaximaal",
}

# kaufda industry mapping (kaufda has no industry field — derive from retailer name)
_INDUSTRY_MAP = {
    "rewe":              "Supermarkt",
    "nahkauf":           "Supermarkt",
    "e center":          "Supermarkt",
    "edeka":             "Supermarkt",
    "kaufland":          "Supermarkt",
    "marktkauf":         "Supermarkt",
    "globus":            "Supermarkt",
    "hit":               "Supermarkt",
    "cap markt":         "Supermarkt",
    "bioladen":          "Supermarkt",
    "alnatura":          "Supermarkt",
    "penny":             "Discounter",
    "aldi süd":          "Discounter",
    "aldi nord":         "Discounter",
    "aldi":              "Discounter",
    "lidl":              "Discounter",
    "netto":             "Discounter",
    "dm":                "Drogerie & Gesundheit",
    "dm-drogerie markt": "Drogerie & Gesundheit",
    "rossmann":          "Drogerie & Gesundheit",
    "müller":            "Drogerie & Gesundheit",
    "hornbach":          "Baumarkt",
    "obi":               "Baumarkt",
    "toom":              "Baumarkt",
    "bauhaus":           "Baumarkt",
    "globus baumarkt":   "Baumarkt",
    "mediamarkt":        "Elektromarkt",
    "saturn":            "Elektromarkt",
    "expert":            "Elektromarkt",
    "awg":               "Mode & Schuh",
    "h&m":               "Mode & Schuh",
    "decathlon":         "Sport & Outdoor",
    "stihl":             "Zoo & Garten",
    "pflanzen":          "Zoo & Garten",
    "metro":             "Getränkemarkt",
    "selgros":           "Getränkemarkt",
    "trinkgut":          "Getränkemarkt",
    "ikea":              "Möbelhaus",
    "xxxlutz":           "Möbelhaus",
    "mömax":             "Möbelhaus",
    "möbel":             "Möbelhaus",
    "galeria":           "Mode & Schuh",
    "smyths":            "Elektromarkt",
}


def _industry_for(retailer_name: str) -> str:
    key = retailer_name.lower()
    for k, ind in _INDUSTRY_MAP.items():
        if k in key:
            return ind
    return "Sonstige"


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


def fetch_all() -> tuple[list[dict], list[dict]]:
    """Return (leaflets, offers) merged from marktguru + kaufda."""
    import marktguru_crawler as mg
    import kaufda_crawler as kd

    print("  Fetching from marktguru and kaufda in parallel…", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=2) as pool:
        mg_future = pool.submit(mg.fetch_all)
        kd_future = pool.submit(kd.fetch_all)
        mg_leaflets, mg_offers = mg_future.result()
        kd_brochures, kd_products = kd_future.result()

    # ── Build set of retailers well-covered by marktguru ──────────────────────
    mg_counts: dict[str, int] = {}
    for o in mg_offers:
        mg_counts[_normalize_name(o["retailer"])] = mg_counts.get(_normalize_name(o["retailer"]), 0) + 1

    covered = {name for name, cnt in mg_counts.items() if cnt >= _MG_MIN_OFFERS}
    print(f"  marktguru: {len(mg_offers)} offers, {len(mg_leaflets)} leaflets", file=sys.stderr)
    print(f"  kaufda:    {len(kd_products)} offers, {len(kd_brochures)} leaflets", file=sys.stderr)

    # ── Convert kaufda products to marktguru schema ───────────────────────────
    # Deduplicate within kaufda by (retailer, title)
    kd_seen: set[tuple[str, str]] = set()
    kd_converted: list[dict[str, Any]] = []
    for p in kd_products:
        retailer_norm = _normalize_name(p["retailer"])
        if retailer_norm in covered:
            continue  # marktguru already covers this retailer well
        key = (retailer_norm, _normalize_name(p["title"]))
        if key in kd_seen:
            continue
        kd_seen.add(key)
        industry = _industry_for(p["retailer"])
        price_val = p.get("price_value") or 0
        kd_converted.append({
            "id":            f"kd_{p['id']}",
            "title":         p["title"],
            "description":   p.get("description") or "",
            "brand":         p.get("brand") or "",
            "retailer":      p["retailer"],
            "retailer_id":   "",
            "industry":      industry,
            "category":      industry,
            "price":         p.get("price") or "",
            "price_value":   price_val,
            "original_price": p.get("original_price") or "",
            "price_per_unit": p.get("price_per_unit") or "",
            "discount_pct":  p.get("discount_pct"),
            "valid_from":    p.get("valid_from") or "",
            "valid_until":   p.get("valid_until") or "",
            "image_url":     p.get("image_url") or "",
            "has_image":     bool(p.get("image_url")),
        })

    # ── Convert kaufda brochures to marktguru leaflet schema ──────────────────
    kd_leaflets: list[dict[str, Any]] = []
    kd_leaflet_seen: set[str] = set()
    for b in kd_brochures:
        retailer_norm = _normalize_name(b["retailer"])
        if retailer_norm in covered:
            continue
        if retailer_norm in kd_leaflet_seen:
            continue
        kd_leaflet_seen.add(retailer_norm)
        slug = re.sub(r"[^a-z0-9]+", "-",
                      b["retailer"].lower()
                      .replace("ä","ae").replace("ö","oe")
                      .replace("ü","ue").replace("ß","ss")).strip("-")
        kd_leaflets.append({
            "id":          f"kd_{retailer_norm}",
            "leaflet_id":  f"kd_{retailer_norm}",
            "retailer":    b["retailer"],
            "industry":    _industry_for(b["retailer"]),
            "page_count":  b.get("page_count") or "",
            "offer_count": 0,
            "valid_from":  b.get("valid_from") or "",
            "valid_until": b.get("valid_until") or "",
            "cover_url":   b.get("cover_image_url") or "",
            "url":         f"https://www.marktguru.de/rp/{slug}-prospekte",
        })

    all_offers   = [o for o in mg_offers + kd_converted if o["retailer"] not in _EXCLUDED_RETAILERS]
    all_leaflets = [l for l in mg_leaflets + kd_leaflets if l["retailer"] not in _EXCLUDED_RETAILERS]

    # ── Vision scraping for retailers with offerCount=0 ───────────────────────
    try:
        import leaflet_scraper as vs
        vision_offers = vs.scrape_missing_leaflets(mg_leaflets)
        if vision_offers:
            vis_seen: set[str] = {_normalize_name(o["retailer"]) + "|" + _normalize_name(o["title"])
                                  for o in all_offers}
            vis_added = []
            for o in vision_offers:
                key = _normalize_name(o["retailer"]) + "|" + _normalize_name(o["title"])
                if key in vis_seen:
                    continue
                vis_seen.add(key)
                # Set industry based on retailer name
                o["industry"]  = _industry_for(o["retailer"])
                o["category"]  = o["industry"]
                vis_added.append(o)
            all_offers.extend(vis_added)
            print(f"  vision gap-fill: {len(vis_added)} offers from "
                  f"{sorted(set(o['retailer'] for o in vis_added))}", file=sys.stderr)
    except Exception as e:
        print(f"  vision scraping skipped: {e}", file=sys.stderr)

    all_offers.sort(key=lambda o: (o["industry"], o["retailer"], o["title"]))

    # ── Deduplicate marktguru offers (same retailer+title+price from multiple branches) ──
    dedup_seen: set[tuple] = set()
    deduped: list[dict] = []
    for o in all_offers:
        key = (_normalize_name(o["retailer"]), _normalize_name(o["title"]), o.get("price_value", 0))
        if key in dedup_seen:
            continue
        dedup_seen.add(key)
        deduped.append(o)
    removed = len(all_offers) - len(deduped)
    if removed:
        print(f"  Deduplicated {removed} duplicate offers", file=sys.stderr)
    all_offers = deduped

    added = [p["retailer"] for p in kd_converted]
    if added:
        unique_added = sorted(set(added))
        print(f"  kaufda gap-fill: {len(kd_converted)} offers from {unique_added}", file=sys.stderr)

    print(f"  Combined: {len(all_offers)} offers, {len(all_leaflets)} leaflets", file=sys.stderr)
    return all_leaflets, all_offers


if __name__ == "__main__":
    import json
    leaflets, offers = fetch_all()
    if "--json" in sys.argv:
        print(json.dumps({"leaflets": leaflets, "offers": offers}, ensure_ascii=False, indent=2))
    else:
        retailers: dict[str, int] = {}
        for o in offers:
            retailers[o["retailer"]] = retailers.get(o["retailer"], 0) + 1
        for r, cnt in sorted(retailers.items()):
            src = "" if not str(offers[next(i for i,x in enumerate(offers) if x["retailer"]==r)]["id"]).startswith("kd_") else " (kaufda)"
            print(f"  {r}: {cnt}{src}")
