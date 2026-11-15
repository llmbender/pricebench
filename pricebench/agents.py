"""Agent adapters: the shared prompt, per-agent API clients, and the run loop."""

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

from .common import (OUT_DIR, PROTOCOL, FINDCHEAP_API, FINDCHEAP_URL, FINDCHEAP_KEY,
                     CLAUDE_MODEL, OPENAI_MODEL, GEMINI_AGENT_MODEL,
                     PRICING, token_cost,
                     extract_json, normalize_results, fetch_image_b64,
                     _gemini_throttle, save, load)


# ------------------------------------------------------------ prompts

AGENT_SYSTEM_SHARED = """\
You are a shopping agent inside a price-comparison product. The user is \
viewing a product page. {mission}

Rules:
- Use web search (and page fetches if available) to find real, currently \
purchasable NEW listings from US retailers. Used, refurbished, and \
for-parts listings do NOT qualify; open-box or damaged-packaging new \
items are acceptable.
- "Cheaper" means TOTAL cost: the item price plus any mandatory shipping \
for a single-item order to a US address, with no paid memberships. A \
listing whose shipping fee erases the savings does not qualify.
- Every result must be a direct product-page URL, not a search-results or \
category page.
- Only return listings that are IN STOCK and purchasable right now — check \
the page; out-of-stock, unavailable, or discontinued listings do not count.
- "price" must be the standing purchase price on that page in USD — the price \
any shopper pays, NOT first-order/new-customer "welcome" prices, coupon prices, \
or membership prices.
- "shipping" is that page's mandatory shipping cost in USD for a \
single-item order: 0 if it ships free, null if you could not determine it.
- "pack_count" is the number of identical units in the listing (1 for a \
single item, 12 for a 12-pack).
- Never return the exact page the user is already on.
- Up to 10 results, best matches and biggest savings first.

Respond with ONLY a JSON object, no prose before or after, exactly:
{{"results": [{{"title": "...", "price": 12.34, "shipping": 0, \
"pack_count": 1, "currency": "USD", "url": "https://...", \
"retailer": "...", "same_product": true}}]}}
same_product: true if the identical product (same brand, model/style, \
size/pack count), false if an equivalent alternative.
If nothing qualifying and cheaper can be found, return {{"results": []}}."""

MISSION = {
    "exact": (
        "Find where to buy the IDENTICAL product for a LOWER price than the "
        "user currently sees. Identical means: same brand, same model/style, "
        "same per-item size/capacity. A different colorway of the same model "
        "is acceptable — except for fashion/apparel/footwear products, where "
        "the color and design must match the product shown. For apparel or "
        "footwear a different garment/shoe size is acceptable. Pack/unit "
        "QUANTITY is NOT part of identity: a listing with a different pack or "
        "unit count of the identical item (including a multi-pack consisting "
        "only of that item) is a valid result — value is judged on price PER "
        "UNIT — (item price + mandatory shipping) divided by unit count — "
        "against the reference's per-unit price. Include such results "
        "whenever they win per unit, even if their total price is higher "
        "than the reference. Do NOT return similar or equivalent products, "
        "other models or lines from the same brand, different capacities, or "
        "mini/travel sizes — the item itself must be identical."
    ),
    "equiv": (
        "Find where to buy the SAME product — or a functionally EQUIVALENT "
        "substitute — for a LOWER price than the user currently sees. An "
        "equivalent substitute must genuinely serve the same purpose at a "
        "comparable quality and specification level; a cheap lookalike of "
        "materially lower quality does not count. For fashion/apparel/footwear "
        "products, appearance matters: an equivalent should be visually "
        "similar in overall look and style to the reference and the same "
        "general color (though not necessarily an exact match) — not merely "
        "the same type of garment. Listings with a DIFFERENT "
        "pack/unit count than the reference are valid results: value is "
        "judged on price PER UNIT — (item price + mandatory shipping) divided "
        "by pack count — against the reference's per-unit price. Include such "
        "results whenever they win per unit, even if their total price is "
        "higher than the reference."
    ),
}


def is_fashion(task: dict) -> bool:
    """Category-aware criteria: for fashion products,
    appearance IS a functional characteristic — color, style, and
    silhouette matter, for agents and judges alike."""
    return str(task.get("category", "")).startswith("fashion")


def agent_system(task: dict, track: str) -> str:
    return AGENT_SYSTEM_SHARED.format(mission=MISSION[track])


