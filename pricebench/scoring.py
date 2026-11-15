"""Metrics and the report tables (savings capture, CIs, per-judge agreement)."""

from __future__ import annotations

from .common import (OUT_DIR, SYSTEMS, JUDGE_PANEL, PROTOCOL, load,
                     load_tasks, save)
from .judges import UNIVERSAL_JUDGE
from . import verification
from .verification import (load_labels, load_ref_audit, ref_landed,
                           policy_shipping, effective_price, effective_shipping,
                           effective_count, qualifies,
                           AMAZON_NONPRIME_DEFAULT, _is_amazon)


# ------------------------------------------------------------ report


def bootstrap_ci(values: list[float], n_boot: int = 10000, seed: int = 0) -> list[float] | None:
    """95% bootstrap CI on the mean: resample TASKS with replacement.
    Fixed seed so the report is reproducible from the same artifacts."""
    import random

    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values)
        for _ in range(n_boot)
    )
    return [round(means[int(0.025 * n_boot)], 1), round(means[int(0.975 * n_boot)], 1)]


def _task_cost(rec: dict) -> float | None:
    """Per-task dollar cost: the adapter-stamped cost_usd, else Perplexity's
    native usage.cost.total_cost (the only cost recoverable from old raws)."""
    if rec.get("cost_usd") is not None:
        return rec["cost_usd"]
    return ((rec.get("usage") or {}).get("cost") or {}).get("total_cost")


