#!/usr/bin/env python3
"""
kaufda.de crawler — fetches current promotions for Köngen (73257).

Fetches:
  - brochures from the city landing page
  - all products from each retailer's store page (parallel requests)
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

CITY_SLUG = "Koengen"
BASE_URL = f"https://www.kaufda.de/{CITY_SLUG}"

# Retailer name → category label (used for filtering in the UI)
RETAILER_CATEGORIES = {
    "rewe":              "Supermarkt",
    "e center":          "Supermarkt",
    "edeka":             "Supermarkt",
    "kaufland":          "Supermarkt",
    "penny":             "Discounter",
    "penny-markt":       "Discounter",
    "aldi":              "Discounter",
    "aldi süd":          "Discounter",
    "aldi nord":         "Discounter",
    "lidl":              "Discounter",
    "netto":             "Discounter",
    "dm":                "Drogerie",
    "dm-drogerie markt": "Drogerie",
    "rossmann":          "Drogerie",
    "müller":            "Drogerie",
    "hornbach":          "Baumarkt",
    "toom baumarkt":     "Baumarkt",
    "obi":               "Baumarkt",
    "bauhaus":           "Baumarkt",
    "mediamarkt":        "Elektronik",
    "mediamarkt saturn": "Elektronik",
    "saturn":            "Elektronik",
    "awg":               "Mode",
    "h&m":               "Mode",
    "zara":              "Mode",
    "decathlon":         "Sport",
    "stihl":             "Garten",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def _category_for(retailer_name: str) -> str:
    key = retailer_name.lower()
    if key in RETAILER_CATEGORIES:
        return RETAILER_CATEGORIES[key]
    for k, cat in RETAILER_CATEGORIES.items():
        if k in key or key in k:
            return cat
    return "Sonstige"


def _fetch(url: str) -> tuple[str, BeautifulSoup | None]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return url, BeautifulSoup(r.text, "html.parser")
    except Exception:
        return url, None


def _next_data(soup: BeautifulSoup) -> dict | None:
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return None
    return json.loads(tag.string)


def fetch_all() -> tuple[list[dict], list[dict]]:
    """Return (brochures, products). Fetches every store linked from the city page."""

    # Step 1: fetch city landing page
    _, city_soup = _fetch(BASE_URL)
    if city_soup is None:
        raise RuntimeError(f"Failed to fetch {BASE_URL}")

    city_data = _next_data(city_soup)
    if not city_data:
        raise RuntimeError("__NEXT_DATA__ not found on city page")

    # Collect ALL store page URLs from the city page (every /p-r link)
    all_store_urls: list[str] = []
    seen_store_urls: set[str] = set()
    for a in city_soup.find_all("a", href=True):
        href = a["href"]
        if "/p-r" in href and href not in seen_store_urls:
            seen_store_urls.add(href)
            all_store_urls.append(href)

    # Step 2: fetch all store pages in parallel; collect brochures + products from each
    brochures: list[dict] = []
    products: list[dict] = []
    seen_brochure_ids: set = set()
    seen_product_ids: set = set()

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch, url): url for url in all_store_urls}
        for future in as_completed(futures):
            store_url, soup = future.result()
            if soup is None:
                continue
            data = _next_data(soup)
            if not data:
                continue
            info = data["props"]["pageProps"].get("pageInformation", {})

            # Brochure: prefer the viewer entry (the one currently open on this store page)
            brochure_candidates = (
                info.get("brochures", {}).get("viewer", [])
                or info.get("brochures", {}).get("topRanked", [])
            )
            for b in brochure_candidates[:1]:  # one brochure per store is enough
                bid = b.get("contentId") or b.get("id")
                if bid in seen_brochure_ids:
                    continue
                seen_brochure_ids.add(bid)
                pub = b.get("publisher", {})
                name = pub.get("name", "")
                pages = b.get("pages", [])
                cover = pages[0].get("url", {}).get("normal", "") if pages else ""
                brochures.append({
                    "retailer": name,
                    "title": b.get("title", ""),
                    "page_count": b.get("pageCount", ""),
                    "valid_from": _fmt_date(b.get("validFrom")),
                    "valid_until": _fmt_date(b.get("validUntil")),
                    "cover_image_url": cover,
                    "retailer_url": store_url,
                    "category": _category_for(name),
                })

            # Products
            main = info.get("offers", {}).get("main", {})
            items = main.get("items", []) if isinstance(main, dict) else []
            for o in items:
                oid = o.get("id")
                if oid in seen_product_ids:
                    continue
                seen_product_ids.add(oid)

                retailer_name = o.get("publisherName", "")
                prices = o.get("prices", {})
                img = o.get("offerImages", {}).get("url", {}).get("normal", "")
                main_price = prices.get("mainPrice", 0) or 0
                orig_price = prices.get("secondaryPrice", 0) or 0
                discount_pct = (
                    round((1 - main_price / orig_price) * 100)
                    if orig_price > main_price > 0
                    else None
                )
                products.append({
                    "id": oid,
                    "title": o.get("title", ""),
                    "description": o.get("description", ""),
                    "brand": o.get("brand", ""),
                    "retailer": retailer_name,
                    "category": _category_for(retailer_name),
                    "price": prices.get("mainPriceFormatted", ""),
                    "price_value": main_price,
                    "original_price": prices.get("secondaryPriceFormatted", ""),
                    "price_per_unit": prices.get("priceByBaseUnit", ""),
                    "discount_pct": discount_pct,
                    "valid_from": _fmt_date(o.get("validFrom")),
                    "valid_until": _fmt_date(o.get("validUntil")),
                    "image_url": img,
                    "retailer_url": store_url,
                })

    brochures.sort(key=lambda b: b["retailer"])
    products.sort(key=lambda p: (p["category"], p["retailer"], p["title"]))
    return brochures, products


def main() -> None:
    print(f"Fetching from {BASE_URL} …", file=sys.stderr)
    try:
        brochures, products = fetch_all()
    except requests.HTTPError as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        sys.exit(1)

    if "--json" in sys.argv:
        print(json.dumps({"brochures": brochures, "products": products},
                         ensure_ascii=False, indent=2))
        return

    print(f"\n{'='*60}")
    print(f"  kaufDA — Köngen 73257  |  {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print(f"{'='*60}")
    print(f"\nProspekte: {len(brochures)}   Produkte: {len(products)}\n")

    for p in products:
        price = p["price"]
        if p["original_price"]:
            price += f" (war {p['original_price']})"
        print(f"[{p['category']:12}] {p['retailer']:20} {price:12}  {p['title']}")


if __name__ == "__main__":
    main()
