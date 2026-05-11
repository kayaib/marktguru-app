#!/usr/bin/env python3
"""
Leaflet Vision Scraper — for retailers that marktguru has as prospekt-only (low offerCount).
Downloads page images from marktguru CDN (or prospektmaschine.de for dm) and extracts
offers via gpt-4o Vision (AI Core).  Product images are cropped from the page using
bounding boxes returned by the vision model and saved to docs/images/.
Results cached in leaflet_offers_cache.json.
"""

import base64
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image
import requests

CACHE_FILE  = Path(__file__).parent / "leaflet_offers_cache.json"
IMAGES_DIR  = Path(__file__).parent / "docs" / "images"
CDN_HOST    = "mg2de.b-cdn.net"

# Scrape leaflets where marktguru has fewer than this many indexed offers
_SCRAPE_THRESHOLD = 50

AICORE_AUTH_URL      = "https://retail-ai-g1f2q3e8.authentication.eu10.hana.ondemand.com"
AICORE_API_URL       = "https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com"
AICORE_CLIENT_ID     = os.environ.get("AICORE_CLIENT_ID", "")
AICORE_CLIENT_SECRET = os.environ.get("AICORE_CLIENT_SECRET", "")
AICORE_DEPLOYMENT_ID = os.environ.get("AICORE_DEPLOYMENT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Blacklist: skip these retailers even if they have few/no indexed offers
# (travel agencies, gas stations, banks, insurance — not grocery/retail)
_IRRELEVANT_RETAILERS = {
    "RAN Tankstelle", "PENNY Reisen", "RIW Touristik",
    "Volksbank Raiffeisenbank", "Schöffel-LOWA", "Kochlöffel",
    "Bosch Car Service", "Matratzen Concord", "ElectronicPartner",
    "Tuinmaximaal",
}

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


def _page_url(leaflet_id: str, page: int) -> str:
    return f"https://{CDN_HOST}/api/v1/leaflets/{leaflet_id}/images/pages/{page}/medium.webp"


# ── prospektmaschine.de (dm source) ─────────────────────────────────────────

def _fetch_pm_leaflets() -> list[dict]:
    """Fetch dm leaflets from prospektmaschine.de (dm is absent from marktguru)."""
    url = "https://www.prospektmaschine.de/dm-drogerie/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  prospektmaschine fetch failed: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    leaflets = []
    seen_ids: set[str] = set()
    for card in soup.select(".brochure-thumb"):
        bid = card.get("data-brochure-id", "")
        if not bid or bid in seen_ids:
            continue
        shop_name = card.select_one(".shop-name")
        if shop_name and "dm" not in shop_name.get_text().lower():
            continue
        seen_ids.add(bid)
        link = card.select_one("a[href]")
        dates_el = card.select_one(".hidden-sm")
        dates_text = dates_el.get_text(strip=True) if dates_el else ""
        valid_from, valid_until = "", ""
        m = re.match(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})", dates_text)
        if m:
            valid_from, valid_until = m.group(1), m.group(2)
        leaflets.append({
            "leaflet_id": f"pm_{bid}",
            "retailer": "dm-drogerie markt",
            "offer_count": 0,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "_pm_brochure_id": bid,
            "_pm_detail_url": link["href"] if link else "",
        })
    return leaflets[:1]