def report(track: str) -> None:
    tasks = load_tasks()
    if not (OUT_DIR / f"grades_{track}.json").exists():
        print(f"[report/{track}] no grades yet — run --run then --grade first "
              f"(looked in {OUT_DIR})")
        return
    grades = load(f"grades_{track}.json")
    human = load_labels(track)
    refs = load_ref_audit(track)
    raws = {s: load(f"raw_{s}_{track}.json") for s in SYSTEMS
            if (OUT_DIR / f"raw_{s}_{track}.json").exists()}

    # Performance metrics accumulate ONLY over opportunity tasks (the pool
    # found a verified cheaper qualifying listing, so Opportunity > 0).
    # No-opportunity tasks are counted in meta for context but carry no
    # per-agent metric (README metric-set freeze).
    per = {s: {"found": [], "capture": [], "success": 0, "recall": 0,
               "lat": []} for s in raws}
    n_opp, n_noopp = 0, 0
    for t in tasks:
        label = t["label"]
        # All comparisons are PER UNIT (landed price / pack count): for
        # single-unit references this is identical to landed price; for
        # multi-packs it matches production's price-per-count display.
        # Review-time ref-count corrections outrank the frozen value.
        ref_count = int(human.get("ref_counts", {}).get(label) or
                        t.get("pack_count") or 1)
        ref = ref_landed(t, human, refs) / ref_count  # per-unit reference
        best = {}
        ran = set()  # systems that actually ran this task (partial coverage)
        for s in raws:
            rec = raws[s]["tasks"].get(label)
            if rec is None:
                best[s] = None
                continue
            ran.add(s)
            if rec.get("latency_s"):
                per[s]["lat"].append(rec["latency_s"])
            # Review protocol: the reviewer verifies top-down and stops
            # at the first PASS. Once ANY human pass exists for this
            # agent+task, only human-passed rows compete for the agent's
            # best (lowest landed among them) — an unreviewed
            # panel-accepted row with unknown shipping must not undercut
            # a verified one. No labels → panel fallback.
            b = None
            b_human, has_human_pass = None, False
            for idx, cand in enumerate(rec["results"]):
                g = grades.get(s, {}).get(label, {}).get(str(idx), {})
                hl = human["labels"].get(f"{track}|{s}|{label}|{idx}")
                q = qualifies(g, hl)
                # landed = item + shipping; human-audited values > vendor
                # data > page audit > agent claim, independently per part
                key = f"{track}|{s}|{label}|{idx}"
                h_price = human["prices"].get(key)
                h_ship = human["shipping"].get(key)
                price = h_price if h_price is not None else effective_price(cand, g)
                # policy shipping is locked — it outranks even saved human
                # values (they may predate the policy ruling)
                pol = policy_shipping(cand.get("url"), price)
                ship = pol if pol is not None else \
                    h_ship if h_ship is not None else effective_shipping(cand, g)
                if ship is None and _is_amazon(cand.get("url")) and price < 35.0:
                    # same non-Prime $6.99 default as the review UI shows —
                    # scoring matches the display
                    ship = AMAZON_NONPRIME_DEFAULT
                h_count = human.get("counts", {}).get(key)
                count = h_count or effective_count(cand, g, ref_count)
                # Single-item references compare on TOTAL landed price:
                # wanting one item doesn't mean the user would buy a
                # cheaper-per-unit multipack. Per-unit scoring applies
                # only to multipack references.
                if ref_count == 1:
                    count = 1
                lp = (price + (ship or 0.0)) / count  # per-unit landed
                eps = 0.005 / ref_count  # half-cent, scaled to the unit basis
                if lp < ref - eps:
                    if q:
                        b = lp if b is None else min(b, lp)
                        if hl == "pass":
                            has_human_pass = True
                            b_human = lp if b_human is None else min(b_human, lp)
            best[s] = b_human if has_human_pass else b
        pool = [p for p in best.values() if p is not None]
        if not pool:
            # No opportunity: nobody surfaced a verified cheaper listing.
            n_noopp += 1
            continue
        oracle = min(pool)  # oracle < ref by construction, so Opportunity > 0
        n_opp += 1
        for s in ran:  # only systems that ran the task are scored on it
            p = best[s]
            per[s]["found"].append(100 * (ref - p) / ref if p else 0.0)
            per[s]["capture"].append(100 * (ref - p) / (ref - oracle) if p else 0.0)
            per[s]["success"] += bool(p)
            per[s]["recall"] += bool(p is not None and p <= oracle + 0.01 / (t.get("pack_count") or 1))
            per[s]["n_ran"] = per[s].get("n_ran", 0) + 1

    summary = {}
    for s in raws:
        d = per[s]
        lat = sorted(d["lat"])
        u_in = sum((raws[s]["tasks"].get(t["label"], {}).get("usage") or {}).get("input_tokens") or 0 for t in tasks)
        u_out = sum((raws[s]["tasks"].get(t["label"], {}).get("usage") or {}).get("output_tokens") or 0 for t in tasks)
        n_ran = d.get("n_ran", 0)  # opportunity tasks this system actually ran
        # Dollar cost stamped by each adapter at run time (cost_usd); for
        # older raws, Perplexity's native usage.cost.total_cost is the one
        # recoverable value — everything else shows "—", no guesses.
        costed = [c for c in (_task_cost(raws[s]["tasks"].get(t["label"]) or {})
                              for t in tasks) if c is not None]
        summary[s] = {
            "tasks_run": sum(1 for t in tasks if t["label"] in raws[s]["tasks"]),
            "savings_capture_pct_mean": round(sum(d["capture"]) / len(d["capture"]), 1) if d["capture"] else None,
            "savings_capture_ci95": bootstrap_ci(d["capture"]),
            "savings_found_pct_mean": round(sum(d["found"]) / len(d["found"]), 1) if d["found"] else None,
            "success_rate_pct": round(100 * d["success"] / n_ran, 1) if n_ran else None,
            "best_price_recall_pct": round(100 * d["recall"] / n_ran, 1) if n_ran else None,
            "median_latency_s": lat[len(lat) // 2] if lat else None,
            "cost_usd_total": round(sum(costed), 2) if costed else None,
            "cost_usd_per_search": round(sum(costed) / len(costed), 3) if costed else None,
            "tokens_in": u_in or None, "tokens_out": u_out or None,
        }
    meta = {"track": track, "n_tasks": len(tasks),
            "n_opportunity_tasks": n_opp, "n_no_opportunity_tasks": n_noopp,
            "judge_panel": JUDGE_PANEL,
            "universal_judge_prompts": UNIVERSAL_JUDGE,
            "protocol": PROTOCOL}
    meta["shipping_basis"] = ("PRIME COUNTERFACTUAL: all Amazon shipping $0 "
                              "(~75% of US Amazon shoppers have Prime)") if verification.PRIME_MODE \
        else ("non-Prime / no membership programs; ref shipping pre-verified "
              "(--ref-shipping: locked policy table, else manual logged-out "
              "check at ZIP 10001)")
    suffix = "_prime" if verification.PRIME_MODE else ""
    save(f"summary_{track}{suffix}.json", {"meta": meta, "systems": summary})

    cols = list(summary)
    hdr = f"\n=== TRACK: {track} ===  (opportunity on {n_opp}/{len(tasks)} tasks, " \
          f"{n_noopp} no-opportunity)"
    print(hdr)
    w = 30

    def fmt(s, key):
        v = summary[s].get(key)
        if key == "savings_capture_ci95" and v:
            return f"[{v[0]},{v[1]}]"
        return str(v if v is not None else "—")

    print(f"{'':{w}}" + "".join(f"{c:>14}" for c in cols))
    for key, name in [("tasks_run", "tasks run"),
                      ("savings_capture_pct_mean", "savings capture % (HEADLINE)"),
                      ("savings_capture_ci95", "  95% bootstrap CI"),
                      ("savings_found_pct_mean", "savings found % (mean)"),
                      ("success_rate_pct", "success rate %"),
                      ("best_price_recall_pct", "best-price recall %"),
                      ("median_latency_s", "median latency s"),
                      ("cost_usd_total", "cost $ (total)"),
                      ("cost_usd_per_search", "cost $/search"),
                      ("tokens_in", "tokens in"),
                      ("tokens_out", "tokens out")]:
        print(f"{name:<{w}}" + "".join(f"{fmt(c, key):>14}" for c in cols))
