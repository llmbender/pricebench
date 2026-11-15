"""Automated verification: /extract audits, link checks, shipping policy,
qualification rules, and the landed-cost authority chains.

The published N=100 artifacts were produced by an equivalent internal
verification implementation; divergence disclosed in the README."""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from .common import (OUT_DIR, TASKS_PATH, FINDCHEAP_API, FINDCHEAP_KEY,
                     SYSTEMS, JS_PRICE_HOSTS, save, load, load_tasks)


# ------------------------------------------------------------ grading


def check_link(url: str | None, price: float) -> dict:
    """'blocked' = bot wall = unknown, never failure. JS-rendered hosts
    get price_on_page=None (static check not applicable)."""
    if not url:
        return {"link": "none", "price_on_page": None}
    from curl_cffi import requests as curl_requests

    try:
        r = curl_requests.get(url, impersonate="chrome", timeout=25, allow_redirects=True)
    except Exception as e:
        return {"link": "error", "detail": str(e)[:120], "price_on_page": None}
    if r.status_code in (403, 429, 503):
        return {"link": "blocked", "price_on_page": None}
    if r.status_code >= 400:
        return {"link": "dead", "status": r.status_code, "price_on_page": None}
    html = r.text or ""
    if re.search(r"captcha|robot check|are you a human", html[:20000], re.I):
        return {"link": "blocked", "price_on_page": None}
    host = url.split("/")[2].lower() if "://" in url else ""
    if any(h in host for h in JS_PRICE_HOSTS):
        return {"link": "ok", "price_on_page": None}
    return {"link": "ok", "price_on_page": f"{price:.2f}" in html}


# ------------------------------------------------------------ listing verification

_extract_cache: dict = {}


def fc_extract(url: str | None) -> dict | None:
    """Findcheap /extract client — the automated listing verifier
    (https://findcheap.ai/developers/docs). Returns None on failure;
    the row then falls to human verification."""
    if not url:
        return None
    if url in _extract_cache:
        return _extract_cache[url]
    result = None
    try:
        r = None
        for attempt in (1, 2, 3):
            r = httpx.post(f"{FINDCHEAP_API}/extract", json={"url": url},
                           headers={"X-Findcheap-Key": FINDCHEAP_KEY} if FINDCHEAP_KEY else {},
                           timeout=60.0)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "10") or 10))
                continue
            if r.status_code < 500:
                break
            time.sleep(5)
        if r is not None and r.status_code == 200:
            result = r.json()
    except Exception:
        result = None
    _extract_cache[url] = result
    return result


# Published retailer shipping POLICIES: hard-coded thresholds beat
# per-item lookups — no API credits, no human chasing. Values are LOCKED
# in the verify UI. Amazon's under-$35 non-membership fee varies by
# item, so below threshold it stays unknown (editable).
SHIPPING_POLICIES = {
    "amazon.com": {"free_over": 35.0, "under_fee": None},
    "walmart.com": {"free_over": 35.0, "under_fee": 6.99},
    "target.com": {"free_over": 35.0, "under_fee": 5.99},
    # reviewed batch 2026-08-26 (under_fee None = varies/unspecified):
    "bestbuy.com": {"free_over": 35.0, "under_fee": None},
    "kohls.com": {"free_over": 49.0, "under_fee": 8.95},
    "apple.com": {"free_over": 0.0, "under_fee": None},      # always free
    "nordstrom.com": {"free_over": 0.0, "under_fee": None},  # always free
    "macys.com": {"free_over": 49.0, "under_fee": None},
    "shein.com": {"free_over": 29.0, "under_fee": None},     # matches us.shein.com too
    "sephora.com": {"free_over": 50.0, "under_fee": None},
    "chewy.com": {"free_over": 49.0, "under_fee": 4.99},
}


# Amazon shipping basis: primary scoring assumes NO Prime (consistent
# with the protocol's no-membership-prices rule) — under-$35 Amazon
# shipping varies, so those refs/candidates resolve via manual
# logged-out checks. `--report --prime` recomputes the counterfactual
# where all Amazon shipping is $0 (~75% of US Amazon shoppers have
# Prime), written to summary_<track>_prime.json — the primary artifacts
# untouched.
PRIME_MODE = False