def agent_user_prompt(task: dict, track: str) -> str:
    return "\n".join([
        f"Product: {task['title']}",
        f"Brand: {task['brand']}",
        f"Product page: {task['url']}",
        f"Price the user sees: ${task['price']:.2f} {task['currency']}",
    ])


# ------------------------------------------------------------ adapters


def _resolve_findcheap_links(results: list[dict], headers: dict) -> None:
    """Some findcheap results carry a resolve_id instead of a direct URL.
    Resolve ids are SHORT-LIVED, so links must be resolved promptly after
    the search returns — ids left in saved artifacts cannot be resolved
    later. Non-fatal: a failed resolution leaves url null."""
    for r in results:
        rid = r.get("resolve_id")
        if not rid or r.get("url"):
            continue
        try:
            resp = httpx.post(f"{FINDCHEAP_API}/resolve", json={"id": rid},
                              headers=headers, timeout=30.0)
            if resp.status_code == 200 and resp.json().get("resolved"):
                r["url"] = resp.json().get("url")
        except Exception:
            pass


def run_findcheap(task: dict, track: str) -> dict:
    """exact track = Exact-brand mode ON (the product's own toggle), with
    the brand hint the extension's Tier-1 extractor would forward. equiv
    track = default mode, rubric-blind — the product as shipped."""
    payload = {
        "title": task["title"],
        "image": task["image"],
        "url": task["url"],
        "price": task["price"],
        "currency": task["currency"],
        "brand": task["brand"],
        "exact_brand": track == "exact",
    }
    # the hosted API authenticates via this header (matches the extension)
    headers = {"X-Findcheap-Key": FINDCHEAP_KEY} if FINDCHEAP_KEY else {}
    t0 = time.time()
    resp = httpx.post(FINDCHEAP_URL, json=payload, headers=headers, timeout=360.0)
    elapsed = time.time() - t0
    if resp.status_code == 422:
        # Production REFUSAL (e.g. no_brand on exact-brand searches, the
        # used-grocery gate) — a genuine zero-result outcome of the product
        # as shipped, NOT a transport error to retry.
        try:
            detail = str(resp.json().get("detail", ""))[:200]
        except Exception:
            detail = ""
        return {"results": [], "latency_s": round(elapsed, 1), "usage": None,
                "cost_usd": PRICING["findcheap"]["per_search"],
                "refused": detail or "422 refusal"}
    resp.raise_for_status()
    results = normalize_results(resp.json().get("results", []), "findcheap")
    _resolve_findcheap_links(results, headers)
    return {
        "results": results,
        "latency_s": round(elapsed, 1),
        "usage": None,
        "cost_usd": PRICING["findcheap"]["per_search"],
    }


def claude_image_block(task: dict) -> dict:
    """Base64 of the harness-normalized image, URL-source fallback —
    URL fetching times out on bot-walled retailer CDNs; same transport
    as the other adapters."""
    img = fetch_image_b64(task["image"])
    if img:
        return {"type": "image", "source": {"type": "base64",
                                            "media_type": "image/jpeg", "data": img}}
    return {"type": "image", "source": {"type": "url", "url": task["image"]}}


def run_claude(task: dict, track: str, model: str = CLAUDE_MODEL,
               source: str = "claude") -> dict:
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": agent_user_prompt(task, track)},
        claude_image_block(task),
    ]}]
    usage = {"input_tokens": 0, "output_tokens": 0}
    tools = {"web_search": 0, "web_fetch": 0, "search_errors": 0, "fetch_errors": 0}

    def _tally(blocks):
        for b in blocks:
            bt = getattr(b, "type", "")
            if bt == "server_tool_use":
                name = getattr(b, "name", "")
                if name in tools:
                    tools[name] += 1
            elif bt in ("web_search_tool_result", "web_fetch_tool_result"):
                c = getattr(b, "content", None)
                ctype = getattr(c, "type", "") if not isinstance(c, (list, dict)) else \
                    (c.get("type", "") if isinstance(c, dict) else "")
                if "error" in str(ctype):
                    tools["search_errors" if bt.startswith("web_search") else "fetch_errors"] += 1

    t0 = time.time()
    for _ in range(8):
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=agent_system(task, track),
            tools=[
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 12},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 12},
            ],
            messages=messages,
        )
        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        _tally(resp.content)
        if resp.stop_reason == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            continue
        break
    text = "".join(b.text for b in resp.content if b.type == "text")
    parsed = extract_json(text)
    return {
        "results": normalize_results((parsed or {}).get("results", []), source),
        "latency_s": round(time.time() - t0, 1),
        "usage": usage,
        "tool_stats": tools,
        # Errored searches aren't billed; web_fetch is free. Priced only
        # for the standard seat — model overrides have different rates.
        "cost_usd": token_cost("claude", usage["input_tokens"], usage["output_tokens"],
                               searches=max(0, tools["web_search"] - tools["search_errors"]))
                    if source == "claude" and model == CLAUDE_MODEL else None,
        "stop_reason": resp.stop_reason,
        "parse_error": parsed is None,
        "raw_text": text[-4000:],
    }


