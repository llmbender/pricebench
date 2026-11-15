"""PriceBench task collector — mechanical sourcing per the "Task sourcing" section of README.md.

Fetches a cell's source list (Firecrawl, JS-rendered), extracts the
ranked product list (LLM pass, order-preserving, price-blind), applies
the every-3rd rule + pre-declared exclusions in code, then freezes the
selected product from ITS OWN structured data (Findcheap /extract, else
the page's JSON-LD Product / OpenGraph) — no hand-typed values.
Provenance for every step lands in sourcing_log.json.

Usage:
    python source_tasks.py --cell 1        # source one grid cell
    python source_tasks.py --all           # batch-walk every cell (resume-safe)
    python source_tasks.py --range 5-20    # batch-walk cells 5..20
    python source_tasks.py --list-url URL  # debug: show extracted ranking
    python source_tasks.py --freeze URL --cell N  # freeze a specific product URL

Cells are configured in CELLS below (the retailer x category grid); a
cells.json in the run directory overrides it — the full-run grid is
data, not code. The selected tasks accumulate in
runs/$PRICEBENCH_DIR/tasks.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

OUT_DIR = ROOT / "runs" / os.environ.get("PRICEBENCH_DIR", "run1")
TASKS_PATH = OUT_DIR / "tasks.json"
LOG_PATH = OUT_DIR / "sourcing_log.json"

EXTRACT_MODEL = "gemini-3.5-flash"  # list extraction; cheap, order-preserving

CELLS = {
    1: {"retailer": "amazon", "category": "beauty",
        "list_url": "https://www.amazon.com/Best-Sellers-Beauty/zgbs/beauty"},
    2: {"retailer": "amazon", "category": "toys-games",
        "list_url": "https://www.amazon.com/Best-Sellers-Toys-Games/zgbs/toys-and-games"},
    3: {"retailer": "amazon", "category": "home-kitchen",
        "list_url": "https://www.amazon.com/Best-Sellers-Home-Kitchen/zgbs/home-garden"},
    4: {"retailer": "amazon", "category": "office",
        "list_url": "https://www.amazon.com/Best-Sellers-Office-Products/zgbs/office-products"},
    5: {"retailer": "walmart", "category": "grocery-consumables",
        "list_url": "https://www.walmart.com/browse/food/976759?sort=best_seller"},
    6: {"retailer": "apple", "category": "electronics",
        "list_url": "https://www.apple.com/shop/accessories/all"},
    7: {"retailer": "dtc-head", "category": "fashion-apparel", "brand_site": "gap.com",
        "list_url": "https://www.gap.com/browse/women/bestsellers"},
    8: {"retailer": "dtc-head", "category": "fashion-footwear", "brand_site": "nike.com",
        "list_url": "https://www.nike.com/w/best-shoes-76m50zy7ok"},
    9: {"retailer": "dtc-body", "category": "grocery-consumable-dtc", "brand_site": "orgain.com",
        "list_url": "https://orgain.com/collections/best-sellers"},
    10: {"retailer": "ebay", "category": "pet-supplies",
         "list_url": "https://www.ebay.com/b/Pets-Dog-Supplies/20742/bn_16366183"},
}

# A cells.json in OUT_DIR overrides the hardcoded example grid: the
# full-run grid is data, not code. Shape: {"label_prefix": "n100_",
# "cells": {"1": {"retailer": ..., "category": ..., "list_url": ...}, ...}}
LABEL_PREFIX = "task"
LABEL_DIGITS = 2
_cells_file = OUT_DIR / "cells.json"
if _cells_file.exists():
    _cfg = json.loads(_cells_file.read_text())
    CELLS = {int(k): v for k, v in _cfg["cells"].items()}
    LABEL_PREFIX = _cfg.get("label_prefix", LABEL_PREFIX)
    LABEL_DIGITS = 3 if len(CELLS) > 99 else 2

TRACKING_PARAMS = re.compile(
    r"^(utm_|mkevt|mkcid|mkrid|campid|toolid|customid|ref_?|rrec|mlink|clink|tag|linkCode)", re.I)


def clean_url(url: str) -> str:
    """Strip TRACKING params only — product identity often lives in the
    query string (e.g. Gap pid=...)."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not TRACKING_PARAMS.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


