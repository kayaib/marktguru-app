#!/usr/bin/env python3
"""
Combined crawler: marktguru.de + kaufda.de — beide Quellen gleichwertig.

Strategy:
  - Beide Quellen vollständig abrufen (parallel)
  - Nur Angebote und Prospekte der erlaubten Märkte behalten
  - Duplikate über beide Quellen hinweg entfernen (retailer + title + preis)
"""

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# Erlaubte Märkte (lowercase, normalisiert)
ALLOWED_RETAILERS = {
    "aldi nord",
    "aldi süd",
    "dm-drogerie markt",
    "e center",
    "edeka",
    "kaufland",
    "lidl",
    "marktkauf",
    "müller",
    "nahkauf",
    "netto marken-discount",
    "norma",
    "penny",
    "rewe",
    "rossmann",
    "tegut",
    "wasgau",
}

# kaufda industry mapping (kaufda has no industry field — derive from retailer name)
_INDUSTRY_MAP = {
    "rewe":                    "Supermarkt",
    "nahkauf":                 "Supermarkt",
    "e center":                "Supermarkt",
    "edeka":                   "Supermarkt",
    "kaufland":                "Supermarkt",
    "marktkauf":               "Supermarkt",
    "wasgau":                  "Supermarkt",
    "tegut":                   "Supermarkt",
    "penny":                   "Discounter",
    "aldi süd":                "Discounter",
    "aldi nord":               "Discounter",
    "aldi":                    "Discounter",
    "lidl":                    "Discounter",
    "netto marken-discount":   "Discounter",
    "netto":                   "Discounter",
    "norma":                   "Discounter",
    "dm-drogerie markt":       "Drogerie & Gesundheit",
    "dm":                      "Drogerie & Gesundheit",
    "rossmann":                "Drogerie & Gesundheit",
    "müller":                  "Drogerie & Gesundheit",
}


def _industry_for(retailer_name: str) -> str:
    key = retailer_name.lower()
    for k, ind in _INDUSTRY_MAP.items():
        if k in key:
            return ind
    return "Sonstige"


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


# Kanonische Schreibweise (wie in der Allowlist, aber mit Großbuchstaben für die Anzeige)
_CANONICAL_NAMES = {
    "aldi nord":             "ALDI Nord",
    "aldi süd":              "ALDI SÜD",
    "dm-drogerie markt":     "dm-drogerie markt",
    "e center":              "E center",
    "edeka":                 "EDEKA",
    "kaufland":              "Kaufland",
    "lidl":                  "Lidl",
    "marktkauf":             "Marktkauf",
    "müller":                "Müller",
    "nahkauf":               "nahkauf",
    "netto marken-discount": "Netto Marken-Discount",
    "norma":                 "Norma",
    "penny":                 "PENNY",
    "rewe":                  "REWE",
    "rossmann":              "Rossmann",
    "tegut":                 "tegut",
    "wasgau":                "Wasgau",
}


def _is_allowed(retailer_name: str) -> bool:
    norm = _normalize_name(retailer_name)
    if norm in ALLOWED_RETAILERS:
        return True
    for allowed in ALLOWED_RETAILERS:
        # exact substring only if the match covers most of the name (avoid "penny" matching "penny reisen")
        if allowed in norm and len(allowed) / len(norm) > 0.8:
            return True
        if norm in allowed and len(norm) / len(allowed) > 0.8:
            return True
    return False


def _canonical(retailer_name: str) -> str:
    """Return the canonical display name for a retailer, or the original if not in allowlist."""
    norm = _normalize_name(retailer_name)
    for allowed, canonical in _CANONICAL_NAMES.items():
        if allowed in norm or norm in allowed:
            return canonical
    return retailer_name