# ---- Claude batch transport ----------------------------------------------
# The claude searcher column runs through the Message Batches API:
# identical model/tools/prompts to run_claude, tokens at 50% batch rates
# (web-search calls priced the same as interactive). Per-task latency is
# meaningless under batch scheduling (latency_s=None) and completion is
# guaranteed only within 24h — submit right after the reference-price
# freeze; the poll loop warns when the column's wall-clock threatens the
# same-day window. CLAUDE_BATCH=0 forces the interactive path.
CLAUDE_BATCH = os.environ.get("CLAUDE_BATCH", "1") != "0"
CLAUDE_BATCH_POLL_S = 30
CLAUDE_BATCH_FRESHNESS_WARN_S = 3 * 3600


def run_claude_batch(tasks: list[dict], track: str) -> dict[str, dict]:
    """Whole-column batch analog of run_claude. Round 1 submits every task;
    each later round carries only pause_turn continuations (cap 8, matching
    the interactive loop) plus the protocol's single transport-level retry
    of errored/expired batch results. Returns {label: record} in the same
    shape run_system's threaded path produces."""
    import anthropic

    client = anthropic.Anthropic()
    t0 = time.time()
    state = {}
    for task in tasks:
        state[task["label"]] = {
            "task": task,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": agent_user_prompt(task, track)},
                claude_image_block(task),
            ]}],
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "tools": {"web_search": 0, "web_fetch": 0,
                      "search_errors": 0, "fetch_errors": 0},
            "retried": False,
            "final": None,   # terminal Message (stop_reason != pause_turn)
            "error": None,
        }

    pending = [t["label"] for t in tasks]
    try:
        _claude_batch_rounds(client, state, pending, track, t0)
    except Exception as e:
        # Transport-level failure (credit exhaustion, auth, network): stamp
        # the unresolved tasks and fall through so the raw file still saves
        # every completed round — never lose the column to one exception.
        print(f"  [claude/{track}] BATCH TRANSPORT FAILURE: {type(e).__name__}: {e}")
        for label, st in state.items():
            if st["final"] is None and st["error"] is None:
                st["error"] = f"batch transport failure: {type(e).__name__}: {e}"[:300]

    out = {}
    for label, st in state.items():
        msg = st["final"]
        if msg is None:
            out[label] = {"results": [], "latency_s": None, "transport": "batch",
                          "usage": st["usage"], "tool_stats": st["tools"],
                          "error": st["error"] or "no terminal batch result"}
            continue
        text = "".join(b.text for b in msg.content if b.type == "text")
        parsed = extract_json(text)
        out[label] = {
            "results": normalize_results((parsed or {}).get("results", []), "claude"),
            # Batch scheduling makes per-task latency meaningless; the
            # report cites the rehearsal's interactive medians instead.
            "latency_s": None,
            "transport": "batch",
            "usage": st["usage"],
            "tool_stats": st["tools"],
            "cost_usd": token_cost(
                "claude", st["usage"]["input_tokens"], st["usage"]["output_tokens"],
                searches=max(0, st["tools"]["web_search"] - st["tools"]["search_errors"]),
                token_rate_factor=0.5),
            "stop_reason": msg.stop_reason,
            "parse_error": parsed is None,
            "raw_text": text[-4000:],
        }
    return out


def _batch_tally(st: dict, blocks) -> None:
    """Per-task tool diagnostics for the batch path — same thin-results
    counters as the interactive run_claude."""
    for b in blocks:
        bt = getattr(b, "type", "")
        if bt == "server_tool_use":
            name = getattr(b, "name", "")
            if name in st["tools"]:
                st["tools"][name] += 1
        elif bt in ("web_search_tool_result", "web_fetch_tool_result"):
            c = getattr(b, "content", None)
            ctype = getattr(c, "type", "") if not isinstance(c, (list, dict)) else \
                (c.get("type", "") if isinstance(c, dict) else "")
            if "error" in str(ctype):
                st["tools"]["search_errors" if bt.startswith("web_search")
                            else "fetch_errors"] += 1