# Review-time default for unknown-shipping Amazon candidates under $35:
# the standard non-Prime fee. Pre-filled and EDITABLE in the verify UI
# (not a locked policy — "almost always" isn't always); scoring applies
# the same default so unedited rows score as displayed. Under --prime
# the $0 policy outranks it, so the counterfactual is clean.
AMAZON_NONPRIME_DEFAULT = 6.99


def _is_amazon(url: str | None) -> bool:
    if not url or "://" not in url:
        return False
    host = url.split("/")[2].lower().removeprefix("www.")
    return host == "amazon.com" or host.endswith(".amazon.com")


def policy_shipping(url: str | None, item_price: float) -> float | None:
    if not url or "://" not in url:
        return None
    host = url.split("/")[2].lower().removeprefix("www.")
    if PRIME_MODE and (host == "amazon.com" or host.endswith(".amazon.com")):
        # Counterfactual: Prime ships everything free. Returned as a LOCKED
        # policy value so it outranks stored non-Prime manual entries.
        return 0.0
    for phost, rule in SHIPPING_POLICIES.items():
        if host == phost or host.endswith("." + phost):
            if item_price >= rule["free_over"]:
                return 0.0
            return rule["under_fee"]  # None = varies → stays unknown
    return None


def effective_shipping(cand: dict, g: dict) -> float | None:
    """Policy (hard-coded, locked) > vendor-sourced shipping stamped into
    the grade (shipping_probe) > page audit > agent-reported > unknown
    (human entries are applied above all of these in report()/the UI)."""
    pol = policy_shipping(cand.get("url"), effective_price(cand, g))
    if pol is not None:
        return pol
    probe = g.get("shipping_probe") or {}
    if probe.get("cost") is not None:
        return probe["cost"]
    a = g.get("page_audit") or {}
    if a.get("mandatory_shipping") is not None:
        return float(a["mandatory_shipping"])
    return cand.get("shipping")


def effective_price(cand: dict, g: dict) -> float:
    """Vendor-sourced price (stamped into the grade) > page-audit
    displayed price > agent claim. FINDCHEAP candidates' item prices are
    vendor-API sourced and TRUSTED — the page audit never overrides them
    (its shipping/stock findings still apply)."""
    sp = g.get("shipping_probe") or {}
    if sp.get("price") is not None:
        return float(sp["price"])
    if cand.get("source") != "findcheap":
        a = g.get("page_audit") or {}
        if a.get("displayed_price") is not None:
            return float(a["displayed_price"])
    return cand["price"]


def landed_price(cand: dict, g: dict) -> float:
    """Audited item price + known mandatory shipping (unknown treated as
    0 — the frontier audit / human pass owns the unknown cases)."""
    return effective_price(cand, g) + (effective_shipping(cand, g) or 0.0)


def effective_count(cand: dict, g: dict, ref_count: int) -> int:
    """Pack count for per-unit scoring: agent-reported count > panel
    median > reference count (no normalization — conservative; the
    frontier audit/human corrects). Human count entries are applied in
    report()/UI, above all of these."""
    if cand.get("pack_count"):
        return cand["pack_count"]
    if g.get("pack_count"):
        return g["pack_count"]
    return ref_count


# ------------------------------------------------------------ frontier page audit
# One automated check covering the three things a title/image judge
# cannot see: is the listing IN STOCK, is the claimed PRICE real, and
# what is the mandatory SHIPPING. Runs only on the price frontier (the
# cheapest panel-passing candidates per system/task — the only ones
# that can move a score). Unreadable pages stay unknown → resolved by
# the human verification pass.

AUDIT_FRONTIER_N = 3  # cheapest N per system per task

def page_audit_one(cand: dict) -> dict:
    """Audit one listing via /extract: status ok | gone (not a product
    page) | error (unknown → human). Shipping resolves via the policy
    table, the endpoint's report when present, and the UI."""
    d = fc_extract(cand.get("url"))
    if d is None:
        return {"status": "error"}
    if not d.get("is_product_page"):
        return {"status": "gone", "in_stock": False,
                "displayed_price": None, "mandatory_shipping": None}
    return {"status": "ok",
            "in_stock": d.get("in_stock"),
            "displayed_price": d.get("price"),
            "mandatory_shipping": d.get("shipping"),
            "extracted_title": d.get("title")}