def fetch_all() -> tuple[list[dict], list[dict]]:
    """Return (leaflets, offers) merged from marktguru + kaufda."""
    import marktguru_crawler as mg
    import kaufda_crawler as kd

    print("  Fetching from marktguru and kaufda in parallel…", file=sys.stderr)

    mg_leaflets: list[dict] = []
    mg_offers:   list[dict] = []
    kd_brochures: list[dict] = []
    kd_products:  list[dict] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        mg_future = pool.submit(mg.fetch_all)
        kd_future = pool.submit(kd.fetch_all)
        try:
            mg_leaflets, mg_offers = mg_future.result()
            print(f"  marktguru: {len(mg_offers)} offers, {len(mg_leaflets)} leaflets", file=sys.stderr)
        except Exception as e:
            print(f"  marktguru FAILED: {e}", file=sys.stderr)
        try:
            kd_brochures, kd_products = kd_future.result()
            print(f"  kaufda:    {len(kd_products)} offers, {len(kd_brochures)} leaflets", file=sys.stderr)
        except Exception as e:
            print(f"  kaufda FAILED: {e}", file=sys.stderr)

    # ── Filter marktguru auf Allowlist ────────────────────────────────────────
    mg_offers_filtered   = [o for o in mg_offers   if _is_allowed(o["retailer"])]
    mg_leaflets_filtered = [l for l in mg_leaflets if _is_allowed(l["retailer"])]

    # ── kaufda-Produkte auf Allowlist filtern + Schema konvertieren ───────────
    kd_converted: list[dict[str, Any]] = []
    kd_seen: set[tuple] = set()
    for p in kd_products:
        if not _is_allowed(p["retailer"]):
            continue
        retailer_norm = _normalize_name(p["retailer"])
        key = (retailer_norm, _normalize_name(p["title"]))
        if key in kd_seen:
            continue
        kd_seen.add(key)
        industry = _industry_for(p["retailer"])
        price_val = p.get("price_value") or 0
        canonical_retailer = _canonical(p["retailer"])
        kd_converted.append({
            "id":             f"kd_{p['id']}",
            "title":          p["title"],
            "description":    p.get("description") or "",
            "brand":          p.get("brand") or "",
            "retailer":       canonical_retailer,
            "retailer_id":    "",
            "industry":       industry,
            "category":       industry,
            "price":          p.get("price") or "",
            "price_value":    price_val,
            "original_price": p.get("original_price") or "",
            "price_per_unit": p.get("price_per_unit") or "",
            "discount_pct":   p.get("discount_pct"),
            "valid_from":     p.get("valid_from") or "",
            "valid_until":    p.get("valid_until") or "",
            "image_url":      p.get("image_url") or "",
            "has_image":      bool(p.get("image_url")),
        })

    # ── kaufda-Prospekte auf Allowlist filtern + Schema konvertieren ──────────
    kd_leaflets: list[dict[str, Any]] = []
    kd_leaflet_seen: set[str] = set()
    for b in kd_brochures:
        if not _is_allowed(b["retailer"]):
            continue
        retailer_norm = _normalize_name(b["retailer"])
        if retailer_norm in kd_leaflet_seen:
            continue
        kd_leaflet_seen.add(retailer_norm)
        slug = re.sub(r"[^a-z0-9]+", "-",
                      b["retailer"].lower()
                      .replace("ä", "ae").replace("ö", "oe")
                      .replace("ü", "ue").replace("ß", "ss")).strip("-")
        kd_leaflets.append({
            "id":          f"kd_{retailer_norm}",
            "leaflet_id":  f"kd_{retailer_norm}",
            "retailer":    _canonical(b["retailer"]),
            "industry":    _industry_for(b["retailer"]),
            "page_count":  b.get("page_count") or "",
            "offer_count": 0,
            "valid_from":  b.get("valid_from") or "",
            "valid_until": b.get("valid_until") or "",
            "cover_url":   b.get("cover_image_url") or "",
            "url":         f"https://www.marktguru.de/rp/{slug}-prospekte",
        })

    # ── Zusammenführen ────────────────────────────────────────────────────────
    all_offers   = mg_offers_filtered + kd_converted
    all_leaflets = mg_leaflets_filtered + kd_leaflets

    # ── Prospekte deduplizieren (gleicher Händler aus beiden Quellen) ─────────
    leaflet_seen: set[str] = set()
    deduped_leaflets: list[dict] = []
    for l in all_leaflets:
        key = _normalize_name(l["retailer"])
        if key in leaflet_seen:
            continue
        leaflet_seen.add(key)
        deduped_leaflets.append(l)
    all_leaflets = deduped_leaflets

    # ── Vision scraping für Prospekte ohne Angebote ───────────────────────────
    try:
        import leaflet_scraper as vs
        # Deduplizieren nach Händler vor dem Vision-Scrape (verhindert mehrfaches Scrapen
        # desselben Prospekts aus verschiedenen ZIP-Codes)
        mg_leaflets_deduped: list[dict] = []
        _seen_retailers: set[str] = set()
        for l in mg_leaflets_filtered:
            key = _normalize_name(l["retailer"])
            if key not in _seen_retailers:
                _seen_retailers.add(key)
                mg_leaflets_deduped.append(l)
        vision_offers = vs.scrape_missing_leaflets(mg_leaflets_deduped, kd_brochures=[
            {**b, "retailer": _canonical(b["retailer"])}
            for b in kd_brochures
            if _is_allowed(b["retailer"])
        ])
        if vision_offers:
            vis_seen: set[str] = {
                _normalize_name(o["retailer"]) + "|" + _normalize_name(o["title"])
                for o in all_offers
            }
            vis_added = []
            for o in vision_offers:
                if not _is_allowed(o["retailer"]):
                    continue
                key = _normalize_name(o["retailer"]) + "|" + _normalize_name(o["title"])
                if key in vis_seen:
                    continue
                vis_seen.add(key)
                o["industry"] = _industry_for(o["retailer"])
                o["category"] = o["industry"]
                vis_added.append(o)
            all_offers.extend(vis_added)
            print(f"  vision gap-fill: {len(vis_added)} offers from "
                  f"{sorted(set(o['retailer'] for o in vis_added))}", file=sys.stderr)
    except Exception as e:
        print(f"  vision scraping skipped: {e}", file=sys.stderr)

    all_offers.sort(key=lambda o: (o["industry"], o["retailer"], o["title"]))

    # ── Angebote deduplizieren (beide Quellen, gleicher Händler+Titel+Preis) ──
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

    mg_count = sum(1 for o in all_offers if not str(o["id"]).startswith("kd_"))
    kd_count = sum(1 for o in all_offers if str(o["id"]).startswith("kd_"))
    print(f"  Combined: {len(all_offers)} offers ({mg_count} marktguru, {kd_count} kaufda), "
          f"{len(all_leaflets)} leaflets", file=sys.stderr)
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
            src = " (kaufda)" if str(offers[next(i for i, x in enumerate(offers) if x["retailer"] == r)]["id"]).startswith("kd_") else ""
            print(f"  {r}: {cnt}{src}")
