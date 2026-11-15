"""The 3-lab judge panel: universal prompts, per-judge clients, grading."""

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

from .common import (OUT_DIR, JUDGE_PANEL, SYSTEMS, fetch_image_b64,
                     _gemini_throttle, save, load, load_tasks)
from .verification import check_link


UNIVERSAL_JUDGE = {
    "exact": (
        "You are evaluating whether a candidate listing is the IDENTICAL product "
        "as a reference product.\n"
        "ACCEPT only if the candidate is the same brand, same model/style, and "
        "same per-item size/capacity as the reference. A different colorway of "
        "the same model is acceptable — EXCEPT for fashion/apparel/footwear "
        "products, where the candidate must match the color and design shown. "
        "For apparel/footwear/wearables the garment or shoe SIZE is a variant — "
        "never a reason to reject. "
        "Pack/unit QUANTITY is NOT part of identity: a different pack or unit "
        "count of the identical item — including a multi-pack or bundle "
        "consisting ONLY of that item — is ACCEPTABLE (value is scored on "
        "price per unit by the pipeline, not by you). Per-item capacity, "
        "volume, and size DO remain part of identity: REJECT mini/travel "
        "sizes, different capacities, bundles that include other products, "
        "other models or lines from the same brand, and counterfeit "
        "or replica versions.\n"
        "Condition: reject ONLY if the listing explicitly states used, "
        "refurbished, pre-owned, or for-parts. Do NOT infer condition from "
        "photos or terse titles. When a marketplace-stated condition is "
        "provided, trust it: 'Open box' / 'New other' / no-box / damaged-box "
        "grades mean a NEW item and are acceptable.\n"
        'Return ONLY JSON: {"verdict": "ACCEPT" or "REJECT", '
        '"pack_count": <TOTAL number of identical base units in the candidate '
        "listing, counted in the same base unit as the reference — e.g. 2 for "
        "a 2-pack; a listing of 4 packs of 500 swabs against a 500-count "
        'reference is 2000; null if unclear>, "reason": "<20 words"}'
    ),
    "equiv": (
        "You are evaluating whether a candidate product is a reasonable substitute "
        "for a reference product from the perspective of a typical shopper.\n"
        "Determine whether the candidate:\n"
        "- serves the same primary purpose;\n"
        "- is the same general product type;\n"
        "- preserves the important functional characteristics of the reference product;\n"
        "- is not materially lower quality than the reference, incomplete, "
        "counterfeit, or otherwise non-comparable. "
        "A quality objection must rest on AFFIRMATIVE evidence in the listing "
        "(stated specs that fall short, missing components, counterfeit "
        "indications such as an implausible brand/model claim). Being "
        "unbranded or an unfamiliar brand is NOT evidence of lower quality, "
        "and specifications absent from a terse listing title are UNKNOWN — "
        "not evidence the product lacks them;\n"
        "- physical size, capacity, dimensions, apparel sizing, AND pack/unit "
        "count are NONESSENTIAL for equivalence — a different size, capacity, "
        "or pack count of an otherwise comparable product still qualifies "
        "(different counts are scored on price-per-unit by the pipeline, not "
        "by you);\n"
        "- variant and formulation differences within the same brand's "
        "comparable product family are NONESSENTIAL unless they change the "
        "product's primary purpose or product type;\n"
        "- is NEW: reject on condition ONLY if the listing explicitly states "
        "used, refurbished, pre-owned, or for-parts — never inferred from "
        "photos or terse titles; a marketplace-stated 'Open box' / 'New other' "
        "/ no-box / damaged-box grade means NEW and is acceptable;\n"
        "- may differ in brand, appearance, or nonessential features — EXCEPT "
        "for fashion/apparel/footwear products, where appearance matters: "
        "judge visual similarity from the images. The candidate should have a "
        "closely similar overall look to the reference — same kind of garment "
        "worn the same way, similar style and aesthetic. The candidate and "
        "reference should be the same general color, but need not match "
        "exactly. REJECT only when the look is clearly different from the "
        "reference.\n"
        'Return ONLY JSON: {"verdict": "ACCEPT" or "REJECT", '
        '"pack_count": <TOTAL number of identical base units in the candidate '
        "listing, counted in the same base unit as the reference — e.g. 2 for "
        "a 2-pack; a listing of 4 packs of 500 swabs against a 500-count "
        'reference is 2000; null if unclear>, "reason": "<20 words"}'
    ),
}