def audit(track: str, only: str | None = None,
          systems: tuple | None = None, reaudit: bool = False) -> None:
    tasks = {t["label"]: t for t in load_tasks()}
    grades = load(f"grades_{track}.json")
    raws = {s: load(f"raw_{s}_{track}.json")["tasks"] for s in (systems or SYSTEMS)
            if (OUT_DIR / f"raw_{s}_{track}.json").exists()}
    jobs = []
    for s, raw in raws.items():
        for label, rec in raw.items():
            if only and label != only:
                continue
            frontier = []
            for idx, cand in enumerate(rec.get("results", [])):
                g = grades.get(s, {}).get(label, {}).get(str(idx), {})
                if g.get("page_audit") is not None and not reaudit:
                    continue  # idempotent: audited once, verdict frozen
                if g.get("match") and g.get("link") != "dead" \
                        and landed_price(cand, g) < tasks[label]["price"] - 0.005:
                    frontier.append((landed_price(cand, g), idx, cand, g))
            frontier.sort(key=lambda f: f[0])
            jobs += [(s, label, idx, cand, g) for _, idx, cand, g in frontier[:AUDIT_FRONTIER_N]]
    print(f"[audit/{track}] {len(jobs)} frontier candidates")
    lock = threading.Lock()

    def one(job):
        s, label, idx, cand, g = job
        a = page_audit_one(cand)
        with lock:
            grades[s][label][str(idx)]["page_audit"] = a
            print(f"  [audit/{track}] {s}/{label}#{idx}: {a.get('status')} "
                  f"stock={a.get('in_stock')} price={a.get('displayed_price')} "
                  f"ship={a.get('mandatory_shipping')}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(one, jobs))
    save(f"grades_{track}.json", grades)

    # The REFERENCE pages get the same audit: the original product's
    # mandatory shipping enters the comparison too, so the reference
    # plays by identical landed-cost rules.
    ref_audit = load_ref_audit(track)

    def one_ref(task):
        pseudo = {"price": task["price"], "retailer": task["url"].split("/")[2],
                  "url": task["url"]}
        a = page_audit_one(pseudo)
        with lock:
            ref_audit[task["label"]] = a
            print(f"  [audit/{track}] REF {task['label']}: {a.get('status')} "
                  f"stock={a.get('in_stock')} price={a.get('displayed_price')} "
                  f"ship={a.get('mandatory_shipping')}")

    ref_todo = [t for t in tasks.values() if not only or t["label"] == only]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one_ref, ref_todo))
    save(f"ref_audit_{track}.json", ref_audit)


# ------------------------------------------------------------ verification (human)


USED_CONDITION_RE = re.compile(r"used|refurb|pre-?owned|parts|broken", re.I)


def qualifies(g: dict, human: str | None) -> bool:
    """Human label wins outright; otherwise dead links, verified
    out-of-stock listings, and marketplace-stated used/refurb/for-parts
    conditions never qualify (Open box / New other are NEW — pass);
    otherwise the panel decides."""
    if human is not None:
        return human == "pass"
    if g.get("link") == "dead":
        return False
    if (g.get("shipping_probe") or {}).get("in_stock") is False:  # vendor-sourced
        return False
    if (g.get("page_audit") or {}).get("in_stock") is False:
        return False
    cond = (g.get("shipping_probe") or {}).get("condition") or g.get("condition") or ""
    if USED_CONDITION_RE.search(cond) and "open box" not in cond.lower():
        return False
    return bool(g.get("match"))


REF_FIELDS = ("ref_prices", "ref_shipping", "ref_counts")


def load_shared_refs() -> dict:
    """Reference products are identical across tracks, so ref-level review
    corrections live in ONE shared file — an edit on either track's page
    ports to the other without relitigation."""
    path = OUT_DIR / "verify_labels_refs.json"
    out = {k: {} for k in REF_FIELDS}
    if path.exists():
        out.update(json.loads(path.read_text()))
    return out


