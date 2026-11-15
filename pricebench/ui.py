"""The human-verification page (verify_<track>.html) builder and the
--review-serve localhost review server (`python -m pricebench.ui --review-serve`)."""

from __future__ import annotations

import json
import re
import time

from .common import OUT_DIR, SYSTEMS, TRACKS, load, load_tasks
from .agents import is_fashion
from .verification import (load_labels, load_ref_audit,
                           policy_shipping, effective_price,
                           effective_shipping, effective_count,
                           USED_CONDITION_RE, SHIPPING_POLICIES,
                           AMAZON_NONPRIME_DEFAULT, _is_amazon,
                           REF_FIELDS, load_shared_refs)

# Review-time default for unknown-shipping Amazon candidates under $35:
# the standard non-Prime fee. Pre-filled and EDITABLE in the verify UI
# (not a locked policy — "almost always" isn't always); the same default
# AMAZON_NONPRIME_DEFAULT, _is_amazon, REF_FIELDS, load_shared_refs come
# from verification (single home; load_labels already applies the shared
# ref overlay and the reasons/ref_counts defaults).


def load_review_labels(track: str) -> dict:
    return load_labels(track)


# ------------------------------------------------------------ page builder


def build_verify_ui(track: str) -> None:
    """Self-contained page for the human-verification pass: EVERY returned
    candidate is a row (nothing filtered, so false "cheaper" claims stay
    visible), cheapest-landed first per system, with editable
    prices/shipping/counts, pass/fail labels, and per-row reason codes.
    Rulings autosave through --review-serve; Export remains a fallback."""
    tasks = load_tasks()
    grades = load(f"grades_{track}.json")
    refs = load_ref_audit(track)
    human = load_review_labels(track)  # rebuilds must round-trip saved work
    raws = {s: load(f"raw_{s}_{track}.json")["tasks"] for s in SYSTEMS
            if (OUT_DIR / f"raw_{s}_{track}.json").exists()}
    data = []
    for t in tasks:
        spec = ("IDENTICAL: same brand, model/style, per-item size/capacity; colorway OK; "
                "any pack/unit COUNT of the identical item OK (scored per unit); "
                "no minis, other capacities, other models, or replicas."
                if track == "exact" else
                "SUBSTITUTE (typical-shopper standard): same purpose and product type, "
                "preserves the important functional characteristics, and not cheaper "
                "mainly via lower quality / smaller quantity / incomplete / "
                "counterfeit / used.")
        if is_fashion(t):
            spec += (" FASHION: appearance is essential — must match style, "
                     "color/pattern, and silhouette (size is a variant).")
        ref_price = human["ref_prices"].get(t["label"], t["price"])
        ref_pol = policy_shipping(t["url"], ref_price)
        h_ref_ship = human["ref_shipping"].get(t["label"])
        entry = {"label": t["label"], "title": t["title"], "image": t["image"],
                 "url": t["url"], "price": ref_price, "spec": spec,
                 # mirrors ref_landed precedence: policy > pre-run stamp
                 # (the --ref-shipping pass) > review edit > audit
                 "ref_ship": ref_pol if ref_pol is not None
                             else t.get("ref_shipping") if t.get("ref_shipping") is not None
                             else h_ref_ship if h_ref_ship is not None
                             else (refs.get(t["label"]) or {}).get("mandatory_shipping"),
                 "ref_locked": ref_pol is not None,
                 # review-time ref-count correction outranks the frozen value
                 "ref_count": int(human.get("ref_counts", {}).get(t["label"]) or
                                  t.get("pack_count") or 1),
                 "candidates": []}
        for s, raw in raws.items():
            cands = []
            # EVERY returned result is a row. Red = non-qualifying for ANY
            # reason — hard facts (dead link, out of stock, used
            # condition), panel rejection, or landed >= reference (that
            # one recomputed live in JS as prices/shipping/ref are edited).
            if t["label"] not in raw:
                continue  # system didn't run this task
            for idx, cand in enumerate(raw[t["label"]]["results"]):
                g = grades.get(s, {}).get(t["label"], {}).get(str(idx), {})
                a = g.get("page_audit") or {}
                hard = []
                if g.get("link") == "dead":
                    hard.append("DEAD LINK")
                if a.get("in_stock") is False:
                    hard.append("AUDIT: OUT OF STOCK")
                cond = g.get("condition")
                if cond and USED_CONDITION_RE.search(cond) and "open box" not in cond.lower():
                    hard.append(f"CONDITION: {cond}")
                # Per-judge verdicts, leading with the side that DECIDED
                # the outcome (rejecter's reason when rejected, etc.)
                panel = g.get("panel") or {}
                marks = " ".join(f"{j}:{'✓' if v.get('accept') else '✗' if 'accept' in v else '!'}"
                                 for j, v in panel.items())
                decided = not g.get("match")
                reason = next((v.get("reason", "") for v in panel.values()
                               if v.get("accept") is not decided and "reason" in v), "") \
                    or next((v.get("reason", "") for v in panel.values() if "reason" in v), "")
                verdict = ("PANEL ACCEPTED" if g.get("match")
                           else "PANEL REJECTED") + f" {g.get('votes', '?')} [{marks}]"
                if cond:
                    verdict = f"condition: {cond} · {verdict}"
                if hard:
                    verdict = " · ".join(hard) + " · " + verdict
                if a.get("in_stock") is not False and a.get("displayed_price") is not None \
                        and abs(a["displayed_price"] - cand["price"]) > 0.02:
                    reason = f"AUDIT: page shows ${a['displayed_price']:.2f} · " + reason
                key = f"{track}|{s}|{t['label']}|{idx}"
                h_price = human["prices"].get(key)
                h_ship = human["shipping"].get(key)
                price = h_price if h_price is not None else effective_price(cand, g)
                pol = policy_shipping(cand.get("url"), price)
                ship = pol if pol is not None else \
                    h_ship if h_ship is not None else effective_shipping(cand, g)
                ship_default = None
                if ship is None and _is_amazon(cand.get("url")) and price < 35.0:
                    # unknown-shipping Amazon items under $35 pre-fill the
                    # standard non-Prime fee — EDITABLE, not locked; same
                    # basis as the pre-run-stamped Amazon refs
                    ship = AMAZON_NONPRIME_DEFAULT
                    ship_default = True
                rc = int(human.get("ref_counts", {}).get(t["label"]) or
                         t.get("pack_count") or 1)
                count = human["counts"].get(key) or effective_count(cand, g, rc)
                cands.append({"key": key, "system": s, "locked": pol is not None,
                              "count": count,
                              "title": cand["title"],
                              "price": price, "human": human["labels"].get(key),
                              "ship": ship, "landed": price + (ship or 0.0),
                              "hard": bool(hard),
                              "prej": not g.get("match"),
                              "rejected": bool(hard) or not g.get("match"),
                              "ship_src": "policy" if pol is not None
                                          else "non-Prime default" if ship_default
                                          else ("agent" if cand.get("shipping") is not None else None),
                              "retailer": cand["retailer"], "url": cand.get("url"),
                              "image": cand.get("image"),
                              "reason": f"{verdict} · {reason}"})
            # per-unit order for multipack refs; TOTAL order for single-item
            # refs (counts don't discount a one-item want)
            rc0 = int(human.get("ref_counts", {}).get(t["label"]) or
                      t.get("pack_count") or 1)
            cands.sort(key=lambda c: c["landed"] / (c["count"] if rc0 > 1 else 1))
            entry["candidates"] += cands  # every returned result — no filter, no cap
        data.append(entry)

    page = """<!doctype html><meta charset="utf-8"><title>PriceBench verify — TRACK</title>
<style>
 body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:24px;background:#faf8f3;color:#1a2b23}
 .task{border:1px solid #ddd;background:#fff;margin-bottom:22px;padding:14px;border-radius:6px}
 .ref{display:flex;gap:12px;margin-bottom:8px}.ref img{width:90px;height:90px;object-fit:contain}
 .spec{font-size:12px;color:#556;background:#f1efe8;padding:6px 8px;border-radius:4px;margin-bottom:8px}
 .cand{display:flex;gap:10px;align-items:center;border-top:1px solid #eee;padding:7px 0}
 .cand.rej{background:#fdf3f2}
 .agent{flex:0 0 68px;text-align:center;font-size:11px;font-weight:700;color:#fff;
        border-radius:4px;padding:4px 0}
 .agent-findcheap{background:#1b5e3f}
 .agent-claude{background:#d97757}
 .agent-openai{background:#10a37f}
 .agent-gemini{background:#4285f4}
 .agent-perplexity{background:#20808d}
 .agent-perplexity_xhigh{background:#0e5560}
 .agent-exa{background:#1f40ed}
 .agent-parallel{background:#b45309}
 .cand img{width:56px;height:56px;object-fit:contain}
 .meta{flex:1}.meta .t{font-weight:500}.meta .m{font-size:12px;color:#556}
 button{padding:4px 12px;border-radius:4px;border:1px solid #bbb;cursor:pointer;background:#fff}
 .pass.on{background:#1b7f4d;color:#fff;border-color:#1b7f4d}
 .fail.on{background:#c0392b;color:#fff;border-color:#c0392b}
 .rsn{margin-left:6px;font-size:12px;max-width:230px}
 .rnote{margin-left:4px;font-size:12px;width:150px}
 #savebar{position:sticky;top:0;z-index:5;background:#fffbe8;border:1px solid #e6d9a8;
   padding:6px 12px;margin:8px 0;font-size:13px}
 input.rp{width:90px} .done{color:#1b7f4d;font-weight:600}
 #export{width:100%;height:140px;font:12px monospace}
 .hint{color:#667;font-size:13px;margin-bottom:14px}
</style>
<h2>PriceBench human verification — TRACK track</h2>
<div class="hint">For each task: confirm the reference price is still current (edit if not),
then verify candidates <b>top-down (sorted cheapest-first per system, by LANDED price —
item + known shipping)</b>. Price basis is the STANDING price — if a page shows you a
first-order "welcome deal" (AliExpress/Temu/Shein show these to logged-out visitors),
enter the regular price instead. Every live cheaper candidate is shown, including ones the
judge panel REJECTED (tinted red) — your pass/fail overrules the panel in either
direction, so a red row you believe qualifies gets a PASS. PASS = live, in stock, really
this price, genuinely qualifies for the track (new — not used/refurbished/for-parts;
open-box OK), and still cheaper after mandatory shipping. <b>FAIL any listing whose
shipping fee erases the savings</b> (check the page when the row says "ship ?"). Stop at each
system's FIRST PASS — later rows can stay unlabeled. If you end up passing several rows for
one system (live prices shifted while checking), the lowest verified total wins automatically.
Rulings <b>autosave to disk</b> as you go (watch the saved-indicator top right); reopening
this page restores your work. Pick a <b>reason</b> only when your ruling OVERTURNS the LLM
judges' verdict (a PASS on a red-tinted row, or a FAIL on a panel-accepted one) — reasons are
recorded with the label for auditability; rulings that agree with the panel need none. The
Export button remains as a fallback if autosave shows OFFLINE.</div>
<div id="savebar">autosave: <span id="savest">idle</span></div>
<div id="tasks"></div>
<button onclick="doExport()" style="font-size:16px;padding:8px 20px">Export labels JSON (fallback)</button>
<textarea id="export" placeholder="exported JSON appears here"></textarea>
<script>
const DATA = __DATA__;
const SAVED = __SAVED__;  // previously saved labels file — round-tripped
// Retailer shipping policies, mirrored client-side so the policy lock
// RECOMPUTES when a price is corrected across the free-shipping
// threshold. Matches SHIPPING_POLICIES + the Amazon non-Prime default;
// scoring derives identically server-side.
const POLICIES = __POLICIES__;
const AMZ_DEFAULT = __AMZ_DEFAULT__;
function policyShip(url, price) {
  if (!url || !url.includes('://')) return null;
  const host = url.split('/')[2].toLowerCase().replace(/^www\./, '');
  for (const [ph, rule] of Object.entries(POLICIES)) {
    if (host === ph || host.endsWith('.' + ph))
      return price >= rule.free_over ? 0 : rule.under_fee;
  }
  return null;
}
function isAmazon(url) {
  if (!url || !url.includes('://')) return false;
  const host = url.split('/')[2].toLowerCase().replace(/^www\./, '');
  return host === 'amazon.com' || host.endsWith('.amazon.com');
}
const labels = SAVED.labels || {}, refPrices = SAVED.ref_prices || {},
      ships = SAVED.shipping || {}, prices = SAVED.prices || {},
      refShips = SAVED.ref_shipping || {}, counts = SAVED.counts || {},
      reasons = SAVED.reasons || {}, refCounts = SAVED.ref_counts || {};
const REASONS = [
  ["", "reason…"],
  ["oos", "Item is out of stock"],
  ["link", "Link broken / different product"],
  ["valid", "Valid match (judges lacked info)"],
  ["invalid", "Not a valid match (judges lacked info)"],
  ["other", "Other (note)"],
];
function reasonSel(key) {
  const cur = (reasons[key] || {}).code || "";
  const note = (reasons[key] || {}).note || "";
  const opts = REASONS.map(([v, t]) =>
    `<option value="${v}"${v === cur ? " selected" : ""}>${t}</option>`).join("");
  return `<select class="rsn" onchange="setReason('${key}', this.value)">${opts}</select>` +
         `<input class="rnote" placeholder="note" value="${note.replace(/"/g,'&quot;')}"
           style="${cur === 'other' ? '' : 'display:none'}"
           onchange="setNote('${key}', this.value)">`;
}
const root = document.getElementById('tasks');
for (const t of DATA) {
  // saved review corrections win over the frozen price (don't clobber)
  if (!(t.label in refPrices) || refPrices[t.label] == null) refPrices[t.label] = t.price;
  const div = document.createElement('div'); div.className = 'task';
  div.innerHTML = `<div class="ref"><img src="${t.image}"><div>
    <b>${t.title.slice(0,110)}</b><br>
    ref price $<input class="rp" id="rp_${t.label}" type="number" step="0.01" value="${refPrices[t.label] ?? t.price}"
      onchange="refPrices['${t.label}']=parseFloat(this.value);updRef('${t.label}')">
    + ref ship $<input class="rp" id="rs_${t.label}" type="number" step="0.01" min="0"
      value="${t.ref_ship === null ? '' : t.ref_ship}" placeholder="?" ${t.ref_locked ? 'disabled title="retailer policy — locked"' : ''}
      onchange="refShips['${t.label}']=this.value===''?null:parseFloat(this.value);updRef('${t.label}')">${t.ref_locked ? '<i>(policy)</i>' : ''}
    &nbsp;× ref count <input class="rp" id="rc_${t.label}" type="number" step="1" min="1"
      value="${refCounts[t.label] || t.ref_count}" style="width:52px"
      title="units in the reference pack — correct here when the freeze missed it"
      onchange="refCounts['${t.label}']=parseInt(this.value)||1;updRef('${t.label}')">
    = <b id="rt_${t.label}"></b>
    &nbsp;<a href="${t.url}" target="_blank">open product page</a></div></div>
    <div class="spec">${t.spec}</div>`;
  for (const c of t.candidates) {
    const row = document.createElement('div');
    row.className = 'cand' + (c.rejected ? ' rej' : '');
    row.id = 'row_' + c.key;
    row.dataset.hard = c.hard ? '1' : '';
    row.dataset.prej = c.prej ? '1' : '';
    const shipVal = c.ship === null ? '' : c.ship;
    const shipTag = c.ship === null ? '<b style="color:#c0392b">ship ?</b>'
                                    : `ship (${c.ship_src})`;
    const agentName = {findcheap: 'Findcheap', claude: 'Opus 5', openai: 'GPT', gemini: 'Gemini', perplexity: 'Perplexity'}[c.system] || c.system;
    row.innerHTML = `<span class="agent agent-${c.system}">${agentName}</span>
      ${c.image ? `<img src="${c.image}">` : '<div style="width:56px"></div>'}
      <div class="meta"><div class="t">${c.title.slice(0,90)}</div>
      <div class="m">item $<input class="sp" id="p_${c.key}" type="number" step="0.01" min="0"
          value="${c.price.toFixed(2)}"
          onchange="prices['${c.key}']=this.value===''?null:parseFloat(this.value);upd('${c.key}')">
        + ${shipTag}
        $<input class="sp" id="s_${c.key}" type="number" step="0.01" min="0" value="${shipVal}"
          placeholder="?" ${c.locked ? 'disabled title="retailer policy — locked"' : ''}
          onchange="ships['${c.key}']=this.value===''?null:parseFloat(this.value);upd('${c.key}')">
        &nbsp;× count <input class="sp" id="c_${c.key}" type="number" step="1"
          min="1" value="${c.count}" style="width:52px"
          onchange="counts['${c.key}']=this.value===''?null:parseInt(this.value);upd('${c.key}')">
        = <b id="t_${c.key}"></b> <span id="f_${c.key}" style="color:#c0392b;font-weight:600"></span>
        · ${c.system} · ${c.url ?
        `<a href="${c.url}" target="_blank">${c.retailer}</a>` : c.retailer}
        · <i>${c.reason}</i></div></div>
      <button class="pass${c.human === 'pass' ? ' on' : ''}" onclick="mark(this,'${c.key}','pass')">pass</button>
      <button class="fail${c.human === 'fail' ? ' on' : ''}" onclick="mark(this,'${c.key}','fail')">fail</button>
      ${reasonSel(c.key)}`;
    div.appendChild(row);
  }
  if (!t.candidates.length) div.innerHTML += '<div class="m">no judge-passing cheaper candidates</div>';
  root.appendChild(div);
}
function mark(btn, key, v) {
  labels[key] = v;
  for (const b of btn.parentElement.querySelectorAll('button')) b.classList.remove('on');
  btn.classList.add('on');
  queueSave();
}
function setReason(key, code) {
  reasons[key] = Object.assign(reasons[key] || {}, {code});
  const note = event.target.nextElementSibling;
  if (note) note.style.display = code === 'other' ? '' : 'none';
  queueSave();
}
function setNote(key, text) {
  reasons[key] = Object.assign(reasons[key] || {}, {note: text});
  queueSave();
}
function blob() {
  return {labels, ref_prices: refPrices, ref_shipping: refShips,
          shipping: ships, prices, counts, reasons, ref_counts: refCounts};
}
let saveTimer = null;
function queueSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, 800);
}
async function doSave() {
  const st = document.getElementById('savest');
  const body = JSON.stringify(blob(), null, 1);
  try { localStorage.setItem('pb_TRACK_backup', body); } catch (e) {}
  if (location.protocol.startsWith('http')) {
    try {
      const r = await fetch('/save/TRACK', {method: 'POST', body});
      st.textContent = r.ok ? 'saved ' + new Date().toLocaleTimeString() : 'SAVE FAILED';
      st.style.color = r.ok ? '#1a7a2e' : '#b33';
      return;
    } catch (e) {}
  }
  st.textContent = 'OFFLINE — use Export (backup kept in this browser)';
  st.style.color = '#b33';
}
// every price/shipping/count input change also autosaves
document.addEventListener('change', () => queueSave(), true);
function totalText(priceEl, shipEl) {
  const p = parseFloat(priceEl.value) || 0;
  const raw = shipEl.value;
  const s = raw === '' ? null : (parseFloat(raw) || 0);
  return '$' + (p + (s || 0)).toFixed(2) + (s === null ? ' (+ ship?)' : '');
}
function unitFmt(v) {  // sub-cent unit prices need real precision
  return '$' + (v < 0.1 ? v.toFixed(4) : v.toFixed(2));
}
function totalVal(priceEl, shipEl) {
  return (parseFloat(priceEl.value) || 0) + (parseFloat(shipEl.value) || 0);
}
const CAND = {};
for (const t of DATA) for (const c of t.candidates) CAND[c.key] = c;
function applyPolicyLock(sEl, url, price, store, storeKey) {
  // live policy lock: crossing a free-shipping threshold re-derives the
  // policy value; leaving policy coverage unlocks the field (amazon<35
  // pre-fills the non-Prime default, still editable)
  const pol = policyShip(url, price);
  if (pol !== null && pol !== undefined) {
    if (!sEl.disabled || parseFloat(sEl.value) !== pol) {
      sEl.value = pol; store[storeKey] = pol;
    }
    sEl.disabled = true; sEl.title = 'retailer policy — locked';
  } else if (sEl.disabled) {
    sEl.disabled = false; sEl.title = '';
    const d = isAmazon(url) && price < 35 ? AMZ_DEFAULT : '';
    sEl.value = d; store[storeKey] = d === '' ? null : d;
  }
}
function upd(key) {
  const pEl = document.getElementById('p_' + key), sEl = document.getElementById('s_' + key);
  const label = key.split('|')[2];
  const task = DATA.find(t => t.label === label);
  const refCount = refCounts[label] || (task ? task.ref_count : 1);
  const cand = CAND[key];
  if (cand && cand.url) applyPolicyLock(sEl, cand.url, parseFloat(pEl.value) || 0, ships, key);
  const cEl = document.getElementById('c_' + key);
  const count = cEl ? (parseInt(cEl.value) || refCount) : 1;
  const total = totalVal(pEl, sEl);
  // single-item reference -> compare on TOTAL landed price (a cheaper
  // per-unit multipack is NOT a cheaper way to buy one item); per-unit
  // math applies only when the reference itself is a multipack
  const unit = refCount > 1 ? total / count : total;
  if (cEl) {
    cEl.disabled = refCount === 1;
    cEl.title = refCount === 1 ? 'ignored — single-item reference compares on total price' : '';
  }
  // per-unit comparison against the (editable) per-unit reference
  const refTot = totalVal(document.getElementById('rp_' + label),
                          document.getElementById('rs_' + label));
  const refUnit = refTot / refCount;
  // candidate count defaults track the (possibly corrected) ref count
  // only when the judge emitted none
  document.getElementById('t_' + key).textContent = totalText(pEl, sEl) +
    (refCount > 1 ? ` (${unitFmt(unit)}/ct vs ref ${unitFmt(refUnit)}/ct)` : '');
  const over = unit >= refUnit - 0.005 / refCount;
  document.getElementById('f_' + key).textContent =
    over ? (refCount > 1 ? 'NOT CHEAPER per count' : 'NOT CHEAPER than reference') : '';
  const row = document.getElementById('row_' + key);
  row.classList.toggle('rej', over || row.dataset.hard === '1' || row.dataset.prej === '1');
}
function updRef(label) {
  const task = DATA.find(t => t.label === label);
  if (task && task.url) applyPolicyLock(
    document.getElementById('rs_' + label), task.url,
    parseFloat(document.getElementById('rp_' + label).value) || 0, refShips, label);
  const rc = refCounts[label] || (task ? task.ref_count : 1);
  const rTot = totalVal(document.getElementById('rp_' + label),
                        document.getElementById('rs_' + label));
  document.getElementById('rt_' + label).textContent =
    totalText(document.getElementById('rp_' + label), document.getElementById('rs_' + label)) +
    (rc > 1 ? ` (${unitFmt(rTot / rc)}/ct)` : '');
  if (task) for (const c of task.candidates) upd(c.key);  // ref edits move every flag
}
// initial totals + flags
for (const t of DATA) updRef(t.label);
function doExport() {
  document.getElementById('export').value = JSON.stringify(blob(), null, 1);
}
</script>"""
    page = page.replace("TRACK", track).replace("__DATA__", json.dumps(data)) \
               .replace("__SAVED__", json.dumps(human)) \
               .replace("__POLICIES__", json.dumps(SHIPPING_POLICIES)) \
               .replace("__AMZ_DEFAULT__", json.dumps(AMAZON_NONPRIME_DEFAULT))
    (OUT_DIR / f"verify_{track}.html").write_text(page)
    n = sum(len(e["candidates"]) for e in data)
    print(f"  wrote {OUT_DIR / f'verify_{track}.html'} ({n} candidates queued)")