EXCLUDE_TERMS = re.compile(
    r"gift card|e-?gift|subscription|digital (code|download)|membership"
    # references must be NEW — the protocol is new-only, and some browse
    # lists (eBay) surface refurbished items the original terms predate
    r"|refurbished|renewed|certified refurb|pre-?owned|open box|\bused\b", re.I)


# ------------------------------------------------------------ fetching

def firecrawl(url: str, fmt: str = "markdown", wait_ms: int = 4000) -> str:
    # PRICEBENCH_FC_PROXY=stealth (+ PRICEBENCH_FC_WAIT_MS) for the
    # bot-walled-cell retry sweep; default matches the main batch.
    proxy = os.environ.get("PRICEBENCH_FC_PROXY", "auto")
    wait_ms = int(os.environ.get("PRICEBENCH_FC_WAIT_MS", wait_ms))
    r = httpx.post(
        "https://api.firecrawl.dev/v2/scrape",
        headers={"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}",
                 "Content-Type": "application/json"},
        json={"url": url, "formats": [fmt], "proxy": proxy, "waitFor": wait_ms},
        timeout=180.0 if proxy == "stealth" else 90.0)
    r.raise_for_status()
    return ((r.json().get("data") or {}).get(fmt) or "").strip()


# ------------------------------------------------------------ list extraction

LIST_PROMPT = """\
Below is the rendered content of a retail best-sellers / product-list page.
Extract the FIRST 30 products IN THE ORDER SHOWN (stop after 30). For each
give its display title and its absolute product-page URL (resolve relative
links against {base}). Skip navigation, ads, and category tiles — products
only. Do NOT reorder, filter, or editorialize.
Return ONLY JSON: {{"items": [{{"rank": 1, "title": "...", "url": "https://..."}}, ...]}}

PAGE CONTENT:
{content}"""


def extract_ranked_items(url: str, content: str) -> list[dict]:
    base = "/".join(url.split("/")[:3])
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{EXTRACT_MODEL}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        json={"contents": [{"parts": [{"text": LIST_PROMPT.format(
            base=base, content=content[:160000])}]}],
            "generationConfig": {"maxOutputTokens": 16000}},
        timeout=120.0)
    r.raise_for_status()
    text = " ".join(p.get("text", "") for c in r.json().get("candidates", [])
                    for p in c.get("content", {}).get("parts", []))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    blob = m.group(0)
    try:
        items = json.loads(blob)["items"]
    except json.JSONDecodeError:
        # truncated output: salvage up to the last complete item object
        cut = blob.rfind("},")
        if cut == -1:
            return []
        try:
            items = json.loads(blob[:cut + 1] + "]}")["items"]
        except json.JSONDecodeError:
            return []
    return [i for i in items if i.get("url") and i.get("title")]


# ------------------------------------------------------------ product freeze