def _claude_batch_rounds(client, state: dict, pending: list[str],
                         track: str, t0: float) -> None:
    """The submit→poll→continue loop of run_claude_batch (split out so its
    caller can guard the whole chain against transport failures)."""
    for round_no in range(1, 9):  # 8 = the interactive pause_turn cap
        if not pending:
            break
        # Task labels can exceed the 64-char custom_id cap — use an index map.
        ids = {f"t{i}": label for i, label in enumerate(pending)}
        reqs = [{
            "custom_id": cid,
            "params": {
                "model": CLAUDE_MODEL,
                "max_tokens": 8000,
                "system": agent_system(state[label]["task"], track),
                "tools": [
                    {"type": "web_search_20260209", "name": "web_search", "max_uses": 12},
                    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 12},
                ],
                "messages": state[label]["messages"],
            },
        } for cid, label in ids.items()]
        batch = client.messages.batches.create(requests=reqs)
        print(f"[claude/{track}] batch round {round_no}: "
              f"{len(reqs)} requests -> {batch.id}")
        while True:
            time.sleep(CLAUDE_BATCH_POLL_S)
            batch = client.messages.batches.retrieve(batch.id)
            c = batch.request_counts
            elapsed = time.time() - t0
            print(f"  [claude/{track}] {batch.id}: {batch.processing_status} "
                  f"(ok {c.succeeded} / err {c.errored} / processing {c.processing}) "
                  f"{elapsed / 60:.0f}m elapsed")
            if elapsed > CLAUDE_BATCH_FRESHNESS_WARN_S:
                print(f"  [claude/{track}] WARNING: column wall-clock past "
                      f"{CLAUDE_BATCH_FRESHNESS_WARN_S // 3600}h — same-day "
                      f"freshness window at risk")
            if batch.processing_status == "ended":
                break
        nxt = []
        for res in client.messages.batches.results(batch.id):
            label = ids[res.custom_id]
            st = state[label]
            rt = res.result.type
            if rt == "succeeded":
                msg = res.result.message
                st["usage"]["input_tokens"] += msg.usage.input_tokens
                st["usage"]["output_tokens"] += msg.usage.output_tokens
                _batch_tally(st, msg.content)
                if msg.stop_reason == "pause_turn":
                    # Continuation: original user turn + the paused assistant
                    # turn passed back verbatim (encrypted_content included).
                    st["messages"] = [st["messages"][0],
                                      {"role": "assistant", "content": msg.content}]
                    nxt.append(label)
                else:
                    st["final"] = msg
            elif rt in ("errored", "expired") and not st["retried"]:
                st["retried"] = True  # the protocol's single transport-level retry
                nxt.append(label)
            else:
                st["error"] = f"batch result {rt}" + \
                    (" (after retry)" if st["retried"] else "")
        pending = nxt
    for label in pending:  # pause_turn cap exhausted mid-continuation
        state[label]["error"] = state[label]["error"] or "pause_turn cap exhausted"


def run_openai(task: dict, track: str) -> dict:
    # Image as a base64 data URI of the harness-normalized image, URL
    # fallback — bot-walled retailer CDNs 400 the whole request when the
    # server-side fetcher is blocked; same pattern as Perplexity.
    img = fetch_image_b64(task["image"])
    image_url = f"data:image/jpeg;base64,{img}" if img else task["image"]
    body = {
        "model": OPENAI_MODEL,
        "instructions": agent_system(task, track),
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": agent_user_prompt(task, track)},
            {"type": "input_image", "image_url": image_url},
        ]}],
        "tools": [{"type": "web_search"}],
        "max_output_tokens": 8000,
    }
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    t0 = time.time()
    # PROTOCOL: single transport-level retry on 5xx-before-output only —
    for _ in (1, 2):
        resp = httpx.post("https://api.openai.com/v1/responses",
                          headers=headers, json=body, timeout=600.0)
        if resp.status_code < 500:
            break
        time.sleep(5)
    resp.raise_for_status()
    d = resp.json()
    text = " ".join(
        c.get("text", "")
        for o in d.get("output", []) if o.get("type") == "message"
        for c in o.get("content", []) if c.get("type") == "output_text"
    )
    parsed = extract_json(text)
    u = d.get("usage", {})
    searches = sum(1 for o in d.get("output", []) if o.get("type") == "web_search_call")
    return {
        "results": normalize_results((parsed or {}).get("results", []), "openai"),
        "latency_s": round(time.time() - t0, 1),
        "usage": {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                  "web_search_calls": searches},
        "cost_usd": token_cost("openai", u.get("input_tokens"), u.get("output_tokens"),
                               searches=searches),
        "parse_error": parsed is None,
        "raw_text": text[-4000:],
    }