# ------------------------------------------------------------ review server


def review_serve(port: int = 8799) -> None:
    """--review-serve: localhost server for the verify UIs.
    GET /verify_<track>.html rebuilds the page from the saved labels file
    (work-in-progress round-trips on every reload); POST /save/<track>
    writes verify_labels_<track>.json atomically — the page autosaves every
    ruling, so nothing is lost and no export/paste step is needed."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console readable
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            m = re.match(r"^/verify_(\w+)\.html$", path)
            if m and m.group(1) in TRACKS:
                track = m.group(1)
                build_verify_ui(track)  # bakes current saved labels back in
                body = (OUT_DIR / f"verify_{track}.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self):
            m = re.match(r"^/save/(\w+)$", self.path)
            if not m or m.group(1) not in TRACKS:
                self.send_error(404); return
            track = m.group(1)
            try:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                data = json.loads(raw)
                assert isinstance(data.get("labels"), dict)
            except Exception:
                self.send_error(400); return
            tmp = OUT_DIR / f"verify_labels_{track}.json.tmp"
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(OUT_DIR / f"verify_labels_{track}.json")
            # Promote ref-level corrections into the SHARED refs store
            # (refs are identical across tracks). ref_prices arrive as a
            # full page snapshot, so only values that DIFFER from the
            # frozen task price are promoted (a stale open tab can then
            # never clobber the other track's edits with defaults);
            # shipping/count dicts are edit-only and merge directly.
            frozen = {t["label"]: t["price"] for t in load_tasks()}
            shared = load_shared_refs()
            for lab, v in (data.get("ref_prices") or {}).items():
                if v is not None and abs(v - frozen.get(lab, v)) > 0.005:
                    shared["ref_prices"][lab] = v
            for k in ("ref_shipping", "ref_counts"):
                shared[k].update(data.get(k) or {})
            rtmp = OUT_DIR / "verify_labels_refs.json.tmp"
            rtmp.write_text(json.dumps(shared, indent=1))
            rtmp.replace(OUT_DIR / "verify_labels_refs.json")
            n = len([v for v in data["labels"].values() if v])
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            print(f"\r[review] {track}: {n} rulings saved "
                  f"({time.strftime('%H:%M:%S')})", end="", flush=True)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    pages = " and ".join(f"/verify_{t}.html" for t in TRACKS)
    print(f"[review] serving http://127.0.0.1:{port} — {pages} — Ctrl-C to "
          f"stop; labels autosave to {OUT_DIR}/verify_labels_<track>.json")
    srv.serve_forever()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="verify-UI builder / review server (see run.py for the full CLI)")
    ap.add_argument("--review-serve", action="store_true",
                    help="serve the verify UIs on localhost with autosave")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--track", choices=[*TRACKS, "both"], default="both",
                    help="with no --review-serve: build verify_<track>.html once")
    args = ap.parse_args()
    if args.review_serve:
        review_serve(args.port)
    else:
        for _t in (TRACKS if args.track == "both" else [args.track]):
            build_verify_ui(_t)