def load_labels(track: str) -> dict:
    path = OUT_DIR / f"verify_labels_{track}.json"
    out = {"labels": {}, "ref_prices": {}, "shipping": {},
           "prices": {}, "ref_shipping": {}, "counts": {},
           # reason codes ride along with rulings for auditability;
           # report scoring ignores them.
           "reasons": {},
           # review-time ref pack-count corrections (outrank task pack_count)
           "ref_counts": {}}
    if path.exists():
        out.update(json.loads(path.read_text()))
    # shared ref-level corrections overlay the per-track values (refs are
    # identical across tracks; the shared store is authoritative)
    shared = load_shared_refs()
    for k in REF_FIELDS:
        out.setdefault(k, {}).update(shared.get(k) or {})
    return out


def load_ref_audit(track: str) -> dict:
    path = OUT_DIR / f"ref_audit_{track}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def ref_landed(task: dict, human: dict, ref_audit: dict) -> float:
    """Reference landed cost: (reviewer-entered price or frozen price) +
    its own mandatory shipping (policy > pre-run stamped ref_shipping >
    review entry > page audit > 0-if-unknown). The reference plays by
    the same landed-cost rules as candidates."""
    price = human["ref_prices"].get(task["label"], task["price"])
    ship = policy_shipping(task["url"], price)  # locked policy first
    if ship is None:
        # Pre-run manual pass: non-deterministic ref shipping is
        # verified BEFORE searches (--ref-shipping) and stamped into
        # tasks.json, so the target agents chase is landed-correct from
        # the start.
        ship = task.get("ref_shipping")
    if ship is None:
        ship = human["ref_shipping"].get(task["label"])
    if ship is None:
        ship = (ref_audit.get(task["label"]) or {}).get("mandatory_shipping")
    return price + (ship or 0.0)


def ref_shipping_pass() -> None:
    """--ref-shipping: the pause-after-sourcing step. Resolves every
    task's reference shipping deterministically where a rule exists
    (locked policy table; non-Prime Amazon: >= $35 free, under varies ->
    manual), probes the rest through /extract, and emits
    ref_shipping_todo.json for the remainder — the operator fills the
    nulls (logged out, ZIP 10001). Re-run after filling: values merge
    into tasks.json as ref_shipping and searches may start. Idempotent;
    run until it reports complete."""
    tasks = json.loads(TASKS_PATH.read_text())
    todo_path = OUT_DIR / "ref_shipping_todo.json"
    filled = {}
    if todo_path.exists():
        filled = {k: v.get("shipping") for k, v in
                  json.loads(todo_path.read_text()).items()
                  if v.get("shipping") is not None}
    todo, n_policy, n_stamped = {}, 0, 0
    for t in tasks:
        pol = policy_shipping(t["url"], t["price"])
        if pol is not None:
            t.pop("ref_shipping", None)  # policy is locked; no stamp needed
            n_policy += 1
            continue
        # Automated probe BEFORE the manual todo: refs whose pages report
        # a mandatory shipping figure through /extract resolve
        # programmatically at the frozen ZIP. Manual is the remainder.
        if t.get("ref_shipping") is None and t["label"] not in filled:
            d = fc_extract(t["url"])
            ship = (d or {}).get("shipping")
            if ship is not None:
                t["ref_shipping"] = float(ship)
                t["ref_shipping_src"] = "extract probe ZIP-10001"
        if t["label"] in filled:
            t["ref_shipping"] = float(filled[t["label"]])
            t["ref_shipping_src"] = "manual ZIP-10001 non-member basis"
        if t.get("ref_shipping") is not None:
            n_stamped += 1
            continue
        host = t["url"].split("/")[2].removeprefix("www.") if "://" in t["url"] else ""
        todo[t["label"]] = {"url": t["url"], "price": t["price"],
                            "host": host, "shipping": None}
    TASKS_PATH.write_text(json.dumps(tasks, indent=1))
    if todo:
        todo_path.write_text(json.dumps(todo, indent=1))
        print(f"[ref-shipping] {n_policy} policy-covered, {n_stamped} stamped, "
              f"{len(todo)} NEED MANUAL VALUES -> {todo_path}")
        print("[ref-shipping] fill each null 'shipping' (logged out, ZIP 10001), "
              "then re-run --ref-shipping. Do NOT start searches until complete.")
    else:
        todo_path.unlink(missing_ok=True)
        print(f"[ref-shipping] COMPLETE: {n_policy} policy-covered, "
              f"{n_stamped} stamped. Searches may start.")
