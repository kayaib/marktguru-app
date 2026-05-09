#!/usr/bin/env python3
"""
marktguru.de crawler — fetches current promotions for Köngen (73257).
Uses the reverse-engineered marktguru.de API.
"""

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

ZIP_CODE        = "73257"
ZIP_CODE_REGION = "70173"  # Stuttgart — broader regional coverage for national leaflets
CDN_HOST  = "mg2de.b-cdn.net"
API_BASE  = "https://api.marktguru.de/api/v1"
SITE_URL  = "https://www.marktguru.de"

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Industries to fetch (those with indexOffer=true)
TARGET_INDUSTRIES = [
    (1009, "Supermarkt"),
    (1023, "Discounter"),
    (1024, "Drogerie & Gesundheit"),
    (1011, "Baumarkt"),
    (1025, "Elektromarkt"),
    (1004, "Mode & Schuh"),
    (1008, "Sport & Outdoor"),
    (1027, "Zoo & Garten"),
    (1001, "Getränkemarkt"),
    (1003, "Möbelhaus"),
    (1019, "Essen & Genießen"),
]


def _get_api_key() -> str:
    """Extract the current API key from the marktguru.de homepage."""
    r = requests.get(SITE_URL, headers={"User-Agent": HEADERS_BASE["User-Agent"]}, timeout=15)
    r.raise_for_status()
    keys = re.findall(r'apiKey["\'\s:]+([A-Za-z0-9+/=]{20,})', r.text)
    if not keys:
        raise RuntimeError("Could not extract API key from marktguru.de")
    return keys[0]


def _api_headers(api_key: str) -> dict:
    return {**HEADERS_BASE, "X-Apikey": api_key}


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def _retailer_slug(name: str) -> str:
    """Convert retailer name to marktguru URL slug."""
    s = name.lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _image_url(offer_id: int | str, size: str = "medium") -> str:
    return f"https://{CDN_HOST}/api/v1/offers/{offer_id}/images/default/0/{size}.webp"


def _leaflet_image_url(leaflet_id: int | str) -> str:
    return f"https://{CDN_HOST}/api/v1/leaflets/{leaflet_id}/images/pages/0/small.webp"


def _fetch_industry(industry_id: int, industry_name: str, api_key: str, zip_code: str = ZIP_CODE) -> list[dict]:
    """Fetch all offers for one industry, paginating if needed."""
    h = _api_headers(api_key)
    limit = 512
    url = (f"{API_BASE}/offers?as=web&zipCode={zip_code}"
           f"&industryId={industry_id}&limit={limit}&offset=0")
    r = requests.get(url, headers=h, timeout=20)
    r.raise_for_status()
    data = r.json()
    results = data.get("results") or []
    total = data.get("totalResults", 0)

    # Paginate if more results exist
    offset = limit
    while offset < total:
        r2 = requests.get(url.replace("offset=0", f"offset={offset}"), headers=h, timeout=20)
        r2.raise_for_status()
        page = r2.json().get("results") or []
        results.extend(page)
        offset += limit

    offers = []
    for o in results:
        validity = o.get("validityDates", [{}])
        valid_from = _fmt_date(validity[0].get("from") if validity else None)
        valid_until = _fmt_date(validity[0].get("to") if validity else None)
        advertiser = (o.get("advertisers") or [{}])[0]
        category = (o.get("categories") or [{}])[0]
        brand = o.get("brand") or {}
        price = o.get("price") or 0
        old_price = o.get("oldPrice") or 0
        discount_pct = (
            round((1 - price / old_price) * 100)
            if old_price and old_price > price > 0
            else None
        )
        img_count = (o.get("images") or {}).get("count", 0)
        offers.append({
            "id": o["id"],
            "title": (o.get("product") or {}).get("name") or o.get("description") or "",
            "description": o.get("description") or "",
            "brand": brand.get("name") or "",
            "retailer": advertiser.get("name") or "",
            "retailer_id": advertiser.get("id") or "",
            "industry": industry_name,
            "category": category.get("name") or industry_name,
            "price": f"{price:.2f} €".replace(".", ",") if price else "",
            "price_value": price,
            "original_price": f"{old_price:.2f} €".replace(".", ",") if old_price else "",
            "price_per_unit": (
                f"{o['referencePrice']:.2f} € / {(o.get('unit') or {}).get('shortName','')}"
                if o.get("referencePrice") else ""
            ),
            "discount_pct": discount_pct,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "image_url": _image_url(o["id"]) if img_count > 0 else "",
            "has_image": img_count > 0,
        })
    return offers


