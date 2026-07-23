---
name: actualize
description: >-
  Determine the ACTUAL (effective / negotiated / reservation- and savings-plan-adjusted)
  cost of Azure resources versus RETAIL (list / pay-as-you-go) price, with the percent
  difference for each line item. Works even for Azure tenants this skill cannot reach:
  it emits Azure Cloud Shell commands to copy-paste, and ingests the results pasted back.
  Also converts a pasted table of USD cost *reductions* into ACTUAL savings per line item,
  and computes the change in ACTUAL cost when resources are updated or deleted (before/after,
  or projected monthly run-rate if removed). Every number is backed by real Azure Cost
  Management / Consumption / FOCUS data — the skill NEVER guesses, assumes, or fabricates a
  price. Works across Azure agreement / billing types, and still produces
  a retail-vs-actual comparison when an export omits list price by enriching retail from the
  public Azure Retail Prices API.
---

# Actualize — get the actual cost of Azure resources

Report the **actual** cost of Azure resources — what you really pay after negotiated
discounts, reservations, and savings plans — next to the **retail** (list / pay-as-you-go)
price, with the **percent difference** per line item and in total. Then, optionally, turn a
pasted table of proposed **USD cost reductions** into **actual** savings.

## The one rule: never guess

Every retail number, actual number, and percentage **must** come from data the user pasted
back from Azure Cost Management / Consumption / a FOCUS export. If a value is not present in
the data, output `UNKNOWN` / `UNMATCHED` for that cell — **do not** infer, interpolate, or
look up a "typical" discount. The only sanctioned external lookup is the anonymous **Azure
Retail Prices API** for *retail* rates, and even then it is a labeled cross-check, never a
substitute for actual cost data.

## Core method (why this is the right approach)

Do **not** try to independently guess each resource's retail rate and match it to a resource —
that requires assumptions about meter, region, tier, and unit. Instead, pull **retail and
actual from the same authoritative line item**, so the comparison is apples-to-apples:

| Quantity | Source field(s) | Meaning |
|---|---|---|
| Retail cost | `ListCost` (FOCUS), or `payGPrice × quantity` | list / PAYG cost |
| Actual cost | `EffectiveCost` (FOCUS, amortized), or `costInBillingCurrency`, or `pretaxCost` | what you actually pay |
| Discount % | `1 − actual ÷ retail` | negotiated + commitment savings |

Compare on **cost accrued over a representative period using amortized data** — not a single
hourly rate. Cost already bakes in quantity, tiered pricing, reservations, and savings plans,
so a period total is both simpler and more accurate than reconstructing hourly rates. Use
**AmortizedCost** (not ActualCost) when reservations or savings plans are involved, so prepaid
commitments are spread across the resources that consume them instead of appearing as lump-sum
purchase lines.

## Currency: USD is the default only when nothing else is known

Currency is a **label/unit**, never a fabricated cost, so it follows a fixed precedence:

1. If the pasted data carries a currency (`billingCurrency`/`BillingCurrencyCode`/`pricingCurrency`),
   that currency is always used.
2. Else if the user passes `--currency`, that label is used.
3. Else the skill assumes **USD** — the most common Azure billing currency and the Azure Retail
   Prices API default.

USD is a *labeling* assumption applied only when the data has no currency and none was given; it
never changes, invents, or converts a cost figure. **Genuinely mixed-currency data is never coerced
to USD** — it stays `MIXED(...)` and its totals are not aggregated, even if `--currency USD` is
passed. When results rest on the assumed default (no currency in the data), **say so** when
presenting: e.g. "amounts assumed USD; include `billingCurrency` or pass `--currency` to change."
The calculator's constant `DEFAULT_CURRENCY` (in `scripts/actualize.py`) is the single source of
truth, and `--retail-currency` / `fetch_retail.py --currency` default to USD to match.

