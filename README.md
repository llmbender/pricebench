# PriceBench

PriceBench is a benchmark for AI product search agents. It provides each agent
with a set of structured product data, and tasks the agent with searching the
internet in real-time to find lower-priced options. This repository includes
everything needed to use PriceBench to evaluate an arbitrary agent, including
harnesses and prompts.

There are a few things that make PriceBench unique:
- It's designed to run with almost no human intervention.
Human attention is only required for verifying product pages,
because a lot of websites block agents from accessing them.
- It doesn't have a fixed set of input tasks. Instead, it sources
the set of 100 input tasks at runtime. This makes it impossible to game the benchmark
by overfitting an agent on a pre-specified set of tasks. It also ensures that tasks
accurately represent the real world, by sourcing fresh product data in real-time.
- Input tasks are sourced based on real-world data. For a benchmark to accurately
reflect the real world, it must include a mix of large and small retailers, and 
different product categories users shop for. For example,
Amazon accounts for about 42% of non-auto e-commerce sales in the US, so 42 of the 100 input tasks
are sourced from Amazon (Statista, "Market share of leading retail e-commerce companies in the
United States in 2025"). Similarly, 16% of input tasks fall under consumer electronics.
You can find a more comprehensive breakdown below.

Before you go any further, two things to know:
1. The easiest way to run PriceBench is to give your favorite coding agent
access to this repo, and tell it: *"read AGENT_RUNBOOK.md and run PriceBench with me"*.
The runbook tells the agent what to do at each phase and when to hand control to you.
2. Running this benchmark requires human verification at some stages, and depending on
how large a sample size you use, this can be very time-consuming. If you just want to know
how your agent performs on PriceBench without the hassle, we're currently offering this as a 
[service.](https://findcheap.ai/pricebench)

## How it works

```
                tasks.json
                    │
                    ▼
        agents (one invocation each)
                    │
                    ▼
          raw_<agent>_<track>.json
                    │
                    ▼
          3-lab judge panel (majority vote, blind)
                    │
                    ▼
   automated verification (link check, /extract audit, shipping policy)
                    │
                    ▼
      human verification pass (verify_<track>.html, published criteria)
                    │
                    ▼
             summary_<track>.json  +  report tables
```


## Task sourcing

**1. Retailer mix** — Input tasks are sourced proportionally to
US e-commerce sales data (Statista, "Market share of leading retail e-commerce companies in the
United States in 2025"). The benchmark covers shippable consumer goods, so motor-vehicle
retailers (Tesla, Carvana) are excluded. The percentage of e-commerce market share that isn't accounted for
by large retailers is referred to as the "long tail" - direct-to-consumer brand storefronts and independent
stores. Here is how this breaks down for input size N=100:

| Retailer | Slots | | Retailer | Slots |
|---|---|---|---|---|
| Amazon | 42 | | Home Depot | 2 |
| Long tail (DTC) | 28 | | Kroger | 2 |
| Walmart | 10 | | Target | 2 |
| Apple | 3 | | Shein | 2 |
| eBay | 3 | | Costco / Best Buy / Chewy / Wayfair | 1 each |
| Temu | 2 | | | |

**2. Category mix** - Product categories are represented proportionally
to the Statista/eMarketer US e-commerce category forecast. Here is how many
tasks are sourced from each category for N=100: grocery 19,
apparel & accessories 19, consumer electronics 16, furniture/home 15,
health & beauty 9, toys/hobby 7, other (office, pet, books/media, auto
parts) 15.

**3. Grid construction** - The retailer and category quotas are crossed
mechanically: single-category retailers are locked to their category
(Chewy → pet, Best Buy → electronics, Wayfair → furniture/home, Kroger →
grocery); generalist retailers' slots fill the remaining category quotas
largest-deficit-first, each cell drawing from that retailer's public
best-seller/top-rated list for the category. For a full run the grid is
built mechanically into `cells.json` (consumed by `source_tasks.py`),
with cell order shuffled under a fixed seed so any checkpoint slice is
representative across retailers and categories.

**4. Long-tail stratification** - The 28 DTC slots split roughly 2:1 between
large and small DTC retailers. 19 slots are filled by walking the Similarweb
"Top Websites - United States" category
rankings top-down (first site passing all filters: a brand's own
storefront selling physical goods in the cell's category; not a
marketplace, aggregator, or coupon site; not individually named on the
market-share chart; ships to the US; one brand, one cell), and the remaining 9
slots come from the Store Leads public store directory,
category-ordered, taking the first qualifying store in the depth band
(worldwide rank 150–500 within the category report). This ensures that
stores are small enough to be genuinely independent, but established enough to have extractable data.

**5. Product selection** — On each cell's source list, take every 3rd
item top-down until one passes all pre-declared exclusions: in stock
and orderable online; price visible; ships to the US; not a gift card,
digital good, or subscription (digital goods are also caught by page
markers, e.g. auto-delivery/download text); not refurbished, used, or
open-box — references must be NEW; brand not already seated in another
cell; product URL not already selected by another cell (cells sharing a
source list never re-pick the same item); mechanically extractable (a
page that resists extraction is skipped and the walk continues). Where a brand splits best sellers by department,
use the department matching the larger share of the cell's category;
category-specific cells use the brand's category-scoped wall.

**6. Mechanical freeze** — The selected URL's title, price, image, and
brand are captured by `source_tasks.py` through the Findcheap `/extract`
endpoint, falling back to the page's own structured data (JSON-LD
Product, else OpenGraph). When a JSON-LD Product node lacks an image,
the page's `og:image` fills it (extraction source `json-ld+og-image`);
hosts that publish neither JSON-LD nor OpenGraph are frozen from other
machine-readable page state (semantic DOM markers, embedded state JSON)
— still the page's own data, never hand-typed values. List pages are
fetched rendered; their ranked order is extracted by an LLM
pass that is order-preserving and blind to prices, with the every-3rd
rule applied in code. Access fallback: when a list or product page is
bot-walled to every fetcher, a human saves the fully rendered page from
a browser ("Webpage, Complete") and the same extraction runs
mechanically on the saved DOM; this fallback is disclosed in the
provenance log.

**7. Provenance** — Every cell's outcome is logged to
`sourcing_log.json`: source-list URL, access date, the ranked items
seen, which were skipped and by which exclusion, and the final
selection. Roster-walk substitutions (a long-tail site that later fails
a filter and is replaced by the next qualifying site) are logged as
provenance amendments.

## Running agents

**8. Agents** - Each task is a frozen product reference: title, image, URL,
   price, pack count. Each agent is provided with the prompt text and product
   facts (title, brand, URL, reference price, and the product image as real
   image input — passed as base64 of the harness-normalized image, since
   retailer image URLs are typically bot-walled to provider-side URL
   fetchers), and must return up to 10 cheaper listings as JSON - landed
   cost (item + mandatory shipping), standing prices only (no coupon or
   first-order "welcome" prices), in-stock, NEW condition. One invocation
   per task; only transport-level retries; provider-default effort. The
   Claude column runs over the Message Batches API — identical
   model/tools/prompts, documented in `PROTOCOL`; batch scheduling makes
   per-task latency unmeasurable, so its latency is reported from
   interactive-run medians. The full frozen protocol is the `PROTOCOL`
   dict in `pricebench/common.py`, stamped into every artifact.

   | Agent | Configuration |
   |---|---|
   | Findcheap | `/search` API as shipped (exact track: `exact_brand=true`) |
   | GPT-5.6 Sol | Responses API `web_search` |
   | Claude Opus 5 | `web_search` + `web_fetch` tools (Message Batches API transport) |
   | Gemini 3.1 Pro | `google_search` grounding |
   | Perplexity | Agent API, `preset: high` (their curated research config) |

**9. Tracks** - there are two tracks that each agent runs separately for each product,
which represent two different modes of searching:
   - **equiv** - the identical product or a functionally equivalent
     substitute of comparable quality.
   - **exact** - the identical product only: same brand, model, and
     per-item size/capacity. Pack quantity is nonessential (a 4-pack of
     the identical item qualifies, scored per unit). Colorway differences
     are acceptable except for fashion products, where color/design must match.

## Judging

**10. Judges** - Each of the two tracks includes a single universal prompt that is used for
all judges and tasks. This can be found in `pricebench/judges.py`. A panel of 3 judges compares each
task completion to the reference product and decides whether the result is valid or invalid. By default,
the judges are `gpt-5.6-sol`, `claude-opus-5`, and `gemini-3.5-flash`. The verdict is determined by
majority vote (whatever conclusion that at least 2 out of 3 judges agree on). When judging, each judge
sees the reference and candidate (titles + images), but is blind to which agent produced the candidate.

**11. Ground truth** - The benchmark uses TREC-style pooling to autonomously determine ground truth.
In other words, all agents search for cheaper product options for a given input, and the cheapest
verified result found by *any* agent is deemed to be the lowest available price (the "oracle" in TREC
terminology) for that product.

**12. Verification** - Reference shipping is resolved BEFORE the searches
run: a locked retailer shipping-policy table first, then automated checks
at the published buyer ZIP (10001), then manual non-member checks by the
reviewer — the resolved value is stamped into `tasks.json`, so the target
agents chase is landed-correct from the start. Candidate verification is
automated-first through the Findcheap
[`/extract`](https://findcheap.ai/developers/docs) endpoint, which verifies the product page
is active (not a dead link), the product is currently in-stock, and what the current item price is,
plus the shipping-policy table; scoring is on landed cost throughout
(item + mandatory shipping, per §8). After every possible means of autonomously verifying results
has been exhausted, verification falls to a human for final review. A UI is automatically
generated for the human to verify task completions (`verify_<track>.html`,
served locally by `python -m pricebench.ui --review-serve` with autosave), and the human follows
the same criteria as the automated verification process. Human entries overrule the automated pass where
they disagree; a reason code is recorded with any ruling that OVERTURNS
the judge panel's verdict (rulings that agree with the panel need none).
Two comparison rules apply in review and scoring alike: multipack
references compare on landed price per unit, while single-item
references compare on TOTAL landed price — a cheaper-per-unit multipack
is not a cheaper way to buy one item. Listings that cannot be verified
at review time (bot-walled to the reviewer, checkout-gated price, page
temporarily down) are recorded as temporarily unverifiable and never
count as verified savings. Primary scoring assumes no paid memberships;
a Prime counterfactual (all Amazon shipping $0) is reported separately
via `--report --prime` (`summary_<track>_prime.json`). The published
N=100 artifacts were produced by an equivalent internal verification
implementation; results are unaffected by the transport.

## Results

**13. Metrics** - Statistics are reported per track based on the following formula:

`Opportunity = max(ref_landed − oracle_landed, 0)`

(per §8, landed = item price + mandatory shipping price)

When the reference is a multipack, prices are compared per unit - for example,
a pack of 6 t-shirts that costs $24 ($4 per shirt) is 20% cheaper than a pack of 8 t-shirts that costs $40 ($5 per shirt).
Single-item references compare on total landed price (per §12) — a
cheaper-per-unit multipack is not a cheaper way to buy one item.

The following metrics are reported:
   - **Savings capture** (primary metric): `Savings / Opportunity`, mean with a
     95% bootstrap CI.
   - **Savings found**: `Savings / reference` - savings as a fraction of the reference price.
   - **Success rate**: % of tasks where the agent returned ≥1 verified
     cheaper qualifying listing.
   - **Best price recall**: % of tasks where the agent's best find
     matched the oracle (within ±1¢ per unit)
   - Latency and cost for each agent are reported separately, and aren't blended into scores. Scores only measure performance on the tasks.

**14. Audit trail** — Our published results come from a
full N=100 run whose complete artifact set ships in
[`runs/n100/`](runs/n100/): the 100 frozen reference products with
sourcing provenance, every agent's raw answers, every judge verdict,
the automated verification records, the human reviewer's rulings (with
reasons wherever a ruling overturned the judge panel), and the final
scorecards. Anyone can verify that the reported scores follow from that
data — recompute every table locally, no API keys or network needed:

```bash
PRICEBENCH_DIR=n100 python run.py --report            # both tracks
PRICEBENCH_DIR=n100 python run.py --report --prime    # Prime counterfactual
```

The scoring is pure local math over the artifacts, so the printed
tables must match the published ones exactly. The listings themselves
were verified live during the run window (timestamps in the artifacts);
prices on the open web will have drifted since.

## Things to watch out for

- **Price drift**: E-commerce is very dynamic, and product pages are
  constantly changing. For this reason, we recommend running the entire
  benchmark on the same day. If extensive time passes between sourcing input
  data and running the agentic search processes, this can skew the results.
- **Retrieving consistent shipping data**: Product price on its own isn't a useful data
  point unless it's accompanied by shipping price. In fact, many online retailers use this
  as a strategy to game SEO/GEO recommendation systems: they price their products substantially
  below market rates, and make up for this by transferring the cost into inflated shipping prices.
  Because it's necessary to account for shipping cost, we hard-coded a buyer ZIP code into this repo
  (10001, which corresponds to the most populous US metropolitan area) so that all shipping prices
  share a single consistent basis. If you use a different buyer location, make sure to keep it
  constant across all tests to avoid biasing the results.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # then fill in keys
```

| Key | Used for |
|---|---|
| `FINDCHEAP_API_KEY` | Findcheap `/search` (agent) + `/extract` (verification) |
| `ANTHROPIC_API_KEY` | Claude agent + judge |
| `OPENAI_API_KEY` | GPT agent + judge |
| `GEMINI_API_KEY` | Gemini agent + judge |
| `PERPLEXITY_API_KEY` | Perplexity agent |
| `FIRECRAWL_API_KEY` | *(optional)* only for `source_tasks.py` list-page fetches |

## Running

> **Running with a coding agent (recommended):** a full run is a multi-phase
> pipeline with several steps that need a human (shipping checks, review
> labeling, spend approvals). The easiest way to run PriceBench is to point
> a coding agent at [`AGENT_RUNBOOK.md`](AGENT_RUNBOOK.md) — e.g. tell it
> *"read AGENT_RUNBOOK.md and run PriceBench with me"*. The runbook tells
> the agent what to do at each phase and when to hand control to you.

```bash
export PRICEBENCH_DIR=run1          # selects runs/run1/ (needs tasks.json)
python run.py --run all --track both        # agent searches
python run.py --grade --track both          # 3-judge panel
python run.py --audit --track both          # /extract verification
python run.py --verify-ui --track both      # builds verify_<track>.html
python run.py --report --track both         # score tables
```

For the human verification pass, serve the verify UIs locally:

```bash
python -m pricebench.ui --review-serve   # http://127.0.0.1:8799/verify_<track>.html
```

Pages rebuild from the saved labels on every load, and rulings autosave
to `verify_labels_<track>.json` (ref-level corrections are shared across
tracks via `verify_labels_refs.json`); re-running `--report` folds them
in. Opening the built `verify_<track>.html` directly still works — the
Export button is the fallback for saving labels by hand. `--systems`, `--first N`,
`--range A-B`, and `--only <label>` scope any phase. `tasks/example_tasks.json`
is a ready 10-product example set.

## Repository layout

```
run.py               # CLI entry point (all phases)
AGENT_RUNBOOK.md     # operating procedure for coding agents (see Running)
pricebench/          # the package
  common.py          #   shared config, frozen PROTOCOL, task loading
  agents.py          #   the five agent adapters + the shared prompt
  judges.py          #   3-lab judge panel + universal judge prompts
  verification.py    #   /extract audits, link checks, shipping policy, qualification
  scoring.py         #   metrics, bootstrap CIs, report tables
  ui.py              #   human-verification page builder + review server
source_tasks.py      # mechanical task sourcing (see Task sourcing above)
tasks/example_tasks.json
runs/                # created per run: raw results, grades, UIs, summaries
runs/n100/           #   the published N=100 run's audit trail (see §14)
```

Note (2026-11-15): in `runs/n100` and `tasks/example_tasks.json`, some link and source fields were normalised. A listing link that is not published reads `null`, and the source tags read `retailer-data`. No price, shipping figure, grade, label or score changed: `python run.py --report` reproduces the published summaries byte for byte.

## License

MIT — see [LICENSE](LICENSE).

## Citation

```bibtex
@misc{pricebench2026,
  title={PriceBench: A Benchmark for AI Product Search Agents},
  author={Carlson, Jacob},
  year={2026},
  url={https://github.com/llmbender/pricebench}
}
```