def _fetch_leaflets(api_key: str) -> list[dict]:
    """Fetch current brochures from local ZIP + regional ZIP for broader coverage."""
    h = _api_headers(api_key)
    seen = set()
    leaflets = []

    for zip_code in [ZIP_CODE, ZIP_CODE_REGION]:
        url = f"{API_BASE}/leafletflights?as=web&zipCode={zip_code}&limit=100&offset=0"
        r = requests.get(url, headers=h, timeout=20)
        if r.status_code != 200:
            continue
        for lf in r.json().get("results") or []:
            flight_id = lf.get("id")
            if flight_id in seen:
                continue
            seen.add(flight_id)
            leaflet_id = lf.get("mainLeafletId") or flight_id
            advertiser = lf.get("advertiser") or {}
            industry = lf.get("industry") or {}
            leaflets.append({
                "id": flight_id,
                "leaflet_id": leaflet_id,
                "retailer": advertiser.get("name") or "",
                "industry": industry.get("name") or "",
                "page_count": lf.get("pageCount") or "",
                "offer_count": lf.get("offerCount") or 0,
                "valid_from": _fmt_date(lf.get("validFrom")),
                "valid_until": _fmt_date(lf.get("validTo")),
                "cover_url": _leaflet_image_url(leaflet_id),
                "url": f"https://www.marktguru.de/rp/{_retailer_slug(advertiser.get('name', ''))}-prospekte",
            })
    return leaflets


def fetch_all() -> tuple[list[dict], list[dict]]:
    """Return (leaflets, products) from marktguru.de for ZIP 73257."""
    print("  Extracting API key from marktguru.de…", file=sys.stderr)
    api_key = _get_api_key()

    print("  Fetching leaflets…", file=sys.stderr)
    leaflets = _fetch_leaflets(api_key)

    print(f"  Fetching offers for {len(TARGET_INDUSTRIES)} industries in parallel…", file=sys.stderr)
    all_offers: list[dict] = []
    seen_ids: set = set()

    # Fetch from both local and regional ZIP codes
    tasks = [(ind_id, ind_name, ZIP_CODE) for ind_id, ind_name in TARGET_INDUSTRIES] + \
            [(ind_id, ind_name, ZIP_CODE_REGION) for ind_id, ind_name in TARGET_INDUSTRIES]

    with ThreadPoolExecutor(max_workers=8) as pool:
        def _fetch_with_zip(args):
            ind_id, ind_name, zip_code = args
            return ind_name, zip_code, _fetch_industry(ind_id, ind_name, api_key, zip_code=zip_code)

        futures = {pool.submit(_fetch_with_zip, t): t for t in tasks}
        counts: dict[str, int] = {}
        for future in as_completed(futures):
            try:
                ind_name, zip_code, offers = future.result()
                added = 0
                for o in offers:
                    if o["id"] not in seen_ids:
                        seen_ids.add(o["id"])
                        all_offers.append(o)
                        added += 1
                counts[ind_name] = counts.get(ind_name, 0) + added
            except Exception as e:
                t = futures[future]
                print(f"  {t[1]} ({t[2]}): error — {e}", file=sys.stderr)

    for ind_name, cnt in sorted(counts.items()):
        print(f"  {ind_name}: {cnt} offers", file=sys.stderr)

    all_offers.sort(key=lambda o: (o["industry"], o["retailer"], o["title"]))
    print(f"  Total: {len(all_offers)} offers, {len(leaflets)} leaflets", file=sys.stderr)
    return leaflets, all_offers


def main() -> None:
    leaflets, offers = fetch_all()
    if "--json" in sys.argv:
        print(json.dumps({"leaflets": leaflets, "offers": offers}, ensure_ascii=False, indent=2))
        return
    print(f"\nLeaflets: {len(leaflets)}  Offers: {len(offers)}\n")
    for o in offers[:20]:
        disc = f" (-{o['discount_pct']}%)" if o["discount_pct"] else ""
        print(f"[{o['industry']:20}] {o['retailer']:20} {o['price']:10}{disc}  {o['title']}")


if __name__ == "__main__":
    main()