> Reservation caveat to state in findings: turning off a resource already covered by a prepaid
> reservation may not save cash until the reservation expires or is reallocated. The amortized
> effective cost is the best data-backed estimate of its run-rate; call this nuance out rather
> than implying instant cash savings.

## Running the commands: direct (testing) vs copy-paste (production)

There are two ways to get the data, and the skill supports both:

- **Copy-paste (the default, and required in production).** The target tenant usually isn't
  reachable from the user's machine (e.g. a locked-down workstation can't hit the production
  tenant). Give the
  user the Cloud Shell command, they run it in the target tenant, and they paste the output
  back. This is the primary, always-available path.
- **Direct run (testing / when the tenant IS reachable).** When `az` on the current machine is
  already signed in to the target tenant (e.g. testing against a tenant you have access to), the
  commands can be run directly and piped straight into the calculator, e.g.
  `az consumption usage list ... -o json | python scripts/actualize.py report --input -`. Treat
  this only as a convenience for testing — **do not assume** it will work for real workloads,
  where copy-paste in/out is the norm.

Either way the numbers come from the same commands and the same calculator, so results are
identical; only the transport differs.

## Real-world gotchas (observed against a live tenant)

- **`RBACAccessDenied` usually means the *wrong subscription is selected*, not a missing role.**
  Cloud Shell defaults to the first subscription in the user's list, which is often **not** the
  one hosting the resource. When `az consumption usage list` or the Cost Management query returns
  `RBACAccessDenied` (a `403`), **do not** conclude the user lacks Cost Management Reader — first
  confirm the resource actually lives in the selected subscription. Resolve the resource's real
  `subscriptionId` with Azure Resource Graph, `az account set` to it, then retry (see "Pin the
  subscription by resource" below). Only after the correct subscription is selected does a
  persistent `403` point to a genuine role/scope problem.
- **Subscription-scope consumption can be empty for some enrollment-based subscriptions.**
  `az consumption usage list` at subscription scope may return `[]` even when there's spend,
  because usage details live at the **enrollment / billing-account** scope. If Tier 1 is empty,
  use Tier 2/Tier 3 at the billing-account or enrollment-account scope (swap the `{scope}` in
  the URL), or ask the user for the billing scope.
- **`az consumption usage list` can return rows whose cost fields are the literal `"None"`/null.**
  On some subscriptions Tier 1 returns metadata rows (resource, meter, quantity) but
  every `pretaxCost`/`costInBillingCurrency` comes back as the string `"None"` (or null) — the export
  carries **no billed amounts**. This is a *silent* failure: the calculator will correctly mark every
  actual `UNKNOWN`, but that's not a data problem you can fix by parsing. **Detect it early** — if the
  cost column is uniformly `None`/null, stop and switch to the **Cost Management Query API (Tier 2)**,
  which returns real amounts at subscription scope. Don't keep re-pasting Tier 1.
- **`az graph query` prints only a count wrapper with `-o table`.** The rows live under a `data`
  property, so a bare `-o table` shows just `Count`/`Total_records`. Always add `--query data`
  (with `-o table`/`-o json`) — e.g. `az graph query -q "<kql>" --query data -o json` — or you'll
  think the query returned nothing.
- **The Cost Management Query API is aggressively throttled (HTTP 429).** Run it **once** and,
  on 429, wait and retry a single time (honor the `Retry-After` header when present; a plain
  `{"error":{"code":"429"}}` with no header means wait a few minutes). Do **not** hammer it with
  rapid retries — that extends the cooldown. A 429 (vs. a 400) confirms your request URL, auth,
  api-version, and body were valid.
- **Some exports zero out retail (`payGPrice`/`paygCostInBillingCurrency` = 0) while still
  reporting actual cost.** Whether the list-price fields are populated depends on the agreement
  type and export configuration. When retail is `0`/empty but `effectivePrice` /
  `costInBillingCurrency` (actual) is present, you get **actual directly but must enrich retail**
  from the Retail Prices API by `meterId` — see the enrichment section below. When retail is
  present in the row, that in-row value is used as-is and enrichment is skipped.
- **One `meterId` can map to many SKUs (retail is not always unique).** e.g. a single SQL vCore
  `meterId` returns "1 vCore" … "80 vCore" at different unit prices under one id. Picking one would
  be a guess, so the enrichment refuses these (`status=ambiguous`) and leaves retail `UNKNOWN`
  until a human disambiguates by SKU — never silently choosing.

## Workflow

1. **Identify scope & agreement.** Ask (or have the user run) which billing scope and whether
   which Azure agreement / billing type it is — this decides which command returns retail fields. When the
   user names a **specific resource** (e.g. an App Service plan), resolve *its* `subscriptionId`
   and resource group with Resource Graph first and `az account set` to that subscription — don't
   assume the default Cloud Shell subscription is the right one (see "Pin the subscription by
   resource" below).
2. **Emit a Cloud Shell command** (below) for the user to run in their tenant.
3. **User pastes the raw output back.** Save it verbatim to a file.
4. **Run the calculator** — never hand-compute:
   ```bash
   python scripts/actualize.py report --input <pasted-file>
   python scripts/actualize.py savings --cost <pasted-file> --reductions <reduction-table>
   ```
5. **Present** the returned markdown table as-is. Do not edit the numbers; if a cell says
   `UNKNOWN`, explain what additional data would fill it.

## Cloud Shell commands (copy-paste into the target tenant)

Tell the user to open **Azure Cloud Shell** (Bash) at https://shell.azure.com. `az` is
pre-authenticated there, so these run against **their** tenant without giving this skill access.

> **Cloud Shell is remote, not the user's laptop.** It has no `~/Downloads`, no `/mnt/c`, and no
> access to local paths. Write any temp files to `~` or `/tmp` and end the snippet with `cat <file>`
> so the user can copy the output back — never `mkdir` a Windows path or write to `~/Downloads`.

> **Which tier to start with.** For an **actual-cost figure or a before/after `delta`, start with the
> Tier 2 Cost Management Query API** (`az rest … /query`, `AmortizedCost`): it reliably returns real
> amounts at subscription scope, works across agreement types, and — with a daily granularity +
> `ResourceId In (...)` filter — emits a tiny, paste-friendly payload that feeds `delta` directly.
> Reach for Tier 1 only when you specifically need per-line-item **retail** alongside actual, and be
> ready to fall back if its cost fields come back `None` (see gotchas). The Tier 3 async report is the
> most complete but the most fragile to run — prefer Tier 2 unless you need its full per-line CSV.

**Pin the subscription by resource first (when the query targets a named resource).** Cloud
Shell's default subscription is frequently the *wrong* one, which surfaces as `RBACAccessDenied`.
Resolve the resource's real subscription (and resource group, for RG-scoped queries) up front and
select it, so every later command runs against the correct billing scope:

```bash
RES="my-app-plan"                        # the resource name the user gave
az graph query -q "resources | where name =~ '$RES' | project name, resourceGroup, subscriptionId, id" -o table
# then pin the subscription (and capture RG) from that result:
SUB=<subscriptionId-from-above>
RG=<resourceGroup-from-above>
az account set --subscription "$SUB"
az account show --query "{name:name, id:id}" -o table   # confirm you're where you expect
```

If Resource Graph isn't available, `az resource list --name "$RES"` in each candidate subscription
works but is slower. Then set the period window:

```bash
SUB=$(az account show --query id -o tsv)   # already pinned above; or set it explicitly
START=2026-06-01      # first day of the period, YYYY-MM-DD (UTC)
END=2026-06-30        # last day of the period
```

### Tier 1 — retail + actual per line item, one command (includes list price)

Returns per-resource usage details. On **modern** billing accounts each row carries both
`payGPrice` (retail) and `costInBillingCurrency` (actual) plus `effectivePrice` and `quantity`.
Use it when you specifically want per-line **retail** next to actual. **Caveat:** on some
enrollment subscriptions the cost fields come back as literal `"None"`/null (metadata but no billed
amounts) — if so, don't retry Tier 1; switch to the Tier 2 Query API (see gotchas).

```bash
az consumption usage list --start-date $START --end-date $END \
  --include-meter-details --include-additional-properties -o json
```

Paste the JSON back → `python scripts/actualize.py report --input pasted.json`.

If rows lack `payGPrice` (older **legacy** schema), retail shows `UNKNOWN` — use Tier 3 for
list price, or Tier 4 to cross-check retail.

### Tier 2 — grouped actual / amortized totals (all agreements; **start here for actual & delta**)

There is **no** `az costmanagement query` command; use the REST API via `az rest`. This returns
**actual only** (no list price) — combine with Tier 4 retail if you need a retail comparison. It is
the **most reliable actual-cost source at subscription scope** and the recommended first stop for a
single actual figure or a before/after `delta`.

```bash
az rest --method post \
  --url "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.CostManagement/query?api-version=2025-03-01" \
  --body '{
    "type": "AmortizedCost",
    "timeframe": "Custom",
    "timePeriod": { "from": "'"$START"'", "to": "'"$END"'" },
    "dataset": {
      "granularity": "None",
      "aggregation": { "totalCost": { "name": "Cost", "function": "Sum" } },
      "grouping": [ { "type": "Dimension", "name": "ResourceId" } ]
    }
  }'
```

Paste the JSON (it has `columns`/`rows`) → `report`. Use `"type": "ActualCost"` to see
invoice-time charges instead of amortized. Swap the scope in the URL for resource-group,
billing-account, department, or enrollment-account scope as needed. **If you get HTTP 429**,
wait (per `Retry-After`, or a few minutes) and retry **once** — don't loop rapidly.

**Targeted `delta` recipe (recommended).** To compare a handful of resources across two periods,
add `"granularity": "Daily"` and a `ResourceId In (...)` filter. Daily rows let `delta` infer window
length automatically, and the filter keeps the output tiny and paste-friendly. Run it once per
period (`$START`/`$END` set to the before window, then the after window):

```bash
# List every resourceId you care about (one entry per separately billed resource — e.g. each
# member of a multi-region / failover set has its own id). Values are matched case-insensitively.
RID1="/subscriptions/$SUB/resourcegroups/<rg>/providers/<provider>/<type>/<name>"
RID2="/subscriptions/$SUB/resourcegroups/<rg>/providers/<provider>/<type>/<name-2>"
az rest --method post \
  --url "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.CostManagement/query?api-version=2025-03-01" \
  --body '{
    "type": "AmortizedCost",
    "timeframe": "Custom",
    "timePeriod": { "from": "'"$START"'", "to": "'"$END"'" },
    "dataset": {
      "granularity": "Daily",
      "aggregation": { "totalCost": { "name": "Cost", "function": "Sum" } },
      "grouping": [ { "type": "Dimension", "name": "ResourceId" } ],
      "filter": { "Dimensions": { "Name": "ResourceId", "Operator": "In", "Values": [ "'"$RID1"'", "'"$RID2"'" ] } }
    }
  }' -o json | tee ~/period.json    # cat ~/period.json to copy it back
```

Feed the two pasted files to `delta` (add `--annual` for a yearly run-rate):
`python scripts/actualize.py delta --before before.json --after after.json --group-by resource --annual`.
Daily rows normally cover every day the resource billed, so `delta` infers the window automatically.
If a resource can have **zero-cost / inactive days** (which the query may omit), pass explicit
`--before-days`/`--after-days` equal to the `$START`→`$END` span so a short denominator can't inflate
the run-rate.

### Tier 3 — most complete: Cost Details report (retail + actual per line item, async)

The modern replacement for the Usage Details API. Produces a CSV with `payGPrice`,
`effectivePrice`, `costInBillingCurrency`, `quantity`, and full resource metadata. It's a
3-step async flow; this Bash snippet runs the whole thing and prints the CSV to paste back
(`metric` = `AmortizedCost` or `ActualCost`). It's the most fragile tier to run — prefer Tier 2
unless you need the full per-line CSV.

```bash
TOKEN=$(az account get-access-token --query accessToken -o tsv)
SCOPE="subscriptions/$SUB"
# 1) kick off the report; capture the polling URL from the Location header
LOC=$(curl -s -D - -o /dev/null -X POST \
  "https://management.azure.com/$SCOPE/providers/Microsoft.CostManagement/generateCostDetailsReport?api-version=2025-03-01" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"metric":"AmortizedCost","timePeriod":{"start":"'"$START"'","end":"'"$END"'"}}' \
  | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}')   # case-insensitive header match
if [ -z "$LOC" ]; then
  echo "No Location header (report returned inline or errored); use Tier 2 instead." >&2
else
  # 2) poll until the blob URL(s) appear; bail out if the job reports Failed
  until RESP=$(curl -s -H "Authorization: Bearer $TOKEN" "$LOC") \
        && echo "$RESP" | grep -q '"blobLink"'; do
    echo "$RESP" | grep -q '"status":"Failed"' && { echo "report Failed: $RESP" >&2; break; }
    sleep 10
  done
  # 3) download and print the CSV (only if a blob actually appeared)
  if echo "$RESP" | grep -q '"blobLink"'; then
    BLOB=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['manifest']['blobs'][0]['blobLink'])")
    curl -s "$BLOB"
  fi
fi
```

Paste the CSV back → `report`. (Some billing accounts can use `"billingPeriod":"202606"` instead of
`timePeriod`, or `"invoiceId":"..."`.)

### Tier 4 — retail cross-check / fallback (anonymous, any tenant)

The **Azure Retail Prices API** needs no auth and returns list prices. Use it only to fill or
verify **retail** when a row lacks `payGPrice`, and label it as list price, never as actual:

```bash
curl -s "https://prices.azure.com/api/retail/prices?currencyCode='USD'&\$filter=armRegionName eq 'westus2' and armSkuName eq 'Standard_D2_v5' and priceType eq 'Consumption'"
```

### Tier 5 — gold standard for recurring use: FOCUS export

For ongoing reporting, have the user configure a **FOCUS** cost export (Cost Management →
Exports → FOCUS dataset). Its `ListCost` vs `EffectiveCost`/`BilledCost` columns are the
cleanest retail-vs-actual source and are cloud-agnostic (works beyond Azure). Paste the
exported CSV → `report`.

## Retail enrichment (when `payGPrice` is zeroed or absent)

When a cost export has actual cost but **retail = 0** (or the list-price field is absent), a plain
`report` shows retail `UNKNOWN`. Enrich retail from the anonymous Retail Prices API by `meterId`,
using the bundled helper — which **never guesses** on ambiguous meters:

```bash
# 1) list the meterIds that still need retail (and get a ready-to-run fetch hint)
python scripts/actualize.py meters --input costdetails.csv

# 2) fetch published list prices by meterId (Retail Prices API, anonymous).
#    ok = one unambiguous Consumption unit price; ambiguous/notfound get NO price.
python scripts/fetch_retail.py --input costdetails.csv --out retail-map.csv

# 3) re-run the report with the enrichment map
python scripts/actualize.py report --input costdetails.csv --retail-map retail-map.csv --group-by service
```

`fetch_retail.py` resolves each `meterId` to exactly one base-tier (`tierMinimumUnits == 0`)
Consumption price. If a `meterId` resolves to several distinct SKU prices it is written with an
empty `retailPrice` and `status=ambiguous`; `notfound` means the catalog returned no base
Consumption record. Enrichment **only** fills rows whose retail is missing/zeroed — it never
overwrites retail that came from the cost data. Enriched rows are labeled
`retail=list price via Retail Prices API` in the Note column.

**Interpreting the enriched report honestly:**
- `retail incomplete (X/Y rows)` in a group's Note ⇒ some meters were ambiguous/notfound and left
  `UNKNOWN`. Because retail then covers fewer rows than actual, that group's **Savings and Discount %
  are reported `UNKNOWN`** (not a blended number over mismatched row sets); its Retail column shows
  only the resolved rows for reference. The same applies to the **TOTAL** — a blended discount is
  shown only when every contributing group is fully paired.
- `REVIEW: actual > retail` (negative discount) or `REVIEW: >99%` ⇒ a likely meter/unit/tier
  mismatch **or** a genuine case where the negotiated effective rate exceeds public retail. Surface
  these for manual check; do not present them as settled savings.
- The most trustworthy retail is still the in-row `payGPrice`/`ListCost` when present —
  enrichment is a labeled best-effort used only when that field is missing or zeroed.
- Retail enrichment assumes the map's currency (USD by default via `--retail-currency`); rows billed
  in another currency are left `UNKNOWN` rather than mixing currencies, and a TOTAL over mixed
  currencies is not aggregated. If a single **group** (e.g. under `--group-by service`) spans more
  than one currency, that group's Retail/Actual/Savings are `UNKNOWN` too — its rows are never summed
  across currencies.
- A pasted **reduction** line only borrows a resource's discount when the match is unambiguous: it
  must hit the resource at a token boundary (so `vm1` never borrows `vm10`'s discount) and that
  resource must have **complete** retail *and* actual coverage; otherwise it is **UNMATCHED**.
- In `delta`, a resource present in a period but with **no actual-cost data** is `UNKNOWN` (never
  treated as `$0`), and such rows are excluded from the TOTAL (called out in the note).

## The calculator — Actualize (`scripts/actualize.py`, stdlib only, deterministic)

Auto-detects the input shape (FOCUS CSV/JSON, modern/legacy usage-details JSON, or
CostManagement `query` columns/rows) and normalizes each row to `retailCost` / `actualCost`.

```bash
# Retail vs actual vs %-difference (group by resource | meter | service | subscription | row)
python scripts/actualize.py report --input DATA [--group-by resource] [--retail-map MAP] [--format md|csv|json] [--currency USD]

# Actual savings from a pasted USD reduction table
python scripts/actualize.py savings --cost DATA --reductions TABLE [--format md|csv|json]

# List meterIds needing retail enrichment (when retail is zeroed/absent) + a fetch hint
python scripts/actualize.py meters --input DATA

# Change in ACTUAL cost for updated/deleted resources (before/after, or run-rate if removed)
python scripts/actualize.py delta --before DATA [--after DATA2] [--resources LIST] [--group-by resource] [--decreases-only] [--annual]
```

`report` emits one row per group with **Retail / Actual / Savings / Discount % / Note**, plus a
**TOTAL**. Any group lacking retail is marked `UNKNOWN`; a group with only partial retail keeps its
Savings/Discount `UNKNOWN` (retail and actual would cover different rows) and is flagged in Note.

## Reduction-table mode (the "paste a table of USD cost reductions" use case)

The user pastes a markdown or CSV table where each line is a proposed **retail** USD reduction
(e.g. "decommission vm-app01 → $71.42/mo"). For each line the calculator:

1. Matches the line's label to a resource / meter / service in the pasted cost data.
2. Reads that match's **actual ÷ retail** ratio from the data.
3. Reports **actual reduction = retail reduction × ratio**, with the match basis.

Unmatched lines are marked **UNMATCHED** and **excluded** from the total — never guessed. To
resolve them, ask the user to include those resources in the pasted cost data. Example:

```bash
python scripts/actualize.py savings --cost pasted-usage.json --reductions reductions.md
```

| Item | Retail reduction (USD) | Actual/Retail | Actual reduction (USD) | Match basis |
|---|--:|--:|--:|---|
| vm-app01 | 71.42 | 60.0% | 42.85 | exact |
| some-thing | 50.00 | UNMATCHED | UNKNOWN | UNMATCHED (no match in cost data) |

## Change-in-actual-cost mode (`delta`) — resources updated or deleted

Use this to answer "what did the **actual** cost change when these resources were updated or
deleted?" It compares actual cost per resource across data, in two shapes:

**Two-period** — pass a baseline and a later period; get the actual change per resource:

```bash
python scripts/actualize.py delta --before before.csv --after after.csv [--group-by resource|meter|service]
```

Each resource is classified **REMOVED** (in baseline, gone later), **ADDED**, **CHANGED**, or
**UNCHANGED**, with `Chg actual` (raw, over the supplied windows) and `Chg actual/mo` (run-rate).
If the data carries usage dates, window length is inferred automatically and figures are
normalized to a monthly run-rate; otherwise pass `--before-days`/`--after-days`, or compare
equal-length windows. Negative = cost went **down**.

**Single-period run-rate** — point at the current period only to get each resource's actual
monthly run-rate = the **projected reduction if it is removed** (data-backed, with the
reservation caveat stated):

```bash
python scripts/actualize.py delta --before current.csv --group-by resource --decreases-only
```

Restrict to specific resources (paste a list or a table of resourceIds/names). Requested items
with no backing actual-cost data are reported **NOT FOUND** and excluded — never estimated:

```bash
python scripts/actualize.py delta --before before.csv --after after.csv --resources changed-resources.txt
```

To get the before/after data, run the **Tier 2 targeted `delta` recipe** (daily granularity +
`ResourceId In (...)` filter) for each period — its daily rows let `delta` infer window length and
the payload is tiny. Tier 3 (Cost Details report) also works. `delta` uses **actual cost only**
(actual-vs-actual across time), so it needs no retail and is unaffected by a zeroed/absent
`payGPrice`.

**Annualize with `--annual`.** By default `delta` emits a monthly run-rate (`Chg actual/mo`). When
the user wants a **yearly** figure, add `--annual` for a `Chg actual/yr` column (= monthly run-rate
× 12; 30.4375 × 12 = 365.25 days/yr). It is a **forward-looking run-rate projection, not measured
actuals** — valid only while usage and pricing hold; annualizing also multiplies any error in the
monthly run-rate by 12, and a short before/after window makes that run-rate less representative to
begin with. It needs a monthly run-rate (dated data, or `--before-days` and, for two-period mode,
`--after-days`); without one the column is omitted with a note (md) or a stderr `[warn]` (csv/json)
rather than fabricated.

> Reservation nuance: a `REMOVED`/`RUN-RATE` reduction is the amortized run-rate, not guaranteed
> cash. If the resource was covered by a reservation or savings plan, cash savings lag until that
> commitment is reallocated or expires — state this alongside the number.

### Dating a change: when did a scale-down / resize actually deploy?

For an IaC scale-down ("we changed the SKU in the repo — what's the saving?") you first need the
**exact per-resource change date**, because that decides the before/after windows. Key facts learned
the hard way:

- **Merge date ≠ deploy date.** The commit/PR that lowers a SKU can merge days or weeks before it
  reaches production. Staged rollouts deploy **region-by-region**, so a geo-replicated
  resource changes on *different days* per region — those are the "different days," not the merge.
  Never assume the merge date is when cost dropped; confirm from Azure.
- **Exact change time via Resource Graph change history** (last **14 days** only). Filter on
  `properties.targetResourceId` — **not** `resourceId` (that column doesn't exist and errors),
  and remember `--query data`:

  ```bash
  az account set --subscription "$SUB"   # change history is per-subscription
  az graph query -q "resourcechanges | where properties.targetResourceId =~ '$RESID' \
    | extend ts=todatetime(properties.changeAttributes.timestamp), chg=properties.changes \
    | project ts, changeType=tostring(properties.changeType), chg | order by ts asc" \
    --query data -o json
  ```

  Look for the size/SKU field's `previousValue`→`newValue` drop — that timestamp is the real
  scale-down for that region. **The exact field varies by resource type**: SQL DB uses
  `sku.capacity` / `properties.currentSku.capacity` (vCores), a VM uses
  `properties.hardwareProfile.vmSize`, App Service / other SKU-based services use `sku.name` /
  `sku.tier`, Cosmos DB uses provisioned throughput. Scan the `changes` payload for whichever
  size/tier field moved.
- **Older than 14 days → Activity Log** (~90-day retention). Two caveats that cost round-trips:
  it is scoped to the **selected** subscription (run `az account set` first — unlike cross-sub
  `az graph`), and its `properties` do **not** carry the SKU payload, so it tells you *a* write
  happened but not *which* write changed the tier. Use it for timing, Resource Graph (or the cost
  meter drop) for the "what changed."
- **Fallback: the daily cost meter drop.** With no change-history record, the day a resource's daily
  compute/SKU cost steps down (from the Tier 2 daily query) is the effective change date.

**Multi-region / replicated resources bill and scale per region.** Some deployments are **several
separately billed resources with distinct resourceIds** — e.g. an active geo-replication / failover
pair (a primary + secondary SQL database) or the same service deployed independently to several
regions. Each such resource has its own id and changes on its own rollout day, so scope the Tier 2
query to **all** of them. (Contrast a *single* globally distributed resource — e.g. one multi-region
Cosmos DB account — which usually keeps **one** resourceId, with regional cost split across meters /
dimensions rather than separate ids; group or filter by those dimensions instead.) For a SKU
*change* (not a removal) pick a baseline window fully **before the first** region's change and an
after window fully **after the last** region's change, so no partially-migrated day muddies the
run-rate. Isolate the resource you care about by filtering to its exact resourceId; sibling or
parent-scope meters (e.g. a security add-on billed on the parent `/servers/<name>` rather than the
`/databases/<name>` you resized) don't move with the SKU and would otherwise blur the delta.

## Presenting results

- Show the calculator's table verbatim; state the period, scope, currency, and whether costs
  are **amortized** or **actual**. If the currency was **assumed USD** (no currency in the data
  and no `--currency`), say so explicitly.
- Report the blended discount from the TOTAL row, and call out the largest-discount and
  zero-discount line items.
- **Lead with actual when you have it; treat retail as context only.** Retail is the list / PAYG
  price and can differ from what an agreement actually pays, so when actual cost data exists present
  the **actual** figure as the headline and show retail only as a separately labeled cross-check —
  never quote the retail number as the saving. Do not editorialize about the size of any gap between
  the two.
- **If the user asked for a yearly figure, give the annualized number** (`delta --annual`) and label
  it a forward-looking run-rate projection, not measured actuals.
- For every `UNKNOWN`/`UNMATCHED`, say exactly which command/field would resolve it.
- Never present a number that isn't in the pasted data or the calculator output.

## Field reference (what maps to what)

- **Retail**: `ListCost`; else `payGPrice × quantity`.
- **Actual**: `EffectiveCost` (amortized) → `costInBillingCurrency` → `pretaxCost`/`PreTaxCost`/`Cost` → `BilledCost`; else `effectivePrice × quantity`.
- **Amortized vs actual**: prefer amortized (`AmortizedCost` / `EffectiveCost`) so reservations
  and savings plans are spread across consuming resources.
- Docs: usage/cost-details fields — https://learn.microsoft.com/azure/cost-management-billing/automate/understand-usage-details-fields