def run_gemini(task: dict, track: str) -> dict:
    """Gemini agent: google_search grounding, image attached inline (base64)."""
    parts = [{"text": agent_user_prompt(task, track)}]
    img = fetch_image_b64(task["image"])
    if img:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img}})
    body = {
        "system_instruction": {"parts": [{"text": agent_system(task, track)}]},
        "contents": [{"role": "user", "parts": parts}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 8000},
    }
    t0 = time.time()
    # PROTOCOL: transport-level retries only (5xx + rate-limit backoff —
    for attempt in (1, 2, 3):
        _gemini_throttle()
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_AGENT_MODEL}:generateContent",
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
            json=body, timeout=600.0)
        if resp.status_code == 429:
            time.sleep(25 * attempt)
            continue
        if resp.status_code < 500:
            break
        time.sleep(5)
    resp.raise_for_status()
    d = resp.json()
    text = " ".join(p.get("text", "")
                    for c in d.get("candidates", [])
                    for p in c.get("content", {}).get("parts", []))
    parsed = extract_json(text)
    u = d.get("usageMetadata", {})
    queries = sum(len(c.get("groundingMetadata", {}).get("webSearchQueries") or [])
                  for c in d.get("candidates", []))
    out_total = (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0)
    return {
        "results": normalize_results((parsed or {}).get("results", []), "gemini"),
        "latency_s": round(time.time() - t0, 1),
        "usage": {"input_tokens": u.get("promptTokenCount"),
                  "output_tokens": u.get("candidatesTokenCount"),
                  "thoughts_tokens": u.get("thoughtsTokenCount"),
                  "search_queries": queries},
        "cost_usd": token_cost("gemini", u.get("promptTokenCount"), out_total,
                               searches=queries),
        "parse_error": parsed is None,
        "raw_text": text[-4000:],
    }


AGENT_RESULTS_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "price": {"type": "number"},
                    "shipping": {"type": ["number", "null"]},
                    "pack_count": {"type": ["integer", "null"]},
                    "currency": {"type": "string"},
                    "url": {"type": "string"},
                    "retailer": {"type": "string"},
                    "same_product": {"type": "boolean"},
                },
                "required": ["title", "price", "url"],
            },
        }
    },
    "required": ["results"],
}


PPLX_PRESET = os.environ.get("PPLX_PRESET", "high")
PPLX_MODEL = os.environ.get("PPLX_MODEL", "")  # empty = use the preset


def run_perplexity(task: dict, track: str, preset: str | None = None,
                   source: str = "perplexity") -> dict:
    """Perplexity Agent API agent: server-side web search, image attached
    as a base64 data URI of the normalized image (URL-form image input
    is unreliable; oversized data URIs are rejected)."""
    content = [{"type": "input_text", "text": agent_user_prompt(task, track)}]
    img = fetch_image_b64(task["image"])
    if img:
        content.append({"type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{img}"})
    body = {
        "instructions": agent_system(task, track),
        "input": [{"type": "message", "role": "user", "content": content}],
    }
    if preset:
        body["preset"] = preset
    elif PPLX_MODEL:
        body["model"] = PPLX_MODEL
    else:
        body["preset"] = PPLX_PRESET
    headers = {"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}"}
    t0 = time.time()
    # PROTOCOL: transport-level retries only (5xx + 429 backoff — a 429
    for attempt in (1, 2, 3):
        resp = httpx.post("https://api.perplexity.ai/v1/agent",
                          headers=headers, json=body, timeout=600.0)
        if resp.status_code == 429:
            time.sleep(30 * attempt)
            continue
        if resp.status_code < 500:
            break
        time.sleep(5)
    resp.raise_for_status()
    d = resp.json()
    text = " ".join(
        c.get("text", "")
        for o in d.get("output", []) if o.get("type") == "message"
        for c in o.get("content", []) if c.get("type") in ("output_text", "text")
    ) or d.get("output_text", "") or json.dumps(d.get("output", ""))
    parsed = extract_json(text)
    u = d.get("usage", {})
    return {
        "results": normalize_results((parsed or {}).get("results", []), source),
        "latency_s": round(time.time() - t0, 1),
        "usage": {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                  "cost": u.get("cost")},
        "cost_usd": (u.get("cost") or {}).get("total_cost"),
        "parse_error": parsed is None,
        "raw_text": text[-4000:],
    }


