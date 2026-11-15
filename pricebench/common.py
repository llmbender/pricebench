"""Shared configuration, the frozen PROTOCOL, task loading, and utilities."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OUT_DIR = ROOT / "runs" / os.environ.get("PRICEBENCH_DIR", "run1")
TASKS_PATH = OUT_DIR / "tasks.json"

TASK_LIMIT: int | None = None
TASK_RANGE: tuple[int, int] | None = None


def load_tasks() -> list[dict]:
    tasks = json.loads(TASKS_PATH.read_text())
    if TASK_RANGE:
        return tasks[TASK_RANGE[0] - 1:TASK_RANGE[1]]
    return tasks[:TASK_LIMIT] if TASK_LIMIT else tasks

FINDCHEAP_API = os.environ.get("FINDCHEAP_API", "https://api.findcheap.ai")
FINDCHEAP_URL = f"{FINDCHEAP_API}/search"
FINDCHEAP_KEY = os.environ.get("FINDCHEAP_API_KEY", "")
CLAUDE_MODEL = "claude-opus-5"
OPENAI_MODEL = "gpt-5.6-sol"
GEMINI_AGENT_MODEL = os.environ.get("GEMINI_AGENT_MODEL", "gemini-3.1-pro-preview")
SYSTEMS = ("findcheap", "claude", "openai", "gemini", "perplexity")
# What "all" expands to. SYSTEMS may grow R&D adapters; only this lineup
# ever runs implicitly.
RUN_ALL_LINEUP = ("findcheap", "claude", "openai", "gemini", "perplexity")
TRACKS = ("exact", "equiv")

PRICING = {
    "claude": {"in_per_mtok": 5.00, "out_per_mtok": 25.00, "search_per_1k": 10.00},
    "openai": {"in_per_mtok": 4.00, "out_per_mtok": 20.00, "search_per_1k": 10.00},
    "gemini": {"in_per_mtok": 2.00, "out_per_mtok": 12.00, "search_per_1k": 14.00},
    "findcheap": {"per_search": float(os.environ.get("FINDCHEAP_COST_PER_SEARCH", "0.03"))},
}


def token_cost(system: str, tokens_in: int | None, tokens_out: int | None,
               searches: int = 0, token_rate_factor: float = 1.0) -> float:
    # token_rate_factor: 0.5 for Batches API transport (tokens bill at half
    # rate; per-search fees are priced the same as interactive per docs).
    p = PRICING[system]
    return round(((tokens_in or 0) / 1e6 * p["in_per_mtok"]
                  + (tokens_out or 0) / 1e6 * p["out_per_mtok"]) * token_rate_factor
                 + searches / 1000 * p["search_per_1k"], 4)

JUDGE_PANEL = {
    "claude": "claude-opus-5",
    "openai": "gpt-5.6-sol",
    "gemini": os.environ.get("GEMINI_JUDGE_MODEL", "gemini-3.5-flash"),
}

PROTOCOL = {
    "output_cap": "up to 10 listings per task (extras truncated)",
    "invocations": ("one invocation per task, no agent re-attempts; a single "
                    "transport-level retry is allowed only when the vendor API "
                    "5xxs before producing any output"),
    "reasoning_effort": "provider defaults; no effort/verbosity overrides",
    "tool_config": ("claude: web_search_20260209 + web_fetch_20260209, max_uses 12 each, "
                    "pause_turn continuation cap 8; openai: web_search; "
                    "gemini: google_search grounding; "
                    "perplexity: Agent API built-in search, preset=high, "
                    "real image input (normalized base64 data URI); "
                    "findcheap: /search as shipped (exact track: exact_brand=True)"),
    "image_transport": ("product image attached as base64 of the "
                        "harness-normalized image (retailer CDNs bot-wall "
                        "URL fetchers)"),
    "max_execution": "findcheap 360s HTTP timeout; baselines 600s HTTP timeout per call",
    "geography": ("US retailers, US-shippable; buyer location for "
                  "calculated shipping pinned to ZIP 10001 (a generic "
                  "major-metro US ZIP with no personal association; manual "
                  "checks enter the same ZIP in retailer shipping "
                  "estimators; calculated shipping varies by destination, "
                  "disclosed in limitations)"),
    "currency": "USD",
    "shipping_basis": ("non-Prime / no membership programs (2026-08-29 "
                       "ruling, consistent with the no-membership-prices "
                       "rule): Amazon >= $35 ships free, under $35 resolved by "
                       "manual logged-out checks at ZIP 10001 in the pre-run "
                       "reference-shipping pass, which pins every reference's "
                       "non-deterministic shipping BEFORE searches; a Prime "
                       "counterfactual (all Amazon shipping $0, ~75% of US "
                       "Amazon shoppers have Prime) is reported separately"),
    "transport": ("the claude searcher column runs through the Message Batches "
                  "API (2026-08-28) — identical model/tools/prompts, "
                  "tokens billed at 50% batch rates (per-search fees unchanged); "
                  "chosen over halving that column's N, which would have thinned "
                  "the pooled oracle on the skipped tasks and broken column "
                  "comparability. Its per-task latency is not measurable under "
                  "batch scheduling (latency_s=null; the rehearsal's interactive "
                  "medians are the cited latency figures). The batch analog of "
                  "the single transport-level retry re-submits errored/expired "
                  "batch results once. All other columns are interactive HTTP"),
    "pack_normalization": ("multi-pack references (2026-08-26): candidates with "
                           "different pack counts qualify on the equivalence "
                           "track and are scored on landed price PER UNIT — "
                           "(item + mandatory shipping) / pack count — matching "
                           "production's price-per-count display; the exact "
                           "track still requires the same count"),
    "shipping_policies": ("hard-coded published thresholds (2026-08-26): "
                          "Amazon/Walmart/Target/Best Buy free >= $35 (under: "
                          "Walmart $6.99, Target $5.99, Amazon/Best Buy vary); "
                          "Kohl's free >= $49 else $8.95; Macy's free >= $49; "
                          "Chewy free >= $49 else $4.99; Shein free >= $29; "
                          "Sephora free >= $50; Apple and Nordstrom always free"),
    "shipping": ("LANDED COST (2026-08-25): 'cheaper' = item price + mandatory "
                 "shipping for a single-item order to a US address, no paid "
                 "memberships. Shipping resolves via the locked retailer policy "
                 "table, then human verification; "
                 "agent-reported shipping used elsewhere; unknown shipping "
                 "resolved at the price frontier (R&D: human audit; final run: "
                 "automated page-text extraction). The REFERENCE is landed too: "
                 "origin page's item price + its own mandatory shipping under "
                 "the same rules."),
    "discounts": ("no coupons, promo codes, membership-gated prices, or "
                  "first-order/new-customer WELCOME prices (AliExpress/"
                  "Temu/Shein-style) — the basis is the standing price "
                  "available to any shopper"),
    "condition": "new only (no used/refurbished/open-box)",
    "marketplace_sellers": "allowed (Amazon/Walmart/eBay third-party sellers count)",
    "findcheap_prices": ("Findcheap item prices are vendor-API sourced and treated "
                         "as verified; its shipping and "
                         "stock are audited like everyone else's"),
}

JS_PRICE_HOSTS = ("aliexpress.", "shein.", "temu.")


# ------------------------------------------------------------ utilities


def extract_json(text: str) -> dict | None:
    """Pull the last {"results": ...} object out of possibly-chatty model
    text: whole string, then ```json fences, then balanced-brace scan."""
    for candidate in (text, *re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)):
        try:
            obj = json.loads(candidate.strip())
            if isinstance(obj, dict) and "results" in obj:
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    best = None
    for start in [m.start() for m in re.finditer(r"\{", text)]:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start : i + 1]
                    if '"results"' in chunk:
                        try:
                            best = json.loads(chunk)
                        except json.JSONDecodeError:
                            pass
                    break
    return best if isinstance(best, dict) and "results" in best else None


def normalize_results(raw: list, source: str) -> list[dict]:
    out = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        try:
            price = float(str(r.get("price")).replace("$", "").replace(",", ""))
        except (TypeError, ValueError):
            continue
        try:
            shipping = float(str(r.get("shipping")).replace("$", "").replace(",", ""))
        except (TypeError, ValueError):
            shipping = None
        try:
            pack_count = int(r.get("pack_count")) if r.get("pack_count") else None
        except (TypeError, ValueError):
            pack_count = None
        out.append({
            "title": str(r.get("title") or "")[:300],
            "price": price,
            "shipping": shipping,
            "pack_count": pack_count,
            "currency": r.get("currency") or "USD",
            "url": r.get("url"),
            # some findcheap results carry a short-lived resolve_id instead
            # of a URL — keep it; run_findcheap resolves it right away.
            "resolve_id": r.get("resolve_id"),
            "retailer": r.get("retailer") or r.get("marketplace") or "?",
            "image": r.get("image"),
            "claimed_same": r.get("same_product"),
            "source": source,
        })
    return out[:10]


def save(name: str, obj) -> None:
    (OUT_DIR / name).write_text(json.dumps(obj, indent=1))
    print(f"  wrote {OUT_DIR / name}")


def load(name: str):
    return json.loads((OUT_DIR / name).read_text())


def fetch_image_b64(url: str) -> str | None:
    from curl_cffi import requests as curl_requests
    from PIL import Image

    try:
        r = curl_requests.get(url, impersonate="chrome", timeout=20)
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        img.thumbnail((480, 480))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return base64.standard_b64encode(buf.getvalue()).decode()
    except Exception:
        return None


GEMINI_MIN_INTERVAL_S = float(os.environ.get("GEMINI_MIN_INTERVAL_S", "6"))
_gemini_gate = {"lock": threading.Lock(), "next_at": 0.0}


def _gemini_throttle() -> None:
    while True:
        with _gemini_gate["lock"]:
            wait = _gemini_gate["next_at"] - time.time()
            if wait <= 0:
                _gemini_gate["next_at"] = time.time() + GEMINI_MIN_INTERVAL_S
                return
        time.sleep(min(wait, 1.0))
