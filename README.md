# Actualize

Report the **actual** cost of Azure resources — what you really pay after Enterprise/MCA
discounts, reservations, and savings plans — next to the **retail** (list / pay-as-you-go)
price, with the **percent difference** per line item. Optionally convert a pasted table of USD
cost *reductions* into **actual** savings.

Actualize is a **GitHub Copilot skill** (`SKILL.md`, skill name `actualize`). You talk to
GitHub Copilot in plain language; the skill knows how to pull the right cost data from Azure, when to
run what, and how to present the result — backed by a deterministic, dependency-free Python
calculator (`scripts/actualize.py`) that does the math and can also be run standalone.

## Why it's accurate

Retail and actual are pulled from the **same Cost Management line item**, so the comparison is
apples-to-apples and nothing is guessed:

- **Retail** = `ListCost` (FOCUS) or `payGPrice × quantity`
- **Actual** = `EffectiveCost` / `costInBillingCurrency` / `pretaxCost`
- **Discount %** = `1 − actual ÷ retail`

Comparison is on **amortized cost over a period**, which automatically accounts for quantity,
tiers, reservations, and savings plans — more accurate than reconstructing hourly rates.
Anything not present in the data is reported as `UNKNOWN` / `UNMATCHED`, never invented.

## What the skill adds (beyond the script)

The Python script is a trustworthy **calculator**. The skill is the **operator** around it —
the part that needs Azure judgment a script can't provide:

- **Gets the data for you.** Turns "what are we actually paying?" into the correct Azure Cloud
  Shell command for your billing scope and agreement type, drives the async Cost Details report
  flow (start → poll → download), and handles the Cost Management API's 429 throttling rules.
- **Works on tenants it can't reach.** It emits a copy-paste command you run in **your own**
  Cloud Shell, then ingests the pasted result verbatim — no access to your tenant required.
- **Fills missing retail without guessing.** When an export zeroes out list price, it enriches
  retail from the anonymous Azure Retail Prices API by `meterId`, and refuses to guess when a
  meter maps to several SKUs (e.g. SQL "1 vCore" … "80 vCore") — leaving those `UNKNOWN`.
- **Maps intent to the right operation.** "What do we save if we delete these 3 VMs?" →
  a run-rate delta; a pasted savings table → actual-savings mode.
- **Guards the numbers.** Enforces never-guess, explains every `UNKNOWN`, flags outliers for
  review, and refuses to hand-compute — every figure traces back to data you pasted.

## Using it with GitHub Copilot

You interact in natural language. The skill emits the command to run in **your** tenant, reads
the result you paste back, and presents the answer. The tables below are the tool's real output.

### "What are we actually paying vs list price this month?"

> **You:** For subscription *Web-Prod*, what did we actually pay in June vs list price?
>
> **`actualize`:** I can't reach your tenant from here — run this in **Azure Cloud Shell** (Bash)
> and paste the output back:
> ```bash
> az consumption usage list --start-date 2026-06-01 --end-date 2026-06-30 \
>   --include-meter-details --include-additional-properties -o json
> ```
>
> **You:** *(pastes the JSON)*
>
> **`actualize`:** Here's retail vs actual — a **22.5%** effective discount overall:
>
> | Item | Retail (USD) | Actual (USD) | Savings | Discount % |
> |---|--:|--:|--:|--:|
> | egress-westus | 43.50 | 43.50 | 0.00 | 0.0% |
> | vm-app01 | 71.42 | 42.85 | 28.57 | 40.0% |
> | stproddata01 | 18.40 | 17.00 | 1.40 | 7.6% |
> | **TOTAL** | **133.32** | **103.35** | **29.97** | **22.5%** |
>
> `vm-app01` carries the discount (40%); egress gets none. Want this grouped by service instead?

### "Why does my cost export show no discounts?"

> **You:** *(pastes a Cost Details CSV)* This shows actual cost but no savings — why?
>
> **`actualize`:** Your export has actual cost but the list-price field is `0`, so retail is
> `UNKNOWN`. I'll enrich retail from the public **Azure Retail Prices API** by meter — without
> guessing on meters that map to multiple SKUs. Two meters resolved cleanly; the SQL vCore meter
> is **ambiguous** (it maps to 1–80 vCore at different prices), so I'm leaving it `UNKNOWN`:
>
> | Item | Retail (USD) | Actual (USD) | Discount % | Note |
> |---|--:|--:|--:|---|
> | sql-db01 | UNKNOWN | 100.00 | n/a | ambiguous meter — not guessed |
> | vm-app01 | 71.42 | 42.85 | 40.0% | retail via Retail Prices API |
> | stproddata01 | 18.40 | 17.00 | 7.6% | retail via Retail Prices API |
>
> The blended total is flagged *retail incomplete* because retail covers fewer rows than actual.
> Tell me the SQL SKU (e.g. "8 vCore") and I'll use that exact price to finish the row.

### "Finance sent proposed cuts — what do they actually save?"