ADAPTERS = {"findcheap": run_findcheap, "claude": run_claude,
            "openai": run_openai, "gemini": run_gemini,
            "perplexity": run_perplexity}
POOL_SIZES = {"findcheap": 1, "claude": 3, "openai": 3, "gemini": 1,
              "perplexity": 2}


def run_system(system: str, track: str, tasks: list[dict],
               retry_errors: bool = False) -> None:
    if retry_errors:
        # Completion pass after a vendor outage: re-run ONLY tasks missing
        # from the raw file or carrying an error stamp. Succeeded tasks are
        # never re-invoked — keeps the one-invocation rule intact (an
        # errored task produced no output, so its re-run is the protocol's
        # allowed transport-level retry).
        prev_path = OUT_DIR / f"raw_{system}_{track}.json"
        prev = json.loads(prev_path.read_text()).get("tasks", {}) if prev_path.exists() else {}
        tasks = [t for t in tasks
                 if t["label"] not in prev or prev[t["label"]].get("error")]
        if not tasks:
            print(f"[{system}/{track}] retry-errors: nothing to retry")
            return
        print(f"[{system}/{track}] retry-errors: {len(tasks)} errored/missing tasks")
    print(f"[{system}/{track}] running {len(tasks)} tasks")
    if system == "claude" and CLAUDE_BATCH:
        # Batch transport — whole column in one Batches API submission
        # chain; see run_claude_batch.
        results = run_claude_batch(tasks, track)
        for label, rec in results.items():
            err = rec.get("error") or ("PARSE-FAIL" if rec.get("parse_error") else "")
            ts = rec.get("tool_stats")
            ts_str = (f" [search {ts['web_search']} ({ts['search_errors']} err), "
                      f"fetch {ts['web_fetch']} ({ts['fetch_errors']} err)]") if ts else ""
            print(f"  [{system}/{track}] {label}: {len(rec['results'])} results, "
                  f"batch{ts_str} {err}")
    else:
        results, lock = {}, threading.Lock()

        def one(task):
            try:
                rec = ADAPTERS[system](task, track)
            except Exception as e:
                rec = {"results": [], "latency_s": None, "error": f"{type(e).__name__}: {e}"[:300]}
            with lock:
                results[task["label"]] = rec
                err = rec.get("error") or ("PARSE-FAIL" if rec.get("parse_error") else "")
                ts = rec.get("tool_stats")
                ts_str = (f" [search {ts['web_search']} ({ts['search_errors']} err), "
                          f"fetch {ts['web_fetch']} ({ts['fetch_errors']} err)]") if ts else ""
                print(f"  [{system}/{track}] {task['label']}: {len(rec['results'])} results, "
                      f"{rec.get('latency_s')}s{ts_str} {err}")

        with ThreadPoolExecutor(max_workers=POOL_SIZES[system]) as pool:
            list(pool.map(one, tasks))
    path = OUT_DIR / f"raw_{system}_{track}.json"
    if path.exists():  # partial runs merge; re-run tasks overwrite their own entries
        results = {**json.loads(path.read_text()).get("tasks", {}), **results}
    save(f"raw_{system}_{track}.json", {
        "model": {"findcheap": f"findcheap backend @ {FINDCHEAP_URL}",
                  "claude": CLAUDE_MODEL + (" (message batches transport)"
                                            if CLAUDE_BATCH else ""),
                  "openai": OPENAI_MODEL,
                  "gemini": GEMINI_AGENT_MODEL,
                  "perplexity": f"perplexity agent api ({PPLX_MODEL or 'preset=' + PPLX_PRESET})",}[system],
        "track": track,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "protocol": PROTOCOL,
        "tasks": results,
    })
    # Completion summary: a vendor outage (credits, API down) shows up here
    # as errored tasks; every other system keeps running independently.
    n_err = sum(1 for r in results.values() if r.get("error"))
    if n_err:
        print(f"[{system}/{track}] DONE WITH ERRORS: {n_err}/{len(results)} tasks "
              f"errored — top up / fix the vendor, then complete with "
              f"run_system(..., retry_errors=True)")
    else:
        print(f"[{system}/{track}] done: {len(results)} tasks, 0 errors")