def _fetch_pm_page_images(brochure_id: str, detail_url: str) -> list[str]:
    """Return signed page image URLs from a prospektmaschine brochure detail page."""
    if not detail_url.startswith("http"):
        detail_url = "https://www.prospektmaschine.de" + detail_url
    try:
        r = requests.get(detail_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  prospektmaschine detail fetch failed: {e}", file=sys.stderr)
        return []

    m_pages = re.search(r"var maxPages\s*=\s*(\d+)", r.text)
    max_pages = int(m_pages.group(1)) if m_pages else 0
    if max_pages == 0:
        return []

    imgs = []
    img_matches = re.findall(
        r'https://eu\.leafletscdn\.com/thumbor/[^"\']+/de/data/\d+/' + brochure_id + r'/0\.jpg[^"\']*',
        r.text
    )
    p0_url = next((u for u in img_matches if "640x640" in u), img_matches[0] if img_matches else "")
    if not p0_url:
        return []

    imgs.append(p0_url)
    for page in range(1, max_pages):
        try:
            rp = requests.get(f"{detail_url}?page={page + 1}", headers=HEADERS, timeout=15)
            matches = re.findall(
                r'https://eu\.leafletscdn\.com/thumbor/[^"\']+/de/data/\d+/' + brochure_id + r'/' + str(page) + r'\.jpg[^"\']*',
                rp.text
            )
            page_url = next((u for u in matches if "640x640" in u), matches[0] if matches else "")
            if page_url:
                imgs.append(page_url)
        except Exception:
            pass
    return imgs


# ── image fetching ────────────────────────────────────────────────────────────

def _fetch_page_bytes(url: str) -> bytes | None:
    """Download a page image, return raw bytes."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        return r.content if r.status_code == 200 else None
    except Exception:
        return None


def _fetch_mg_page_bytes(leaflet_id: str, page: int) -> bytes | None:
    return _fetch_page_bytes(_page_url(leaflet_id, page))


# ── crop helper ───────────────────────────────────────────────────────────────

def _crop_and_save(img_bytes: bytes, bbox: list, offer_id: str) -> str:
    """
    Crop a product image from a page using bbox [x1,y1,x2,y2] (0-100 percentages).
    Saves to docs/images/{offer_id}.jpg (120×120 px). Returns relative path.
    """
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        x1 = max(0, int(bbox[0] / 100 * w))
        y1 = max(0, int(bbox[1] / 100 * h))
        x2 = min(w, int(bbox[2] / 100 * w))
        y2 = min(h, int(bbox[3] / 100 * h))
        if x2 <= x1 or y2 <= y1:
            return ""
        crop = img.crop((x1, y1, x2, y2)).resize((120, 120), Image.LANCZOS)
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^a-z0-9_]", "_", offer_id)[:60]
        out_path = IMAGES_DIR / f"{safe_id}.jpg"
        crop.save(out_path, "JPEG", quality=80)
        return f"images/{safe_id}.jpg"
    except Exception as e:
        print(f"    crop error for {offer_id}: {e}", file=sys.stderr)
        return ""


# ── vision analysis ───────────────────────────────────────────────────────────

def _analyze_page(img_bytes: bytes, retailer: str, token: str) -> list[dict]:
    """Send one page to gpt-4o Vision and extract offers with bounding boxes."""
    url = f"{AICORE_API_URL}/v2/inference/deployments/{AICORE_DEPLOYMENT_ID}/v1/chat/completions"

    # Detect image format from magic bytes
    mime = "image/webp" if img_bytes[:4] == b"RIFF" or img_bytes[8:12] == b"WEBP" else "image/jpeg"
    b64 = base64.b64encode(img_bytes).decode()

    prompt = """Extract ALL product offers visible on this supermarket/retailer flyer page.
For each offer return a JSON object with these fields:
- title: product name (string)
- brand: brand name if visible, else ""
- price: formatted price string e.g. "1,99 €", "" if not visible
- price_value: numeric price as float, 0 if not visible
- original_price: original price if crossed out, else ""
- discount_pct: discount percentage as integer if shown, else null
- valid_from: validity start date as DD.MM.YYYY if shown, else ""
- valid_until: validity end date as DD.MM.YYYY if shown, else ""
- description: short description or weight/quantity if shown, else ""
- bbox: bounding box of the product IMAGE (not text) as [x1, y1, x2, y2] percentages 0-100 of page size. null if no product image visible.

Return ONLY a JSON array. If no offers are visible, return [].
Do not include non-product content (logos, decorations, store info)."""

    payload = {
        "model": "gpt-4o",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{b64}",
                    "detail": "high"
                }}
            ]
        }],
        "temperature": 0,
        "max_tokens": 3000,
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
    start = content.find("[")
    end   = content.rfind("]") + 1
    if start == -1:
        return []
    return json.loads(content[start:end])


# ── date helper ───────────────────────────────────────────────────────────────

def _fix_date(date_str: str, leaflet_date: str) -> str:
    """Fix vision-OCR date errors: wrong year → current year; empty → leaflet date."""
    if not date_str:
        return leaflet_date
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", date_str)
    if not m:
        return leaflet_date
    day, month, year = m.group(1), m.group(2), int(m.group(3))
    if year < 2024:
        date_str = f"{day}.{month}.{datetime.now(timezone.utc).year}"
    return date_str


# ── scraping ──────────────────────────────────────────────────────────────────

def _build_offer(o: dict, offer_id: str, retailer: str,
                 valid_from: str, valid_until: str,
                 img_bytes: bytes | None) -> dict:
    """Convert a raw vision offer dict to the standard offer schema."""
    image_url = ""
    if img_bytes and o.get("bbox"):
        bbox = o["bbox"]
        if isinstance(bbox, list) and len(bbox) == 4:
            image_url = _crop_and_save(img_bytes, bbox, offer_id)
    return {
        "id":             offer_id,
        "title":          o.get("title", "").strip(),
        "description":    o.get("description") or "",
        "brand":          o.get("brand") or "",
        "retailer":       retailer,
        "retailer_id":    "",
        "industry":       "",  # set by combined_crawler
        "category":       "",
        "price":          o.get("price") or "",
        "price_value":    float(o.get("price_value") or 0),
        "original_price": o.get("original_price") or "",
        "price_per_unit": "",
        "discount_pct":   o.get("discount_pct"),
        "valid_from":     _fix_date(o.get("valid_from", ""), valid_from),
        "valid_until":    _fix_date(o.get("valid_until", ""), valid_until),
        "image_url":      image_url,
        "has_image":      bool(image_url),
        "source":         "vision",
    }


def _scrape_leaflet(leaflet_id: str, retailer: str,
                    valid_from: str, valid_until: str) -> list[dict]:
    """Scrape all pages of one marktguru leaflet, return list of offers."""
    if not AICORE_CLIENT_ID or not AICORE_CLIENT_SECRET or not AICORE_DEPLOYMENT_ID:
        return []

    token = _get_token()

    # Count pages via binary search on CDN
    r0 = requests.head(_page_url(leaflet_id, 0), headers=HEADERS, timeout=10)
    if r0.status_code != 200:
        return []
    lo, hi = 1, 80
    while lo < hi:
        mid = (lo + hi + 1) // 2
        r = requests.head(_page_url(leaflet_id, mid), headers=HEADERS, timeout=10)
        lo = mid if r.status_code == 200 else (hi := mid - 1) or lo
    page_count = lo + 1

    print(f"    Scraping {retailer} leaflet {leaflet_id} ({page_count} pages)…", file=sys.stderr)

    # Fetch page images in parallel
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_mg_page_bytes, leaflet_id, p): p for p in range(page_count)}
        page_images: dict[int, bytes] = {}
        for future in as_completed(futures):
            page = futures[future]
            b = future.result()
            if b:
                page_images[page] = b

    all_offers: list[dict] = []
    seen_titles: set[str] = set()
    for page in sorted(page_images.keys()):
        try:
            raw_offers = _analyze_page(page_images[page], retailer, token)
            for o in raw_offers:
                title = o.get("title", "").strip()
                if not title or title.lower() in seen_titles:
                    continue
                seen_titles.add(title.lower())
                oid = f"vis_{leaflet_id}_{re.sub(r'[^a-z0-9]', '_', title.lower())[:40]}"
                all_offers.append(_build_offer(o, oid, retailer, valid_from, valid_until, page_images[page]))
        except Exception as e:
            print(f"    Page {page} error: {e}", file=sys.stderr)

    print(f"    {retailer}: extracted {len(all_offers)} offers "
          f"({sum(1 for o in all_offers if o['has_image'])} with images)", file=sys.stderr)
    return all_offers


def _scrape_pm_leaflet(brochure_id: str, detail_url: str, retailer: str,
                       valid_from: str, valid_until: str) -> list[dict]:
    """Scrape a prospektmaschine brochure via Vision AI."""
    if not AICORE_CLIENT_ID or not AICORE_CLIENT_SECRET or not AICORE_DEPLOYMENT_ID:
        return []

    token = _get_token()
    page_urls = _fetch_pm_page_images(brochure_id, detail_url)
    if not page_urls:
        print(f"    {retailer} (pm_{brochure_id}): no pages found", file=sys.stderr)
        return []

    print(f"    Scraping {retailer} (prospektmaschine {brochure_id}, {len(page_urls)} pages)…",
          file=sys.stderr)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_page_bytes, url): i for i, url in enumerate(page_urls)}
        page_images: dict[int, bytes] = {}
        for future in as_completed(futures):
            page = futures[future]
            b = future.result()
            if b:
                page_images[page] = b

    all_offers: list[dict] = []
    seen_titles: set[str] = set()
    for page in sorted(page_images.keys()):
        try:
            raw_offers = _analyze_page(page_images[page], retailer, token)
            for o in raw_offers:
                title = o.get("title", "").strip()
                if not title or title.lower() in seen_titles:
                    continue
                seen_titles.add(title.lower())
                oid = f"vis_pm{brochure_id}_{re.sub(r'[^a-z0-9]', '_', title.lower())[:40]}"
                all_offers.append(_build_offer(o, oid, retailer, valid_from, valid_until, page_images[page]))
        except Exception as e:
            print(f"    Page {page} error: {e}", file=sys.stderr)

    print(f"    {retailer}: extracted {len(all_offers)} offers "
          f"({sum(1 for o in all_offers if o['has_image'])} with images)", file=sys.stderr)
    return all_offers


# ── main entry point ──────────────────────────────────────────────────────────

def scrape_missing_leaflets(leaflets: list[dict]) -> list[dict]:
    """
    For each leaflet from a relevant retailer with few indexed offers,
    scrape via Vision AI. Also fetches dm from prospektmaschine.de.
    Uses cache keyed by leaflet_id. Returns list of offers.
    """
    if not AICORE_CLIENT_ID or not AICORE_CLIENT_SECRET or not AICORE_DEPLOYMENT_ID:
        print("  AI Core credentials not set — skipping vision scraping", file=sys.stderr)
        return []

    cache: dict = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    mg_to_scrape = [
        l for l in leaflets
        if l.get("retailer") not in _IRRELEVANT_RETAILERS
        and l.get("offer_count", 0) < _SCRAPE_THRESHOLD
        and not str(l.get("leaflet_id", "")).startswith("kd_")
        and str(l.get("leaflet_id", "")) not in cache
    ]

    pm_leaflets: list[dict] = []
    if "dm-drogerie markt" not in {l.get("retailer") for l in leaflets}:
        pm_leaflets = _fetch_pm_leaflets()
        pm_leaflets = [l for l in pm_leaflets if l["leaflet_id"] not in cache]

    to_scrape_all = mg_to_scrape + pm_leaflets

    if to_scrape_all:
        print(f"  Vision scraping {len(to_scrape_all)} leaflets: "
              f"{[l['retailer'] for l in to_scrape_all]}", file=sys.stderr)

    for l in mg_to_scrape:
        lid = str(l["leaflet_id"])
        offers = _scrape_leaflet(lid, l["retailer"], l.get("valid_from", ""), l.get("valid_until", ""))
        cache[lid] = {
            "retailer":   l["retailer"],
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "offers":     offers,
        }

    for l in pm_leaflets:
        lid = l["leaflet_id"]
        offers = _scrape_pm_leaflet(
            l["_pm_brochure_id"], l["_pm_detail_url"],
            l["retailer"], l.get("valid_from", ""), l.get("valid_until", "")
        )
        cache[lid] = {
            "retailer":   l["retailer"],
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "offers":     offers,
        }

    if to_scrape_all:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  Vision cache saved ({len(cache)} leaflets)", file=sys.stderr)
    else:
        n_relevant = len([l for l in leaflets if l.get("retailer") not in _IRRELEVANT_RETAILERS])
        print(f"  Vision scraping: all {n_relevant} relevant leaflets already cached", file=sys.stderr)

    all_offers: list[dict] = []
    for entry in cache.values():
        all_offers.extend(entry.get("offers", []))
    return all_offers


if __name__ == "__main__":
    test_leaflets = [{
        "leaflet_id": "5295412",
        "retailer": "Kaufland",
        "offer_count": 0,
        "valid_from": "10.05.2026",
        "valid_until": "13.05.2026",
    }]
    print("Testing vision scraper on Kaufland…")
    offers = scrape_missing_leaflets(test_leaflets)
    print(f"\nExtracted {len(offers)} offers ({sum(1 for o in offers if o['has_image'])} with images)")
    for o in offers[:10]:
        print(f"  {o['price']:10} {'[img]' if o['has_image'] else '     '} {o['title']}")