def parse_structured(html: str) -> dict:
    """Structured-data read of a product page: JSON-LD Product first,
    OpenGraph fallback — never hand-typed values."""
    out = {}
    for m in re.finditer(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else \
            data.get("@graph", [data]) if isinstance(data, dict) else []
        # ProductGroup pages (Nike et al.) nest priced Products in hasVariant
        for node in list(nodes):
            if isinstance(node, dict) and node.get("hasVariant"):
                nodes = list(nodes) + [v for v in node["hasVariant"] if isinstance(v, dict)]
        for node in nodes:
            if not isinstance(node, dict) or "Product" not in str(node.get("@type", "")):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price") or offers.get("lowPrice")
            img = node.get("image")
            if isinstance(img, list):
                img = img[0] if img else None
            if isinstance(img, dict):
                img = img.get("url")
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            out = {"title": node.get("name"), "image": img,
                   "price": float(price) if price else None,
                   "currency": offers.get("priceCurrency") or "USD",
                   "brand": brand, "source": "json-ld"}
            if out["title"] and out["price"] is not None:
                if not out["image"]:
                    # some storefronts omit image from the Product node;
                    # og:image is still structured data (no hand-typed values)
                    m2 = re.search(
                        r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
                    if m2:
                        out["image"] = m2.group(1)
                        out["source"] = "json-ld+og-image"
                return out
    og = {}
    for prop in ("og:title", "og:image", "product:price:amount",
                 "product:price:currency", "og:site_name"):
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)', html)
        if m:
            og[prop] = m.group(1)
    if og.get("og:title") and og.get("product:price:amount"):
        return {"title": og["og:title"], "image": og.get("og:image"),
                "price": float(og["product:price:amount"]),
                "currency": og.get("product:price:currency") or "USD",
                "brand": og.get("og:site_name"), "source": "opengraph"}
    return out or {"source": "none"}


def freeze_product(url: str, cell: int, cfg: dict, rank: int | None = None) -> dict | None:
    """Freeze via the Findcheap /extract endpoint; when it fails or the
    page resists it, fall back to the page's own structured data
    (JSON-LD Product, else OpenGraph) from a rendered fetch."""
    data = {}
    try:
        r = httpx.post(f"{os.environ.get('FINDCHEAP_API', 'https://api.findcheap.ai')}/extract",
                       json={"url": url},
                       headers={"Authorization": f"Bearer {os.environ.get('FINDCHEAP_API_KEY', '')}"},
                       timeout=60.0)
        d = r.json() if r.status_code == 200 else {}
        if d.get("is_product_page"):
            data = {"title": d.get("title"), "image": d.get("image"),
                    "price": d.get("price"), "currency": d.get("currency") or "USD",
                    "brand": d.get("brand"), "pack_count": d.get("pack_count"),
                    "source": "findcheap-extract"}
    except (httpx.HTTPError, ValueError):
        data = {}
    if not data.get("title") or data.get("price") is None or not data.get("image"):
        try:
            data = parse_structured(firecrawl(url, fmt="rawHtml"))
        except httpx.HTTPError:
            data = {"source": "none"}
    if not data.get("title") or data.get("price") is None or not data.get("image"):
        return None  # extraction failed (image required too) → rule-5 skip
    label = f"{cell_stub(cell)}{re.sub(r'[^a-z0-9]+', '-', data['title'].lower())[:40].strip('-')}"
    return {
        "label": label,
        "category": cfg["category"],
        "retailer_cell": cfg["retailer"],
        "source_rank": rank,
        "ref_price_asof": time.strftime("%Y-%m-%d"),
        "title": data["title"][:300],
        "image": data.get("image"),
        "url": clean_url(url),
        "price": data["price"],
        "currency": data.get("currency") or "USD",
        "brand": data.get("brand"),
        "pack_count": data.get("pack_count") or 1,  # review adjusts where the freeze missed it
        "extraction": data["source"],
    }


# ------------------------------------------------------------ cell walk

def cell_stub(cell: int) -> str:
    """Label prefix for one cell, e.g. 'task03__' or 'n100_017__'."""
    return f"{LABEL_PREFIX}{cell:0{LABEL_DIGITS}d}__"


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def seated_brands() -> set:
    tasks = load_json(TASKS_PATH, [])
    return {str(t.get("brand") or "").lower() for t in tasks if t.get("brand")}


_LIST_CACHE: dict[str, list[dict]] = {}  # url -> ranked items (cells sharing a list reuse one fetch)


def source_cell(cell: int) -> str:
    """Walk one cell. Returns 'selected' | 'empty-list' | 'exhausted'."""
    cfg = CELLS[cell]
    if not cfg["list_url"]:
        raise SystemExit(f"cell {cell}: list_url not set — run the roster walk first "
                         f"and record the chosen brand's list URL in CELLS")
    log = load_json(LOG_PATH, {})
    entry = {"cell": cell, **cfg, "accessed": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "walk": [], "selected": None}
    if cfg["list_url"] in _LIST_CACHE:
        items = _LIST_CACHE[cfg["list_url"]]
        print(f"[cell {cell}] list cached: {cfg['list_url']}")
    else:
        print(f"[cell {cell}] fetching list: {cfg['list_url']}")
        content = firecrawl(cfg["list_url"])
        items = extract_ranked_items(cfg["list_url"], content)
        if items:
            _LIST_CACHE[cfg["list_url"]] = items
    print(f"[cell {cell}] {len(items)} ranked items extracted")
    entry["items_seen"] = [{"rank": i.get("rank", n + 1), "title": i["title"][:80]}
                           for n, i in enumerate(items)]
    taken = seated_brands()
    # URL-level dedup: brand-less items bypass the brand seat, so cells
    # sharing a list must also never re-select the same product.
    taken_urls = {t.get("url") for t in load_json(TASKS_PATH, [])}
    # every 3rd item: indices 2, 5, 8, ... (ranks 3, 6, 9, ...)
    for idx in range(2, len(items), 3):
        it = items[idx]
        step = {"rank": idx + 1, "title": it["title"][:80], "url": it["url"]}
        if EXCLUDE_TERMS.search(it["title"]):
            step["skip"] = "excluded term (gift card/digital/subscription/not-new)"
            entry["walk"].append(step); print(f"  skip r{idx+1}: excluded term"); continue
        if clean_url(it["url"]) in taken_urls:
            step["skip"] = "product already selected (url dup)"
            entry["walk"].append(step); print(f"  skip r{idx+1}: url dup"); continue
        task = freeze_product(it["url"], cell, cfg, rank=idx + 1)
        if task is None:
            step["skip"] = "not mechanically extractable"
            entry["walk"].append(step); print(f"  skip r{idx+1}: no structured data"); continue
        if str(task.get("brand") or "").lower() in taken:
            step["skip"] = f"brand already seated ({task['brand']})"
            entry["walk"].append(step); print(f"  skip r{idx+1}: brand dup"); continue
        entry["walk"].append(step)
        entry["selected"] = task
        tasks = load_json(TASKS_PATH, [])
        tasks = [t for t in tasks if not str(t["label"]).startswith(cell_stub(cell))]
        tasks.append(task)
        tasks.sort(key=lambda t: t["label"])
        TASKS_PATH.write_text(json.dumps(tasks, indent=1))
        print(f"[cell {cell}] SELECTED r{idx+1}: {task['title'][:70]} — "
              f"${task['price']} ({task['extraction']})")
        break
    else:
        print(f"[cell {cell}] NO qualifying item — extend the list or check extraction")
    log[str(cell)] = entry
    LOG_PATH.write_text(json.dumps(log, indent=1))
    if entry["selected"]:
        return "selected"
    return "empty-list" if not items else "exhausted"


def source_batch(cells: list[int]) -> None:
    """Babysat batch walk: resume-safe (selected cells skipped), one retry
    on transient failure (exception / empty extraction), failures summarized
    at the end for the manual follow-up batch."""
    done = {t["label"][:len(cell_stub(0))] for t in load_json(TASKS_PATH, [])}
    failures = []
    for cell in cells:
        if cell_stub(cell) in done:
            print(f"[cell {cell}] already selected — skip")
            continue
        status = None
        for attempt in (1, 2):
            try:
                status = source_cell(cell)
            except Exception as e:  # vendor/Firecrawl hiccup — retry once
                status = f"error: {e}"
                print(f"[cell {cell}] attempt {attempt} failed: {e}")
            if status == "selected":
                break
            if status == "exhausted":
                break  # real mechanical outcome, retrying won't change it
            _LIST_CACHE.pop(CELLS[cell]["list_url"], None)
            if attempt == 1:
                time.sleep(15)
        if status != "selected":
            failures.append((cell, status))
        time.sleep(2)
    n_done = len(load_json(TASKS_PATH, []))
    print(f"\n[batch] {n_done}/{len(CELLS)} cells selected")
    if failures:
        print("[batch] FAILED cells (manual follow-up):")
        for cell, status in failures:
            print(f"  cell {cell}: {status} — {CELLS[cell]['retailer']}/"
                  f"{CELLS[cell]['category']} {CELLS[cell]['list_url']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cell", type=int)
    ap.add_argument("--list-url", help="debug: extract + print a ranking")
    ap.add_argument("--freeze", metavar="URL", help="freeze one product URL into --cell")
    ap.add_argument("--all", action="store_true", help="batch-walk every cell (resume-safe)")
    ap.add_argument("--range", metavar="A-B", help="batch-walk cells A..B inclusive")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.list_url:
        content = firecrawl(args.list_url)
        for it in extract_ranked_items(args.list_url, content)[:30]:
            print(f"{it.get('rank', '?'):>3}  {it['title'][:70]}  {it['url'][:80]}")
        return
    if args.freeze and args.cell:
        task = freeze_product(args.freeze, args.cell, CELLS[args.cell])
        print(json.dumps(task, indent=1))
        return
    if args.all:
        source_batch(sorted(CELLS))
        return
    if args.range:
        a, b = (int(x) for x in args.range.split("-"))
        source_batch([c for c in sorted(CELLS) if a <= c <= b])
        return
    if args.cell:
        source_cell(args.cell)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