def parse_verdict(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            if str(v.get("verdict", "")).upper() in ("ACCEPT", "REJECT"):
                pc = v.get("pack_count")
                return {"accept": v["verdict"].upper() == "ACCEPT",
                        "pack_count": int(pc) if isinstance(pc, (int, float)) and pc >= 1 else None,
                        "reason": str(v.get("reason", ""))[:120]}
        except (json.JSONDecodeError, ValueError):
            pass
    m = re.search(r"\b(ACCEPT|REJECT)\b", text.upper())
    return {"accept": m.group(1) == "ACCEPT", "reason": text[:120]} if m else None


def _judge_claude(parts: dict, client) -> dict | None:
    content = [{"type": "text", "text": parts["ref_text"]}]
    if parts["ref_img"]:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": parts["ref_img"]}})
    content.append({"type": "text", "text": parts["cand_text"]})
    if parts["cand_img"]:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": parts["cand_img"]}})
    content.append({"type": "text", "text": parts["prompt"]})
    resp = client.messages.create(model=JUDGE_PANEL["claude"], max_tokens=700,
                                  messages=[{"role": "user", "content": content}])
    return parse_verdict("".join(b.text for b in resp.content if b.type == "text"))


def _judge_openai(parts: dict) -> dict | None:
    content = [{"type": "input_text", "text": parts["ref_text"]}]
    if parts["ref_img"]:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{parts['ref_img']}"})
    content.append({"type": "input_text", "text": parts["cand_text"]})
    if parts["cand_img"]:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{parts['cand_img']}"})
    content.append({"type": "input_text", "text": parts["prompt"]})
    resp = httpx.post("https://api.openai.com/v1/responses",
                      headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                      json={"model": JUDGE_PANEL["openai"],
                            "input": [{"role": "user", "content": content}],
                            "max_output_tokens": 600},
                      timeout=120.0)
    resp.raise_for_status()
    text = " ".join(c.get("text", "")
                    for o in resp.json().get("output", []) if o.get("type") == "message"
                    for c in o.get("content", []) if c.get("type") == "output_text")
    return parse_verdict(text)


def _judge_gemini(parts: dict) -> dict | None:
    _gemini_throttle()
    gparts = [{"text": parts["ref_text"]}]
    if parts["ref_img"]:
        gparts.append({"inline_data": {"mime_type": "image/jpeg", "data": parts["ref_img"]}})
    gparts.append({"text": parts["cand_text"]})
    if parts["cand_img"]:
        gparts.append({"inline_data": {"mime_type": "image/jpeg", "data": parts["cand_img"]}})
    gparts.append({"text": parts["prompt"]})
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{JUDGE_PANEL['gemini']}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        json={"contents": [{"parts": gparts}]},
        timeout=120.0)
    resp.raise_for_status()
    text = " ".join(p.get("text", "")
                    for c in resp.json().get("candidates", [])
                    for p in c.get("content", {}).get("parts", []))
    return parse_verdict(text)


def judge_candidate(task: dict, cand: dict, track: str, client,
                    condition: str | None = None) -> dict:
    """Majority verdict of the judge panel. Judges see reference + candidate
    (titles and images) and the universal track prompt — never the
    producing agent, price, or retailer. Stated condition is passed as
    fact. A failed judge is retried once; a 1-1 split rejects."""
    cand_text = f"CANDIDATE listing:\n{cand['title']}"
    if condition:
        cand_text += (f"\nMarketplace-stated condition: {condition} "
                      "(authoritative — trust this over any impression from the photo)")
    parts = {
        "ref_text": f"REFERENCE product:\n{task['title']}\nBrand: {task['brand']}",
        "ref_img": fetch_image_b64(task["image"]),
        "cand_text": cand_text,
        "cand_img": fetch_image_b64(cand["image"]) if cand.get("image") else None,
        "prompt": UNIVERSAL_JUDGE[track],
    }
    all_judges = {"claude": lambda: _judge_claude(parts, client),
                  "openai": lambda: _judge_openai(parts),
                  "gemini": lambda: _judge_gemini(parts)}
    judges = {name: all_judges[name] for name in JUDGE_PANEL}
    panel = {}
    for name, call in judges.items():
        verdict, err = None, None
        for _ in (1, 2):
            try:
                verdict = call()
                if verdict:
                    break
                err = "unparseable verdict"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:150]
                time.sleep(2)
        panel[name] = verdict if verdict else {"error": err}
    votes = [v["accept"] for v in panel.values() if "accept" in v]
    accepts = sum(votes)
    match = len(votes) > 0 and accepts * 2 > len(votes)
    counts = sorted(v["pack_count"] for v in panel.values() if v.get("pack_count"))
    return {"match": match,
            "votes": f"{accepts}/{len(votes)}",
            "pack_count": counts[len(counts) // 2] if counts else None,
            "panel": panel,
            "images_used": bool(parts["ref_img"]) + bool(parts["cand_img"])}


def grade(track: str, only: str | None = None,
          systems: tuple | None = None) -> None:
    import anthropic

    client = anthropic.Anthropic()
    tasks = {t["label"]: t for t in load_tasks()}
    jobs, grades = [], {}
    if (OUT_DIR / f"grades_{track}.json").exists():
        grades = load(f"grades_{track}.json")  # merge — verdicts are frozen
    for system in (systems or SYSTEMS):
        path = OUT_DIR / f"raw_{system}_{track}.json"
        if not path.exists():
            print(f"[grade/{track}] skipping {system} (no raw file)")
            continue
        for label, rec in load(f"raw_{system}_{track}.json")["tasks"].items():
            if only and label != only:
                continue
            if not only and grades.get(system, {}).get(label):
                continue  # incremental: verdicts frozen at first determination
            grades.setdefault(system, {})[label] = {}
            for idx, cand in enumerate(rec.get("results", [])):
                jobs.append((system, label, idx, cand))
    print(f"[grade/{track}] {len(jobs)} candidates")
    lock = threading.Lock()

    def one(job):
        system, label, idx, cand = job
        g = check_link(cand.get("url"), cand["price"])
        cond = cand.get("condition")
        g["condition"] = cond
        g.update(judge_candidate(tasks[label], cand, track, client, condition=cond))
        with lock:
            grades.setdefault(system, {}).setdefault(label, {})[str(idx)] = g
            print(f"  [{track}] {system}/{label}#{idx}: link={g['link']} "
                  f"match={g.get('match')} votes={g.get('votes')}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(one, jobs))
    save(f"grades_{track}.json", grades)
