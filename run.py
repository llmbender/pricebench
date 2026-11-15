"""PriceBench — a two-track benchmark for AI shopping-search agents.

Given a product page (title, image, URL, reference price), each agent
must find real, in-stock, NEW listings that are cheaper on LANDED cost
(item price + mandatory shipping, single item, US). Two tracks over the
same products, run separately:

  exact : find the IDENTICAL product cheaper (same brand/model and
          per-item size/capacity; pack QUANTITY is nonessential and
          scored per unit; colorway differences OK except fashion).
  equiv : identical OR a functionally-equivalent substitute of
          comparable quality (appearance matters for fashion).

INFORMATION SYMMETRY: every agent receives the same prompt text and the
same product facts — nothing task-specific about grading is ever shown.

GRADING: one universal judge prompt per track (published verbatim in
this file), evaluated by a 3-lab panel — claude-opus-5, gpt-5.6-sol,
gemini-3.5-flash — majority vote, judges blind to which agent produced
each candidate and to its price/retailer. Condition is passed as data,
never guessed from photos.

GROUND TRUTH: pooled, TREC-style. The oracle for a task is the cheapest
VERIFIED qualifying listing any agent found (pooling bias disclosed).
Automated verification runs through the Findcheap /extract endpoint
(liveness, standing price, stock) plus a locked retailer shipping-policy
table; a human verification pass (the verify UI) resolves the remainder
and outranks everything. Human-adjudicated counts are reported.

METRICS per track (savings capture is the headline). Opportunity_i =
max(ref - oracle, 0); performance metrics are computed ONLY over tasks
with Opportunity > 0 — an agent is never scored on savings that did not
exist. Headline aggregation: MEAN with a 95% bootstrap CI.
  capture     = Savings_i / Opportunity_i, Savings_i = max(ref - best, 0)
  found       = Savings_i / ref (opportunity tasks)
  opp_success = % of opportunity tasks with >=1 verified cheaper listing
  recall      = % of opportunity tasks where agent_best <= oracle + $0.01
  match_acc   = % of ALL returned listings that qualify for the track
No-opportunity tasks are reported separately (clean vs false claims).
Latency and cost are reported separately, never blended into scores.
Execution rules are frozen in PROTOCOL and stamped into artifacts.

Usage (see README.md for setup and the full walkthrough):
    python run.py --ref-shipping                # before any searches
    python run.py --run all --track both
    python run.py --grade --track both
    python run.py --audit --track both
    python run.py --verify-ui --track both      # or --review-serve
    python run.py --report --track both         # add --prime for the lens

Artifacts land in runs/$PRICEBENCH_DIR/: tasks.json (input, gains
ref_shipping stamps), ref_shipping_todo.json (manual remainder),
raw_<system>_<track>.json, grades_<track>.json, verify_<track>.html
(the human-verification UI), verify_labels_<track>.json (saved from the
UI), verify_labels_refs.json (shared cross-track ref corrections),
ref_audit_<track>.json, summary_<track>.json (and
summary_<track>_prime.json under --prime)."""

import argparse
import threading

from pricebench.common import TRACKS, SYSTEMS, FINDCHEAP_URL, load_tasks
from pricebench import common
from pricebench.agents import run_system
from pricebench.judges import grade
from pricebench import verification
from pricebench.verification import audit, ref_shipping_pass
from pricebench.ui import build_verify_ui
from pricebench.scoring import report

# The published lineup "--run all" expands to; SYSTEMS may carry extra
# comparison columns that are run explicitly, never by "all".
RUN_ALL_LINEUP = getattr(common, "RUN_ALL_LINEUP", SYSTEMS)


# ------------------------------------------------------------ main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", choices=[*SYSTEMS, "all"])
    ap.add_argument("--track", choices=[*TRACKS, "both"], default="both")
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--reaudit", action="store_true",
                    help="force re-auditing candidates that already have a verdict")
    ap.add_argument("--audit", action="store_true",
                    help="frontier page audit: stock/price via /extract over the "
                         "cheapest passing candidates (and every reference)")
    ap.add_argument("--only", metavar="LABEL",
                    help="restrict --grade/--audit to one task label")
    ap.add_argument("--first", type=int, metavar="N",
                    help="use only the first N tasks in every phase (R&D speed)")
    ap.add_argument("--systems", metavar="S1,S2",
                    help="restrict --run/--grade/--audit to these systems")
    ap.add_argument("--range", metavar="A-B",
                    help="use tasks A through B (1-indexed, inclusive), e.g. 6-10")
    ap.add_argument("--retry-errors", action="store_true",
                    help="with --run: re-run only tasks missing/errored in the "
                         "existing raw file (vendor-outage completion pass)")
    ap.add_argument("--ref-shipping", action="store_true",
                    help="pause-after-sourcing pass: stamp deterministic ref "
                         "shipping, emit ref_shipping_todo.json for manual values")
    ap.add_argument("--prime", action="store_true",
                    help="with --report: Prime counterfactual (all Amazon "
                         "shipping $0) -> summary_<track>_prime.json")
    ap.add_argument("--verify-ui", action="store_true")
    ap.add_argument("--review-serve", action="store_true",
                    help="serve the verify UIs on localhost with autosave-to-disk")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.first:
        common.TASK_LIMIT = args.first
    if args.range:
        a, b = args.range.split("-")
        common.TASK_RANGE = (int(a), int(b))
    sys_filter = tuple(args.systems.split(",")) if args.systems else None
    tracks = list(TRACKS) if args.track == "both" else [args.track]
    if args.prime:
        if not args.report or args.run or args.grade or args.audit or args.verify_ui:
            ap.error("--prime is a --report-only counterfactual lens; it never "
                     "runs searches, grading, or audits")
        verification.PRIME_MODE = True
    if args.ref_shipping:
        ref_shipping_pass()
        return
    tasks = load_tasks()

    if args.run:
        systems = list(sys_filter or RUN_ALL_LINEUP) if args.run == "all" else [args.run]
        for track in tracks:
            todo = list(systems)
            # Findcheap-first-serially is a LOCAL-DEV protection (a backend
            # sharing this machine's CPU would contend with the harness and
            # skew its published latency). Against the hosted API it
            # overlaps with the other columns — no shared vendors.
            fc_local = "localhost" in FINDCHEAP_URL or "127.0.0.1" in FINDCHEAP_URL
            if "findcheap" in todo and fc_local:
                run_system("findcheap", track, tasks, args.retry_errors)
                todo.remove("findcheap")
            threads = [threading.Thread(target=run_system,
                                        args=(s, track, tasks, args.retry_errors))
                       for s in todo]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
    if args.grade:
        # Incremental: grade() skips (system,label) pairs already present
        # in grades_<track>.json unless --only — verdicts are frozen at
        # first determination.
        for track in tracks:
            grade(track, args.only, sys_filter)
    if args.audit:
        for track in tracks:
            audit(track, args.only, sys_filter, args.reaudit)
    if args.verify_ui:
        for track in tracks:
            build_verify_ui(track)
    if args.review_serve:
        from pricebench.ui import review_serve  # the review server lives in ui.py

        review_serve()
    if args.report:
        for track in tracks:
            report(track)
    if not (args.run or args.grade or args.audit or args.verify_ui
            or args.report or args.review_serve):
        ap.print_help()


if __name__ == "__main__":
    main()
