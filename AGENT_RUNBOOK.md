# PriceBench — Agent Runbook

This file is written for a **coding agent** (Claude Code, Cursor, Codex, or
similar). If you are an agent: follow it as your operating procedure for
running PriceBench. If you are a human: point your agent at this file
("read AGENT_RUNBOOK.md and run PriceBench with me") — the run has several
steps that genuinely need a human, and this document tells the agent when
to stop and hand control to you.

PriceBench measures how well shopping agents find cheaper prices for real
products. A full run has five phases: **source tasks → run agents → judge →
verify → report**. The agent can drive everything except a few human gates,
which are marked ⛔ below.

---

## Prerequisites

- Python ≥ 3.10, `pip install -r requirements.txt`.
- A `.env` (or exported env vars) with keys for the columns you intend to
  run — each key enables one column; missing keys simply skip that column:

| Env var | Enables |
|---|---|
| `FINDCHEAP_KEY` | the Findcheap column (`/search`, `/extract`) |
| `ANTHROPIC_API_KEY` | the Claude agent column AND the Claude judge seat |
| `OPENAI_API_KEY` | the GPT agent column AND the GPT judge seat |
| `GEMINI_API_KEY` | the Gemini agent column AND the Gemini judge seat |
| `PERPLEXITY_API_KEY` | the Perplexity column |

⛔ **Spend gate:** a full 100-task, five-column, two-track run costs real
money (order of $400–500 at 2026 prices, dominated by the frontier-model
columns; judging adds ~$50). **State the estimate and get the human's
explicit go before the first paid call**, and again before scaling past
the checkpoint (below). Never run columns the human didn't ask for.

## Phase 1 — sourcing (`source_tasks.py`)

Tasks are sourced mechanically (README §Task sourcing). Configure the
retailer×category grid in `cells.json` (or use the built-in cells), then:

```bash
PRICEBENCH_DIR=<run> python source_tasks.py --all     # resume-safe batch
```

Babysit it: transient fetch failures retry once automatically; a cell that
ends `exhausted` needs a deeper list. ⛔ **Bot-walled pages:** when a list
or product page defeats every fetcher, ask the human to open it in a
browser and save it (File → Save Page As → **"Webpage, Complete"** — the
"HTML Only" mode saves pre-JavaScript source and is useless for
client-rendered stores). Extraction then runs mechanically on the saved
DOM. Batch these requests — give the human one list of links with exact
filenames, not one ask at a time.

Freshness matters: reference prices freeze at collection, so plan to run
the searches the same day sourcing completes.

## Phase 2 — reference shipping (⛔ human gate)

```bash
PRICEBENCH_DIR=<run> python run.py --ref-shipping
```

Deterministic cases stamp from the retailer policy table; the remainder is
written to `ref_shipping_todo.json`. ⛔ Hand the human the todo as a
clickable list; they check each store's shipping to the published buyer
ZIP (10001), logged out / no memberships (for Amazon: a non-Prime account
at checkout preview). Write their values into the todo file and re-run the
flag until it prints **COMPLETE**. Searches must not start before that.

## Phase 3 — searches

Run a small slice first, grade it, and show the human before spending on
the rest:

```bash
PRICEBENCH_DIR=<run> python run.py --run all --first 10     # checkpoint
PRICEBENCH_DIR=<run> python run.py --grade --first 10
# ⛔ show per-column result counts, costs, errors, grade sanity — WAIT for approval
PRICEBENCH_DIR=<run> python run.py --run all --range 11-100
```

Operational rules:
- A column that ends `DONE WITH ERRORS` prints its own completion command
  (`--run <system> --track <t> --retry-errors`) — that re-runs ONLY the
  errored/missing tasks. Never re-run tasks that succeeded (the protocol
  is one invocation per task).
- The Claude column uses the Message Batches API by default
  (`CLAUDE_BATCH=0` forces interactive); batch rounds can take tens of
  minutes — that's normal.
- On a vendor outage: let other columns finish, tell the human what to top
  up, and complete later with `--retry-errors`. Note the completion time —
  it belongs in the freshness disclosure.

## Phase 4 — judging and audits

```bash
PRICEBENCH_DIR=<run> python run.py --grade      # 3-judge panel; incremental —
                                                # already-graded tasks are skipped
PRICEBENCH_DIR=<run> python run.py --audit      # link/price/stock verification
```

Grading a full run means thousands of judge calls; run it while later
search waves are still going if you like (it only reads finished columns).
Verdicts freeze at first determination — re-running is safe and cheap.

## Phase 5 — human review (⛔ human gate)

```bash
PRICEBENCH_DIR=<run> python run.py --review-serve
# then send the human to http://127.0.0.1:8799/verify_exact.html and /verify_equiv.html
```

Brief the human on the protocol before they start:
- Work each task **top-down** per system (rows are sorted best-first) and
  **stop at the first PASS** — later rows may stay unlabeled.
- PASS = live, in stock, really that price, genuinely qualifies for the
  track, still cheaper after mandatory shipping.
- Their ruling overrules the judge panel in both directions; a **reason
  is required only when a ruling overturns the panel's verdict**.
- Price/shipping/count fields are editable; corrections flow into scoring
  even without a pass/fail click. Reference-level corrections are shared
  across both tracks automatically.
- Everything autosaves; they can stop and resume freely.

## Phase 6 — report

```bash
PRICEBENCH_DIR=<run> python run.py --report            # human labels govern;
                                                       # panel fallback where unlabeled
PRICEBENCH_DIR=<run> python run.py --report --prime    # all-Amazon-ships-free lens
```

Give the human both tables side by side, the per-column spend versus your
opening estimate, and the disclosures: freshness window, count of
human-adjudicated rows, and any `temporarily-unverifiable` results.

## When in doubt

Anything that spends money, publishes data, or overwrites a frozen
artifact: stop and ask the human. Everything else — retries, babysitting,
building UIs, computing reports — is yours.