> **You:** Here's a list of monthly reductions someone proposed. What's the real saving? *(pastes
> a markdown table)*
>
> **`actualize`:** Those are **retail** cuts; the actual saving depends on each resource's real
> discount, from your cost data:
>
> | Item | Retail reduction | Actual/Retail | Actual reduction | Match basis |
> |---|--:|--:|--:|---|
> | vm-app01 | 71.42 | 60.0% | 42.85 | exact |
> | stproddata01 | 9.20 | 92.4% | 8.50 | exact |
> | some-unmanaged-thing | 50.00 | **UNMATCHED** | UNKNOWN | no match in cost data |
> | **TOTAL** | **80.62** | n/a | **51.35** | *some UNMATCHED — not counted* |
>
> The $71.42 cut on `vm-app01` is worth **$42.85** actual (it's at a 40% discount).
> `some-unmanaged-thing` isn't in your cost data, so I left it out — send me its usage to include it.

### "If we delete these, what's the monthly saving?"

> **You:** What's the run-rate saving if we remove vm1, stor1 and kv1?
>
> **`actualize`:** From their actual spend over the period (normalized to a monthly run-rate):
>
> | Resource | Chg actual (USD) | Proj. change/mo if removed | Status |
> |---|--:|--:|---|
> | vm1 | -60.00 | -608.75 | RUN-RATE |
> | stor1 | -30.00 | -304.38 | RUN-RATE |
> | kv1 | -6.00 | -60.88 | RUN-RATE |
> | **TOTAL** | **-96.00** | **-974.00** | |
>
> About **$974/mo**. Caveat: if any is covered by a reservation or savings plan, the cash saving
> lags until that commitment is reallocated or expires — I won't overstate it. Given a *before*
> and *after* period instead, I'll show the exact change per resource (REMOVED/CHANGED/ADDED).

## Under the hood — the `actualize` calculator

The skill runs a small stdlib-only Python CLI. You can also run it directly; every command is
reproducible from the bundled `samples/` (a short `[info] …` line is printed to stderr).

```bash
# Retail vs actual vs %-difference  (group by resource|meter|service|subscription|row)
python scripts/actualize.py report --input DATA [--group-by resource] [--retail-map MAP] [--format md|csv|json]

# Actual savings from a pasted USD reduction table
python scripts/actualize.py savings --cost DATA --reductions TABLE

# List meterIds whose retail is zeroed/absent (need Retail Prices lookup)
python scripts/actualize.py meters --input DATA

# Fetch retail list prices by meterId (ambiguity-safe, anonymous API)
python scripts/fetch_retail.py --input DATA --out retail-map.csv

# Change in ACTUAL cost for updated/deleted resources (before/after, or run-rate if removed)
python scripts/actualize.py delta --before DATA [--after DATA2] [--resources LIST] [--decreases-only]
```

`DATA` may be FOCUS CSV/JSON, modern/legacy usage-details JSON, or Cost Management `query` output
— the shape is auto-detected. Reproduce the walkthroughs above:

```bash
python scripts/actualize.py report  --input samples/modern-usage.json
python scripts/actualize.py report  --input samples/zeroed-retail.json --retail-map samples/retail-map.csv
python scripts/actualize.py savings --cost samples/modern-usage.json --reductions samples/reductions.md
python scripts/actualize.py delta   --before samples/delta-before.json --after samples/delta-after.json
python scripts/actualize.py delta   --before samples/delta-before.json --decreases-only
```

The exact Azure Cloud Shell commands the skill emits (5 tiers, from `az consumption usage list`
to the async Cost Details report and FOCUS export) are documented in [`SKILL.md`](SKILL.md).

**Currency.** A currency found in the data (`billingCurrency`) is always used; otherwise pass
`--currency`, and if neither is present amounts default to **USD** (a label-only assumption that
never alters a cost). Detected currency wins over the default, and genuinely mixed-currency data is
never coerced to one currency or summed — it stays `MIXED(...)`. `--retail-currency` and
`fetch_retail.py --currency` default to USD to match.

## Not Azure-only in spirit

The FOCUS path (`ListCost` vs `EffectiveCost`) is a cloud-agnostic FinOps standard, so the same
calculator handles FOCUS exports from other providers. Works across Azure agreement types
(EA, MCA, MOSP/PAYG, CSP), and the FOCUS path applies beyond Azure.

## Files

| Path | Purpose |
|---|---|
| `SKILL.md` | The GitHub Copilot skill: workflow, intent handling, and Cloud Shell command tiers |
| `scripts/actualize.py` | Deterministic retail-vs-actual + savings + delta calculator (stdlib only) |
| `scripts/fetch_retail.py` | Ambiguity-safe retail list-price fetch by meterId (stdlib only) |
| `samples/` | Fixtures for every input shape, incl. `zeroed-retail.json` + `retail-map.csv` (enrichment) |
| `tests/test_actualize.py` | Deterministic unit tests (stdlib `unittest`, no third-party deps) |

## Tests

```bash
python tests/test_actualize.py
```

Asserts the exact expected numbers (blended 22.5%, delta −$608.75/mo run-rate, etc.) and the
never-guess rules: zeroed-retail → UNKNOWN; ambiguous meterId → not enriched (even from raw Retail
API JSON, and a paginated `NextPageLink` response adopts **nothing**); **partial-retail groups and
totals leave Savings/Discount UNKNOWN** rather than blending mismatched row sets; substring matches
are **token-boundary aware and require complete retail+actual coverage** (exact match wins, so `vm1`
never drags in — or borrows the discount of — `vm10`), and matches with **different discounts are
refused**; **mixed currencies are never summed** — not across totals, and not within a single group
(e.g. `--group-by service`) — and USD retail is never applied to non-USD rows; `delta` reports a
resource with **no actual-cost data as UNKNOWN, never $0**, and uses **run-rate-normalized** deltas
for unequal windows; UNMATCHED excluded; resource lists never drop the first line. No network access
— the Retail Prices logic is tested via its pure `resolve()` on synthetic records.

## License

This project is licensed under the [MIT License](LICENSE) — © 2026 Microsoft.
