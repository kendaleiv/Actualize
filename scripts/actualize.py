#!/usr/bin/env python3
"""Actualize — deterministic retail vs. actual Azure cost calculator.

This script NEVER guesses. It only computes from numbers present in the input
that you paste from Azure Cost Management / Consumption / FOCUS exports.

It understands several shapes (auto-detected):
  * FOCUS export CSV/JSON      -> ListCost (retail) + EffectiveCost (actual)
  * Modern usage details JSON  -> payGPrice*quantity (retail) + costInBillingCurrency (actual)
  * Legacy usage details JSON  -> pretaxCost (actual); retail only if payGPrice present
  * `az costmanagement query`  -> columns/rows (actual only, unless a list-cost column exists)

Subcommands:
  report   Retail vs actual vs %-difference table (per resource / meter / service / row).
  savings  Apply per-line-item actual/retail ratio to a pasted USD *reduction* table,
           yielding the ACTUAL savings for each line item.
  delta    Change in ACTUAL cost for resources between two periods (before/after a change
           or deletion), or the projected monthly run-rate reduction if a resource is removed.
  meters   List distinct meterIds lacking retail, with an ambiguity-safe fetch snippet.

Anything that cannot be backed by data is marked UNKNOWN / UNMATCHED — never invented.
"""

import argparse
import csv
import datetime
import io
import json
import re
import sys

# Currency assumed for display/enrichment when the data carries no currency and
# the user gives none. It is a *labeling* default only -- it never changes or
# fabricates a cost figure. A currency detected in the data always wins over it,
# and genuinely mixed-currency data is never coerced to it (see detect_currency).
DEFAULT_CURRENCY = "USD"

# ---------------------------------------------------------------------------
# Field dictionaries (canonical -> candidate source headers, case-insensitive)
# ---------------------------------------------------------------------------
FIELDS = {
    "resourceId":   ["resourceId", "instanceId", "ResourceId", "InstanceId"],
    "resourceName": ["resourceName", "instanceName", "ResourceName", "InstanceName"],
    "resourceGroup":["resourceGroup", "resourceGroupName", "ResourceGroup"],
    "service":      ["ServiceName", "serviceName", "meterCategory", "MeterCategory",
                     "consumedService", "ConsumedService", "ServiceCategory"],
    "meter":        ["meterName", "MeterName", "meter", "Meter", "SkuMeterName"],
    "meterId":      ["meterId", "MeterId", "meterGuid"],
    "unitOfMeasure":["unitOfMeasure", "UnitOfMeasure", "unitOfMeasureName"],
    "region":       ["resourceLocation", "ResourceLocation", "region", "RegionId",
                     "location", "instanceLocation"],
    "subscription": ["subscriptionName", "SubscriptionName", "subscriptionId",
                     "SubscriptionId", "subscriptionGuid", "SubAccountName"],
    "currency":     ["billingCurrency", "BillingCurrency", "BillingCurrencyCode",
                     "currency", "Currency", "pricingCurrency"],
    "quantity":     ["quantity", "Quantity", "usageQuantity", "UsageQuantity",
                     "ConsumedQuantity"],
    "date":         ["date", "Date", "usageDate", "UsageDate", "ChargePeriodStart",
                     "chargePeriodStart", "billingPeriodStartDate", "servicePeriodStartDate"],
    "payGPrice":    ["payGPrice", "PayGPrice"],
    "effectivePrice":["effectivePrice", "EffectivePrice"],
    "unitPrice":    ["unitPrice", "UnitPrice", "ContractedUnitPrice"],
    "listCost":     ["ListCost", "listCost"],
    "paygCost":     ["paygCostInBillingCurrency", "PayGCostInBillingCurrency",
                     "paygCostInUsd", "paygCostInUSD"],
    # actual/billed cost, most-preferred first
    "actualCost":   ["EffectiveCost", "effectiveCost", "costInBillingCurrency",
                     "CostInBillingCurrency", "pretaxCost", "PretaxCost", "PreTaxCost",
                     "costInUSD", "CostInUSD", "BilledCost", "billedCost", "cost", "Cost"],
}


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "")
    if s == "" or s.lower() in ("null", "none", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _lc_get(row_lc, candidates):
    """row_lc maps lowercased-header -> (original_value). Returns first candidate hit."""
    for c in candidates:
        v = row_lc.get(c.lower())
        if v not in (None, ""):
            return v
    return None


# ---------------------------------------------------------------------------
# Input loading -> list of flat dict rows
# ---------------------------------------------------------------------------
def _flatten(d):
    """Merge nested 'properties' and 'meterDetails' up to the top level."""
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        if k in ("properties", "meterDetails") and isinstance(v, dict):
            for kk, vv in v.items():
                out.setdefault(kk, vv)
        else:
            out[k] = v
    return out


def load_rows(text):
    """Return (rows, shape). rows is a list of flat dicts."""
    text = text.strip()
    if not text:
        return [], "empty"
    # JSON?
    if text[0] in "[{":
        data = json.loads(text)
        # costmanagement query shape: {"columns":[...],"rows":[[...]]}
        if isinstance(data, dict) and "columns" in data and "rows" in data:
            cols = [c.get("name") if isinstance(c, dict) else c for c in data["columns"]]
            rows = [dict(zip(cols, r)) for r in data["rows"]]
            return [_flatten(r) for r in rows], "costmanagement-query"
        # REST list result: {"value":[...]} or {"properties":{"rows"...}}
        if isinstance(data, dict) and "value" in data and isinstance(data["value"], list):
            data = data["value"]
        if isinstance(data, dict) and "properties" in data and \
                isinstance(data["properties"], dict) and "rows" in data["properties"]:
            p = data["properties"]
            cols = [c.get("name") if isinstance(c, dict) else c for c in p.get("columns", [])]
            rows = [dict(zip(cols, r)) for r in p["rows"]]
            return [_flatten(r) for r in rows], "costmanagement-query"
        if isinstance(data, dict):
            data = [data]
        return [_flatten(r) for r in data], "json-records"
    # CSV / markdown table
    headers, rows = parse_table(text)
    return [dict(zip(headers, r)) for r in rows], "table"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def normalize(row):
    lc = {str(k).lower(): v for k, v in row.items()}
    rec = {c: _lc_get(lc, cands) for c, cands in FIELDS.items()
           if c not in ("payGPrice", "effectivePrice", "unitPrice",
                        "listCost", "paygCost", "actualCost", "quantity")}
    qty = _num(_lc_get(lc, FIELDS["quantity"]))
    payg = _num(_lc_get(lc, FIELDS["payGPrice"]))
    eff = _num(_lc_get(lc, FIELDS["effectivePrice"]))
    unit = _num(_lc_get(lc, FIELDS["unitPrice"]))
    listc = _num(_lc_get(lc, FIELDS["listCost"]))
    paygc = _num(_lc_get(lc, FIELDS["paygCost"]))
    actual = _num(_lc_get(lc, FIELDS["actualCost"]))

    basis_retail = None
    retail = None
    if listc is not None:
        retail, basis_retail = listc, "ListCost"
    elif paygc is not None:
        retail, basis_retail = paygc, "paygCostInBillingCurrency"
    elif payg is not None and qty is not None:
        retail, basis_retail = payg * qty, "payGPrice*quantity"

    basis_actual = None
    if actual is not None:
        # find which candidate produced it, for traceability
        for c in FIELDS["actualCost"]:
            if _num(lc.get(c.lower())) is not None:
                basis_actual = c
                break
    elif eff is not None and qty is not None:
        actual, basis_actual = eff * qty, "effectivePrice*quantity"
    elif unit is not None and qty is not None:
        actual, basis_actual = unit * qty, "unitPrice*quantity"

    # A retail cost of exactly 0 alongside a positive actual cost is not a real
    # $0 list price -- it means the retail field was not populated in the export
    # (this can happen depending on agreement type / export configuration). Treat
    # it as UNKNOWN so it can be enriched from the Retail Prices API by meterId,
    # rather than presenting a misleading 0.
    if retail is not None and retail == 0 and actual is not None and actual > 0:
        retail, basis_retail = None, "zeroed-retail (needs Retail Prices lookup)"

    rec.update({
        "quantity": qty,
        "retailCost": retail,
        "actualCost": actual,
        "retailUnit": payg if payg is not None else (listc / qty if listc and qty else None),
        "actualUnit": eff if eff is not None else (actual / qty if actual and qty else None),
        "basisRetail": basis_retail,
        "basisActual": basis_actual,
    })
    return rec


def load_retail_map(path):
    """meterId(lower) -> retail unit price. Accepts Retail Prices API JSON
    ({"Items":[...]}/[...] with meterId+retailPrice), a plain {meterId: price}
    object, or CSV with meterId,retailPrice columns."""
    if not path:
        return {}
    text = _read(path).strip()
    out = {}
    if text[:1] in "[{":
        data = json.loads(text)
        page_link = None
        if isinstance(data, dict) and "Items" in data:
            page_link = data.get("NextPageLink") or data.get("nextPageLink")
            data = data["Items"]
        if isinstance(data, dict):  # plain map
            for k, v in data.items():
                p = _num(v)
                if p is not None:
                    out[str(k).strip().lower()] = p
            return out
        # A NextPageLink means this saved Retail Prices API response is only the
        # first page -- a later page could carry a conflicting price for the same
        # meterId. An incomplete response cannot be resolved safely, so no prices
        # are adopted (those meters stay UNKNOWN) rather than guessing from page 1.
        if page_link:
            sys.stderr.write("[warn] retail JSON contains a NextPageLink (paginated, incomplete); "
                             "no prices adopted from it. Fetch ALL pages (or use "
                             "scripts/fetch_retail.py) before enriching.\n")
            return out
        # Raw Retail Prices API list: a single meterId can carry several distinct
        # base-tier Consumption prices (many SKUs share one meterId). Only adopt a
        # price when the meterId resolves to exactly ONE distinct base price --
        # otherwise it is ambiguous and left UNKNOWN (never guessed).
        by_meter = {}
        for it in data:  # list of price records
            mid = it.get("meterId") or it.get("MeterId")
            price = _num(it.get("retailPrice", it.get("RetailPrice")))
            ptype = str(it.get("type", it.get("priceType", "Consumption"))).lower()
            tmin = _num(it.get("tierMinimumUnits", 0)) or 0
            if mid and price is not None and ptype == "consumption" and tmin == 0:
                by_meter.setdefault(str(mid).strip().lower(), set()).add(round(price, 10))
        for mid, prices in by_meter.items():
            if len(prices) == 1:
                out[mid] = next(iter(prices))
        return out
    headers, body = parse_table(text)
    lc = [h.lower() for h in headers]
    mi = lc.index("meterid") if "meterid" in lc else 0
    pi = lc.index("retailprice") if "retailprice" in lc else (1 if len(lc) > 1 else 0)
    for row in body:
        if mi < len(row) and pi < len(row):
            p = _num(row[pi])
            if p is not None:
                out[str(row[mi]).strip().lower()] = p
    return out


def enrich_retail(recs, retail_map, map_currency=DEFAULT_CURRENCY):
    """Fill missing/zeroed retail from a meterId->unitPrice map. Never overwrites
    a retail value that already came from the cost data. The map holds list prices
    in `map_currency` (defaults to USD, which is also the Retail Prices API
    default), so a row billed in a different currency is left UNKNOWN rather than
    mixing currencies."""
    n = 0
    mc = str(map_currency or "").upper()
    for r in recs:
        if r["retailCost"] is None and r.get("meterId") and r["quantity"] is not None:
            rc = str(r.get("currency") or "").upper()
            if mc and rc and rc != mc:
                continue  # don't apply a map-currency price to a different-currency row
            up = retail_map.get(str(r["meterId"]).strip().lower())
            if up is not None:
                r["retailCost"] = up * r["quantity"]
                r["basisRetail"] = "RetailPricesAPI(meterId)*quantity"
                n += 1
    return n


def group_key(rec, mode):
    if mode == "resource":
        return rec.get("resourceId") or rec.get("resourceName") or rec.get("meter") or "(unknown)"
    if mode == "meter":
        return rec.get("meter") or rec.get("service") or "(unknown)"
    if mode == "service":
        return rec.get("service") or "(unknown)"
    if mode == "subscription":
        return rec.get("subscription") or "(unknown)"
    return None  # per-row


def label_for(rec, mode):
    if mode == "resource":
        return rec.get("resourceName") or rec.get("resourceId") or rec.get("meter") or "(unknown)"
    if mode == "meter":
        return rec.get("meter") or rec.get("service") or "(unknown)"
    if mode == "service":
        return rec.get("service") or "(unknown)"
    if mode == "subscription":
        return rec.get("subscription") or "(unknown)"
    return rec.get("resourceName") or rec.get("resourceId") or rec.get("meter") or "(row)"


def aggregate(recs, mode):
    groups = {}
    for r in recs:
        k = group_key(r, mode) if mode != "row" else id(r)
        g = groups.setdefault(k, {
            "label": label_for(r, mode if mode != "row" else "resource"),
            "retail": 0.0, "retail_known": 0, "actual": 0.0, "actual_known": 0,
            "n": 0, "enriched": 0, "service": r.get("service"), "meter": r.get("meter"),
            "resourceName": r.get("resourceName"), "resourceId": r.get("resourceId"),
            "currencies": set(),
        })
        g["n"] += 1
        if r.get("currency"):
            g["currencies"].add(str(r["currency"]).upper())
        if r["retailCost"] is not None:
            g["retail"] += r["retailCost"]
            g["retail_known"] += 1
            if r.get("basisRetail") and "RetailPricesAPI" in r["basisRetail"]:
                g["enriched"] += 1
        if r["actualCost"] is not None:
            g["actual"] += r["actualCost"]
            g["actual_known"] += 1
    return groups


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _money(v):
    return "UNKNOWN" if v is None else f"{v:,.2f}"


def _pct(v):
    return "n/a" if v is None else f"{v*100:,.1f}%"


def render_report(groups, currency, fmt):
    mixed = str(currency).upper().startswith("MIXED")
    rows = []
    tot_r = tot_a = 0.0
    retail_groups = 0
    actual_groups = 0
    all_paired = True
    for k, g in sorted(groups.items(), key=lambda kv: -(kv[1]["actual"] or 0)):
        g_mixed = len(g.get("currencies") or ()) > 1
        retail_complete = g["retail_known"] == g["n"] and g["retail_known"] > 0
        retail = g["retail"] if g["retail_known"] > 0 else None
        actual = g["actual"] if g["actual_known"] > 0 else None
        # Savings/discount are only meaningful when retail and actual cover the SAME
        # rows. If any row in the group lacks retail (e.g. an ambiguous meter left
        # UNKNOWN) or lacks actual, leave savings/discount UNKNOWN rather than blend
        # sums taken over different row sets into a misleading discount.
        paired = (retail is not None and actual is not None
                  and g["retail_known"] == g["n"] and g["actual_known"] == g["n"])
        if g_mixed:
            # Rows in this group are billed in more than one currency; summing them
            # into one retail/actual figure (and a discount) would mix currencies.
            retail = actual = None
            paired = False
        savings = (retail - actual) if paired else None
        disc = (savings / retail) if (paired and retail) else None
        if g_mixed:
            note = "mixed currencies (%s) within group -- not aggregated" % ",".join(sorted(g["currencies"]))
        else:
            note = "" if retail_complete else ("retail incomplete (%d/%d rows)" % (g["retail_known"], g["n"]) if g["retail_known"] else "no retail in data")
        flags = []
        if g.get("enriched"):
            flags.append("retail=list price via Retail Prices API")
        if disc is not None and disc < 0:
            flags.append("REVIEW: actual > retail (check meter/unit match)")
        elif disc is not None and disc > 0.99:
            flags.append("REVIEW: >99% (likely unit/tier mismatch)")
        if flags:
            note = (note + "; " if note else "") + "; ".join(flags)
        if retail is not None:
            tot_r += retail
            retail_groups += 1
        if actual is not None:
            tot_a += actual
            actual_groups += 1
        if retail is not None or actual is not None:
            all_paired = all_paired and paired
        rows.append({
            "item": g["label"], "retail": retail, "actual": actual,
            "savings": savings, "discount": disc, "note": note,
        })
    # A blended TOTAL is only valid when every contributing group is fully paired
    # (retail and actual over the same rows) AND everything is in one currency.
    # Otherwise the sum mixes row sets or currencies, so the discount would be
    # misleading -- present it as incomplete/UNKNOWN rather than inventing a number.
    tot_r_full = bool(all_paired and actual_groups and retail_groups == actual_groups and not mixed)
    if mixed:
        tot_r_disp = tot_a_disp = None
        tot_note = "mixed currencies -- totals not aggregated across currencies"
    else:
        tot_r_disp = tot_r if retail_groups else None
        tot_a_disp = tot_a if actual_groups else None
        tot_note = "" if tot_r_full else "retail incomplete or missing"
    tot_sav = (tot_r_disp - tot_a_disp) if (tot_r_full and tot_r_disp is not None and tot_a_disp is not None) else None
    tot_disc = (tot_sav / tot_r_disp) if (tot_sav is not None and tot_r_disp) else None

    if fmt == "json":
        return json.dumps({
            "currency": currency, "lineItems": rows,
            "totals": {"retail": tot_r_disp, "actual": tot_a_disp, "savings": tot_sav,
                       "discount": tot_disc, "retailComplete": tot_r_full},
        }, indent=2)

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Item", f"Retail ({currency})", f"Actual ({currency})",
                    f"Savings ({currency})", "Discount%", "Note"])
        for r in rows:
            w.writerow([r["item"], _money(r["retail"]), _money(r["actual"]),
                        _money(r["savings"]), _pct(r["discount"]), r["note"]])
        w.writerow(["TOTAL", _money(tot_r_disp), _money(tot_a_disp), _money(tot_sav),
                    _pct(tot_disc), tot_note])
        return buf.getvalue()

    # markdown
    out = [f"| Item | Retail ({currency}) | Actual ({currency}) | Savings | Discount % | Note |",
           "|---|--:|--:|--:|--:|---|"]
    for r in rows:
        out.append(f"| {r['item']} | {_money(r['retail'])} | {_money(r['actual'])} | "
                   f"{_money(r['savings'])} | {_pct(r['discount'])} | {r['note']} |")
    out.append(f"| **TOTAL** | **{_money(tot_r_disp)}** | **{_money(tot_a_disp)}** | "
               f"**{_money(tot_sav)}** | **{_pct(tot_disc)}** | "
               f"{('*' + tot_note + '*') if tot_note else ''} |")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Table parsing (markdown or CSV) for reduction input
# ---------------------------------------------------------------------------
def parse_table(text):
    text = text.strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], []
    if "|" in lines[0]:
        def cells(ln):
            ln = ln.strip()
            if ln.startswith("|"):
                ln = ln[1:]
            if ln.endswith("|"):
                ln = ln[:-1]
            return [c.strip() for c in ln.split("|")]
        headers = cells(lines[0])
        body = []
        for ln in lines[1:]:
            if re.match(r"^\s*\|?\s*:?-{2,}", ln):  # separator row
                continue
            body.append(cells(ln))
        return headers, body
    # CSV
    reader = list(csv.reader(io.StringIO(text)))
    if not reader:
        return [], []
    return reader[0], reader[1:]


AMOUNT_RE = re.compile(r"(reduction|savings?|amount|cost|usd|\$|price|monthly|annual)", re.I)
LABEL_RE = re.compile(r"(resource|name|item|meter|service|description|id|sku)", re.I)


def pick_columns(headers, rows):
    amount_idx = None
    for i, h in enumerate(headers):
        if AMOUNT_RE.search(h or ""):
            amount_idx = i
            break
    if amount_idx is None:  # fallback: rightmost mostly-numeric column
        best, best_score = None, 0
        for i in range(len(headers)):
            score = sum(1 for r in rows if i < len(r) and _num(r[i]) is not None)
            if score >= best_score:
                best, best_score = i, score
        amount_idx = best
    label_idx = None
    for i, h in enumerate(headers):
        if i != amount_idx and LABEL_RE.search(h or ""):
            label_idx = i
            break
    if label_idx is None:
        label_idx = 0 if amount_idx != 0 else (1 if len(headers) > 1 else 0)
    return label_idx, amount_idx


# ---------------------------------------------------------------------------
# Savings mode
# ---------------------------------------------------------------------------
def build_ratio_index(recs):
    """Map several keys -> (retail_sum, actual_sum) so we can derive actual/retail."""
    idx = {}

    def add(key, r):
        if not key:
            return
        k = str(key).strip().lower()
        e = idx.setdefault(k, {"retail": 0.0, "actual": 0.0, "rk": 0, "ak": 0,
                               "n": 0, "cur": set()})
        e["n"] += 1
        cur = r.get("currency")
        if cur:
            e["cur"].add(str(cur).upper())
        if r["retailCost"] is not None:
            e["retail"] += r["retailCost"]; e["rk"] += 1
        if r["actualCost"] is not None:
            e["actual"] += r["actualCost"]; e["ak"] += 1

    for r in recs:
        for key in (r.get("resourceId"), r.get("resourceName"),
                    r.get("meter"), r.get("service")):
            add(key, r)
    return idx


def _boundary_contains(needle, hay):
    """True if `needle` occurs in `hay` delimited by alphanumeric-token boundaries.

    So a request for 'vm1' does NOT match inside 'vm10' (the following '0' is
    alphanumeric, i.e. the same token), but 'vm-app01' still matches inside
    '/subscriptions/.../vm-app01' (bounded by '/' and end-of-string). This keeps
    fuzzy/substring matching from silently picking a different resource that
    merely shares a prefix."""
    if not needle or needle not in hay:
        return False
    n = len(needle)
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return False
        before = hay[i - 1] if i > 0 else ""
        after = hay[i + n] if i + n < len(hay) else ""
        if not before.isalnum() and not after.isalnum():
            return True
        start = i + 1


def _entry_ratio(cand, basis):
    """(ratio, basis) for one index entry, or (None, reason) if unusable.

    Requires retail AND actual to cover EVERY row of the matched key. If retail
    is known for fewer rows than actual (or vice-versa), the ratio would be taken
    over different row sets -- a guess -- so it is refused. A key whose rows span
    more than one currency is also refused: actual/retail summed across currencies
    is not a meaningful ratio."""
    if len(cand.get("cur", ())) > 1:
        return None, ("matched but spans multiple currencies (%s)"
                      % ",".join(sorted(cand["cur"])))
    if cand["rk"] == 0 or cand["retail"] == 0:
        return None, "matched but no retail in cost data"
    if cand["ak"] == 0:
        return None, "matched but no actual in cost data"
    if cand["rk"] != cand["n"] or cand["ak"] != cand["n"]:
        return None, ("matched but retail/actual incomplete (%d/%d retail, %d/%d actual rows)"
                      % (cand["rk"], cand["n"], cand["ak"], cand["n"]))
    return cand["actual"] / cand["retail"], basis


def match_ratio(label, idx):
    """Return (ratio, basis) or (None, reason). ratio = actual/retail.

    Exact label match wins. A substring match is only used when it is
    unambiguous: it must occur at a token boundary (so 'vm1' does not match
    'vm10'), and EVERY key it matches must be usable (complete retail+actual
    coverage, non-zero retail, single currency) AND agree on the ratio. If the
    label matches a key we cannot compute -- e.g. a second resource with no
    retail -- the intended target is not determined by the data, so it is
    refused (UNMATCHED) rather than guessing which resource was meant. Several
    matches that agree on the ratio are safe -- the result is identical either
    way."""
    q = str(label).strip().lower()
    if not q:
        return None, "empty label"
    exact = idx.get(q)
    if exact is not None:
        return _entry_ratio(exact, "exact")
    hits = [v for k, v in idx.items() if _boundary_contains(q, k) or _boundary_contains(k, q)]
    if not hits:
        return None, "no match in cost data"
    # Never guess: a substring label that also matches a key we cannot compute
    # (incomplete coverage, no retail, or mixed currency) is ambiguous -- it may
    # refer to that resource -- so refuse instead of silently using the
    # computable subset.
    resolved = [_entry_ratio(v, "substring") for v in hits]
    if any(r is None for r, _ in resolved):
        return None, "ambiguous (matches a resource with incomplete or unusable cost data)"
    ratios = {round(r, 8) for r, _ in resolved}
    if len(ratios) > 1:
        return None, "ambiguous (matches multiple resources with different discounts)"
    return resolved[0][0], (
        "substring" if len(hits) == 1 else "substring(multiple, same ratio)")


def render_savings(items, currency, fmt):
    tot_retail = tot_actual = 0.0
    all_matched = True
    for it in items:
        if it["actualReduction"] is not None:
            tot_retail += it["retailReduction"] or 0
            tot_actual += it["actualReduction"]
        else:
            all_matched = False
    tot_disc = ((tot_retail - tot_actual) / tot_retail) if tot_retail else None

    if fmt == "json":
        return json.dumps({"currency": currency, "lineItems": items,
                           "totals": {"retailReduction": tot_retail,
                                      "actualReduction": tot_actual,
                                      "effectiveDiscount": tot_disc,
                                      "allMatched": all_matched}}, indent=2)
    if fmt == "csv":
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(["Item", f"Retail reduction ({currency})", "Actual/Retail ratio",
                    f"Actual reduction ({currency})", "Match basis"])
        for it in items:
            w.writerow([it["item"], _money(it["retailReduction"]),
                        _pct(it["ratio"]) if it["ratio"] is not None else "UNMATCHED",
                        _money(it["actualReduction"]), it["basis"]])
        w.writerow(["TOTAL", _money(tot_retail), _pct(1 - tot_disc) if tot_disc is not None else "n/a",
                    _money(tot_actual), "" if all_matched else "some UNMATCHED"])
        return buf.getvalue()

    out = [f"| Item | Retail reduction ({currency}) | Actual/Retail | Actual reduction ({currency}) | Match basis |",
           "|---|--:|--:|--:|---|"]
    for it in items:
        ratio = _pct(it["ratio"]) if it["ratio"] is not None else "**UNMATCHED**"
        out.append(f"| {it['item']} | {_money(it['retailReduction'])} | {ratio} | "
                   f"{_money(it['actualReduction'])} | {it['basis']} |")
    out.append(f"| **TOTAL** | **{_money(tot_retail)}** | n/a | **{_money(tot_actual)}** | "
               f"{'' if all_matched else '*some UNMATCHED - not counted*'} |")
    if not all_matched:
        out.append("\n> UNMATCHED line items have no backing cost data and are **excluded** "
                   "from the total. Provide cost data covering these resources to compute them.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Delta mode (change in ACTUAL cost between two periods / projected run-rate)
# ---------------------------------------------------------------------------
DAYS_PER_MONTH = 30.4375  # average calendar month, for run-rate normalization


def _parse_date(s):
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    t = t.split("T")[0].split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def infer_days(recs):
    """Distinct calendar days present in the data (window length), or None."""
    days = {d for d in (_parse_date(r.get("date")) for r in recs) if d}
    return len(days) or None


def resource_actuals(recs, mode):
    """key -> aggregated ACTUAL cost for one dataset (retail ignored here)."""
    out = {}
    for r in recs:
        k = group_key(r, mode)
        if k is None:
            k = id(r)
        g = out.setdefault(k, {
            "label": label_for(r, mode), "actual": 0.0, "actual_known": 0, "n": 0,
            "service": r.get("service"), "meter": r.get("meter"),
            "resourceName": r.get("resourceName"), "resourceId": r.get("resourceId"),
            "currencies": set(),
        })
        g["n"] += 1
        if r.get("currency"):
            g["currencies"].add(str(r["currency"]).upper())
        if r["actualCost"] is not None:
            g["actual"] += r["actualCost"]
            g["actual_known"] += 1
    return out


def _match_keys(requested, groups):
    """Given requested identifiers, return (matched_key_set, unmatched_list).
    Matches on exact key, substring of key, or substring of the group's
    resourceId / resourceName / label. Never fabricates a match."""
    matched, unmatched = set(), []
    keyinfo = {}
    for k, g in groups.items():
        blob = " ".join(str(x).lower() for x in
                        (k, g.get("resourceId"), g.get("resourceName"), g.get("label"))
                        if x is not None)
        keyinfo[k] = blob
    for req in requested:
        q = str(req).strip().lower()
        if not q:
            continue
        # Exact match wins (on the group key, resourceId, or resourceName), so a
        # request for "vm1" does not also drag in "vm10" via substring.
        exact = [k for k, g in groups.items()
                 if q == str(k).lower()
                 or q == str(g.get("resourceId") or "").lower()
                 or q == str(g.get("resourceName") or "").lower()]
        if exact:
            matched.update(exact)
            continue
        hits = [k for k, blob in keyinfo.items() if _boundary_contains(q, blob)]
        if hits:
            matched.update(hits)
        else:
            unmatched.append(req)
    return matched, unmatched


def read_resource_list(text):
    """Parse a --resources input into a list of requested identifiers.

    Accepts a plain list (one resourceId/name per line, no header), a markdown
    table, or a CSV. For tables the identifier column is used and the header row
    is NOT treated as a requested resource. A plain one-per-line list keeps EVERY
    line (including the first) -- so no resource is silently dropped."""
    text = (text or "").strip()
    if not text:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    is_pipe = any("|" in ln for ln in lines)
    is_csv = (not is_pipe) and any("," in ln for ln in lines)
    if not is_pipe and not is_csv:
        return [ln.strip() for ln in lines]  # plain list, no header
    headers, body = parse_table(text)
    idx = None
    for i, h in enumerate(headers):
        if LABEL_RE.search(h or ""):
            idx = i
            break
    out = []
    if idx is not None:  # header names an id/resource column -> skip header, take that column
        for row in body:
            if idx < len(row) and str(row[idx]).strip():
                out.append(str(row[idx]).strip())
    else:  # no obvious id column: treat as headerless -> first cell of every row
        for row in ([headers] + body):
            if row and str(row[0]).strip():
                out.append(str(row[0]).strip())
    return out


def _group_actual(g):
    """Actual cost of a resource-group, or None when it cannot be trusted.

    A group whose rows are only partly populated with actual cost
    (actual_known < n) -- or has none at all -- is UNKNOWN rather than an
    incomplete/zero sum. Legacy dicts that carry no coverage info are trusted
    as-is (used by unit tests that build minimal groups)."""
    if g is None:
        return None
    ak = g.get("actual_known")
    n = g.get("n")
    if ak is None or n is None:
        return g.get("actual")
    if ak == 0 or ak != n:
        return None
    return g.get("actual")


def compute_delta(before, after, before_days, after_days, keep_keys, decreases_only):
    """Build delta rows. `after` may be None (single-period run-rate mode)."""
    single = after is None
    keys = set(before) | (set() if single else set(after))
    if keep_keys is not None:
        keys &= keep_keys
    normalize_rate = bool(before_days) and (single or bool(after_days))

    rows = []
    for k in keys:
        b = before.get(k)
        a = None if single else after.get(k)
        src = b or a
        curs = set()
        if b:
            curs |= (b.get("currencies") or set())
        if a:
            curs |= (a.get("currencies") or set())
        row_mixed = len(curs) > 1
        b_present = b is not None
        a_present = (not single) and a is not None
        b_actual = _group_actual(b)
        a_actual = _group_actual(a)
        b_unknown = b_present and b_actual is None
        a_unknown = a_present and a_actual is None

        if single:
            if row_mixed:
                rows.append({
                    "item": src["label"], "before": None, "after": None,
                    "deltaRaw": None, "deltaMonth": None, "pct": None,
                    "status": "UNKNOWN (mixed currency)",
                })
                continue
            # projected monthly run-rate reduction if this resource is removed
            month = (b_actual / before_days * DAYS_PER_MONTH) if (b_actual is not None and before_days) else None
            rows.append({
                "item": src["label"], "before": b_actual, "after": None,
                "deltaRaw": (-b_actual if b_actual is not None else None),
                "deltaMonth": (-month if month is not None else None),
                "pct": (-1.0 if b_actual else None),
                "status": "RUN-RATE" if b_actual is not None else "UNKNOWN (actual unknown)",
            })
            continue

        if b_present and not a_present:
            status = "REMOVED"
        elif a_present and not b_present:
            status = "ADDED"
        else:
            status = "CHANGED"

        # Never guess: if the resource's rows span multiple currencies, or a side
        # that exists has unknown actual cost, we cannot compute an honest delta.
        if row_mixed or b_unknown or a_unknown:
            reason = "mixed currency" if row_mixed else "actual unknown"
            rows.append({
                "item": src["label"],
                "before": (None if (b_unknown or row_mixed) else b_actual),
                "after": (None if (a_unknown or row_mixed) else a_actual),
                "deltaRaw": None, "deltaMonth": None, "pct": None,
                "status": "UNKNOWN (%s)" % reason,
            })
            continue

        bb = b_actual or 0.0
        aa = a_actual or 0.0
        delta_raw = aa - bb
        if normalize_rate:
            b_month = bb / before_days * DAYS_PER_MONTH
            a_month = aa / after_days * DAYS_PER_MONTH
            delta_month = a_month - b_month
            # When windows differ in length the raw delta is misleading (a 30-day
            # $300 window vs a 15-day $150 window is the SAME run-rate). Base the
            # %, UNCHANGED status, sorting and --decreases-only on the normalized
            # monthly figures so an unchanged run-rate is not reported as a drop.
            eff_delta, eff_base = delta_month, b_month
        else:
            delta_month = None
            eff_delta, eff_base = delta_raw, bb
        pct = (eff_delta / eff_base) if eff_base else None
        if status == "CHANGED" and abs(eff_delta) < 1e-9:
            status = "UNCHANGED"
        rows.append({
            "item": src["label"], "before": b_actual, "after": a_actual,
            "deltaRaw": delta_raw, "deltaMonth": delta_month,
            "pct": pct, "status": status,
        })

    def _eff(r):
        return r["deltaMonth"] if (normalize_rate and r["deltaMonth"] is not None) else r["deltaRaw"]

    if decreases_only:
        rows = [r for r in rows if (_eff(r) is not None and _eff(r) < 0)]
    rows.sort(key=lambda r: (_eff(r) if _eff(r) is not None else 0))  # biggest decrease first
    return rows, normalize_rate, single


def render_delta(rows, unmatched, currency, fmt, normalize_rate, single, before_days, after_days):
    mixed = str(currency).upper().startswith("MIXED")
    n_unknown = sum(1 for r in rows if str(r.get("status", "")).startswith("UNKNOWN"))
    # UNKNOWN rows have no trustworthy delta; keep a present-but-partial side out
    # of every total so an UNKNOWN row's lone before/after cannot masquerade as a
    # complete figure (or make the delta read as 0.00 = "no change").
    counted = [r for r in rows if not str(r.get("status", "")).startswith("UNKNOWN")]
    tot_before = sum(r["before"] for r in counted if r["before"] is not None)
    tot_after = 0.0 if single else sum(r["after"] for r in counted if r["after"] is not None)
    tot_draw = sum(r["deltaRaw"] for r in counted if r["deltaRaw"] is not None)
    tot_dmon = sum(r["deltaMonth"] for r in counted if r["deltaMonth"] is not None) if normalize_rate else None
    if not counted and rows:
        # Every line item is UNKNOWN: an empty sum is 0.0, which would read as a
        # real "$0 / no change". Present totals as UNKNOWN rather than fabricate 0.
        tot_before = tot_after = tot_draw = tot_dmon = None
    if mixed:
        # Summing actual across currencies is meaningless; keep per-resource rows
        # (each in its own currency) but do not present an aggregated total.
        tot_before = tot_after = tot_draw = tot_dmon = None

    if fmt == "json":
        return json.dumps({
            "currency": currency, "mode": "run-rate" if single else "two-period",
            "beforeDays": before_days, "afterDays": after_days,
            "normalizedMonthly": normalize_rate, "lineItems": rows,
            "unmatchedRequested": unmatched,
            "totals": {"before": tot_before, "after": (None if single else tot_after),
                       "deltaActual": tot_draw, "deltaMonthly": tot_dmon},
        }, indent=2)

    dcol = "Proj. change/mo if removed" if single else ("Chg actual/mo" if normalize_rate else "Chg actual")
    if fmt == "csv":
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(["Resource", f"Before ({currency})", f"After ({currency})",
                    f"Chg actual ({currency})", f"{dcol} ({currency})", "% change", "Status"])
        for r in rows:
            w.writerow([r["item"], _money(r["before"]), _money(r["after"]),
                        _signed(r["deltaRaw"]), _signed(r["deltaMonth"]),
                        _pct(r["pct"]), r["status"]])
        w.writerow(["TOTAL", _money(tot_before), ("" if single else _money(tot_after)),
                    _signed(tot_draw), _signed(tot_dmon), "", ""])
        return buf.getvalue()

    hdr_after = "" if single else f" After ({currency}) |"
    out = [f"| Resource | Before ({currency}) |{hdr_after} Chg actual ({currency}) | {dcol} | % change | Status |",
           "|---|--:|" + ("" if single else "--:|") + "--:|--:|--:|---|"]
    for r in rows:
        after_cell = "" if single else f" {_money(r['after'])} |"
        out.append(f"| {r['item']} | {_money(r['before'])} |{after_cell} "
                   f"{_signed(r['deltaRaw'])} | {_signed(r['deltaMonth'])} | "
                   f"{_pct(r['pct'])} | {r['status']} |")
    tot_after_cell = "" if single else f" **{_money(tot_after)}** |"
    out.append(f"| **TOTAL** | **{_money(tot_before)}** |{tot_after_cell} "
               f"**{_signed(tot_draw)}** | **{_signed(tot_dmon)}** | | |")
    note = []
    if mixed:
        note.append(f"Cost data spans multiple currencies ({currency}); per-resource rows keep their "
                    "own currency but totals are NOT aggregated across currencies.")
    if normalize_rate:
        note.append(f"Monthly figures are run-rate: actual / window-days x {DAYS_PER_MONTH:g} "
                    f"(before={before_days}d" + ("" if single else f", after={after_days}d") + ").")
    else:
        if single:
            note.append("Window length (days) is unknown "
                        f"(before={before_days}): change shown as a raw total for the supplied "
                        "window. Pass --before-days (or provide dated data) for a monthly run-rate.")
        else:
            note.append("Window length is unknown for at least one period "
                        f"(before={before_days}, after={after_days}): change shown as raw totals for the "
                        "supplied windows. Pass --before-days/--after-days (or provide dated data for both "
                        "periods) for a monthly run-rate.")
    note.append("Negative change = actual cost went DOWN (a reduction). Every figure is actual Cost "
                "Management data; nothing is projected beyond the stated run-rate.")
    if n_unknown and not mixed:
        note.append(f"{n_unknown} line item(s) are UNKNOWN (unknown actual cost or mixed currency) and are "
                    "EXCLUDED from the TOTAL, which therefore covers only line items with complete data.")
    if single:
        note.append("RUN-RATE assumes the resource keeps its recent actual spend; if it is covered "
                    "by a reservation/savings plan, cash savings may lag until that commitment is reallocated or expires.")
    if unmatched:
        note.append("NOT FOUND in cost data (no actual-cost backing, excluded): "
                    + ", ".join(str(u) for u in unmatched) + ".")
    out.append("\n> " + " ".join(note))
    return "\n".join(out)


def _signed(v):
    if v is None:
        return "UNKNOWN"
    return ("+" if v > 0 else "") + f"{v:,.2f}"


# ---------------------------------------------------------------------------
# Currency detection
# ---------------------------------------------------------------------------
def detect_currency(recs, override):
    """Currency to label/aggregate by, following a never-guess precedence:

    1. Genuinely mixed source data -> MIXED(...) (never coerced, even with an
       override), so cross-currency rows are never summed into one total.
    2. An explicit --currency override (relabels single/unlabeled data).
    3. The one currency actually present in the data.
    4. DEFAULT_CURRENCY (USD) when the data carries no currency and none is given
       -- a labeling assumption only; it never changes a cost figure."""
    seen = {str(r.get("currency")).upper() for r in recs if r.get("currency")}
    seen.discard("NONE")
    if len(seen) > 1:
        return "MIXED(" + ",".join(sorted(seen)) + ")"
    if override:
        return override
    if len(seen) == 1:
        return seen.pop()
    return DEFAULT_CURRENCY


def _read(path):
    if path in ("-", None):
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def main():
    ap = argparse.ArgumentParser(description="Retail vs actual Azure cost — backed only by real data.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="Retail vs actual vs percent-difference.")
    r.add_argument("--input", "-i", default="-", help="Cost data file (JSON/CSV) or - for stdin.")
    r.add_argument("--group-by", default="resource",
                   choices=["resource", "meter", "service", "subscription", "row"])
    r.add_argument("--retail-map", default=None,
                   help="Optional meterId->retailPrice map (Retail Prices API JSON, or CSV/JSON) "
                        "to fill retail on data where PayG fields are zero or absent.")
    r.add_argument("--retail-currency", default=DEFAULT_CURRENCY,
                   help="Currency of the --retail-map list prices (default USD); rows billed in "
                        "another currency are left UNKNOWN rather than mixing currencies.")
    r.add_argument("--currency", default=None,
                   help="Force the display currency label (default: the currency found in the "
                        "data, else USD). Mixed-currency data is never coerced to one currency.")
    r.add_argument("--format", default="md", choices=["md", "csv", "json"])

    s = sub.add_parser("savings", help="Actual savings for a pasted USD reduction table.")
    s.add_argument("--cost", "-c", required=True, help="Cost data file (JSON/CSV).")
    s.add_argument("--reductions", "-r", required=True, help="Reduction table (markdown/CSV).")
    s.add_argument("--retail-map", default=None, help="Optional meterId->retailPrice map (see report).")
    s.add_argument("--retail-currency", default=DEFAULT_CURRENCY,
                   help="Currency of the --retail-map list prices (default USD).")
    s.add_argument("--currency", default=None,
                   help="Force the display currency label (default: the currency found in the "
                        "data, else USD). Mixed-currency data is never coerced to one currency.")
    s.add_argument("--format", default="md", choices=["md", "csv", "json"])

    m = sub.add_parser("meters", help="List distinct meterIds lacking retail, with a fetch snippet.")
    m.add_argument("--input", "-i", default="-", help="Cost data file (JSON/CSV) or - for stdin.")

    d = sub.add_parser("delta", help="Change in ACTUAL cost between two periods, or run-rate if a resource is removed.")
    d.add_argument("--before", "-b", default=None, help="Baseline-period cost data (JSON/CSV). Alias: --input.")
    d.add_argument("--input", "-i", dest="before", help=argparse.SUPPRESS)
    d.add_argument("--after", "-a", default=None,
                   help="Later-period cost data. Omit for single-period run-rate (projected reduction if removed).")
    d.add_argument("--resources", "-R", default=None,
                   help="Optional file of resourceIds/names (one per line, or a table) to restrict to.")
    d.add_argument("--group-by", default="resource",
                   choices=["resource", "meter", "service", "subscription"])
    d.add_argument("--before-days", type=float, default=None, help="Baseline window length in days (else inferred from dates).")
    d.add_argument("--after-days", type=float, default=None, help="Later window length in days (else inferred from dates).")
    d.add_argument("--decreases-only", action="store_true", help="Show only line items whose actual cost went down.")
    d.add_argument("--currency", default=None,
                   help="Force the display currency label (default: the currency found in the "
                        "data, else USD). Mixed-currency data is never coerced to one currency.")
    d.add_argument("--format", default="md", choices=["md", "csv", "json"])

    args = ap.parse_args()

    if args.cmd == "report":
        rows, shape = load_rows(_read(args.input))
        recs = [normalize(r) for r in rows]
        currency = detect_currency(recs, args.currency)
        enriched = enrich_retail(recs, load_retail_map(args.retail_map),
                                 args.retail_currency) if args.retail_map else 0
        groups = aggregate(recs, args.group_by)
        sys.stderr.write(f"[info] shape={shape} rows={len(recs)} groups={len(groups)} "
                         f"currency={currency} retail_enriched={enriched}\n")
        print(render_report(groups, currency, args.format))
        return

    if args.cmd == "meters":
        rows, shape = load_rows(_read(args.input))
        recs = [normalize(r) for r in rows]
        need = {}
        for r in recs:
            if r["retailCost"] is None and r.get("meterId"):
                mid = str(r["meterId"]).strip()
                e = need.setdefault(mid, {"meter": r.get("meter"), "region": r.get("region"),
                                          "service": r.get("service"), "actual": 0.0, "cur": set()})
                c = r.get("currency")
                if c:
                    e["cur"].add(str(c).upper())
                if r["actualCost"]:
                    e["actual"] += r["actualCost"]
        sys.stderr.write(f"[info] shape={shape} rows={len(recs)} meters_needing_retail={len(need)}\n")
        currency = detect_currency(recs, None)
        cur = DEFAULT_CURRENCY if str(currency).upper().startswith("MIXED") or not currency else currency
        print(f"# {len(need)} distinct meterId(s) lack retail price and need Retail Prices API lookup.\n")
        print("| meterId | meterName | region | service | actual so far |")
        print("|---|---|---|---|--:|")
        for mid, e in sorted(need.items(), key=lambda kv: -kv[1]["actual"]):
            # A meterId whose rows span >1 currency cannot show one honest "actual
            # so far" figure; this column is only a fetch-priority hint, so mark it
            # UNKNOWN rather than blend currencies.
            actual_cell = "UNKNOWN (mixed)" if len(e["cur"]) > 1 else f"{e['actual']:,.2f}"
            print(f"| {mid} | {e['meter']} | {e['region']} | {e['service']} | {actual_cell} |")
        ids = list(need.keys())
        print("\n## Fetch retail for these meters (no auth needed)\n")
        if str(currency).upper().startswith("MIXED"):
            print(f"> NOTE: cost data spans multiple currencies ({currency}); the snippet below fetches "
                  f"**{cur}** list prices. Fetch each currency separately and enrich with a matching "
                  "`--retail-currency`.\n")
        print("**Preferred:** if the skill's scripts are available, this handles ambiguous meterIds safely:\n")
        print("```bash")
        print(f"python scripts/fetch_retail.py --input <cost.csv> --currency {cur} --out retail-map.csv")
        print("```")
        print("\n**Cloud Shell fallback** (paste into the target tenant; only emits a price when a "
              "meterId resolves to a single base-tier Consumption price — ambiguous meters are left "
              "blank rather than guessed):\n")
        print("```bash")
        print("cat > /tmp/meterids.txt <<'EOF'")
        for mid in ids:
            print(mid)
        print("EOF")
        print((
            'echo "meterId,retailPrice" > /tmp/retail-map.csv\n'
            "while read MID; do\n"
            '  [ -z "$MID" ] && continue\n'
            "  P=$(curl -s \"https://prices.azure.com/api/retail/prices?currencyCode='CUR'&\\$filter=meterId eq '$MID' and priceType eq 'Consumption'\" \\\n"
            "      | python3 -c \"import sys,json;items=[i for i in json.load(sys.stdin).get('Items',[]) if float(i.get('tierMinimumUnits',0) or 0)==0];ps=sorted({round(float(i['retailPrice']),10) for i in items});print(ps[0] if len(ps)==1 else '')\")\n"
            '  echo "$MID,$P" >> /tmp/retail-map.csv\n'
            "done < /tmp/meterids.txt\n"
            "cat /tmp/retail-map.csv").replace("currencyCode='CUR'", f"currencyCode='{cur}'"))
        print("```")
        print("\nPaste the resulting CSV back, then run: "
              "`python scripts/actualize.py report --input <cost> --retail-map <pasted-map.csv>`. "
              "Blank retail = ambiguous/notfound meter, left UNKNOWN (never guessed).")
        return

    if args.cmd == "savings":
        rows, shape = load_rows(_read(args.cost))
        recs = [normalize(r) for r in rows]
        if args.retail_map:
            enrich_retail(recs, load_retail_map(args.retail_map), args.retail_currency)
        currency = detect_currency(recs, args.currency)
        idx = build_ratio_index(recs)
        headers, body = parse_table(_read(args.reductions))
        li, ai = pick_columns(headers, body)
        items = []
        for row in body:
            if ai >= len(row):
                continue
            label = row[li] if li < len(row) else ""
            amt = _num(row[ai])
            ratio, basis = match_ratio(label, idx)
            actual = (amt * ratio) if (amt is not None and ratio is not None) else None
            items.append({"item": label, "retailReduction": amt, "ratio": ratio,
                          "actualReduction": actual,
                          "basis": basis if ratio is not None else f"UNMATCHED ({basis})"})
        sys.stderr.write(f"[info] cost shape={shape} rows={len(recs)} "
                         f"label_col='{headers[li] if li < len(headers) else '?'}' "
                         f"amount_col='{headers[ai] if ai < len(headers) else '?'}' currency={currency}\n")
        print(render_savings(items, currency, args.format))
        return

    if args.cmd == "delta":
        if not args.before:
            ap.error("delta needs --before (baseline cost data); add --after for a two-period comparison.")
        before_recs = [normalize(r) for r in load_rows(_read(args.before))[0]]
        after_recs = None
        if args.after:
            after_recs = [normalize(r) for r in load_rows(_read(args.after))[0]]
        currency = detect_currency(before_recs + (after_recs or []), args.currency)

        before = resource_actuals(before_recs, args.group_by)
        after = resource_actuals(after_recs, args.group_by) if after_recs is not None else None
        before_days = args.before_days or infer_days(before_recs)
        after_days = args.after_days or (infer_days(after_recs) if after_recs is not None else None)

        keep_keys = None
        unmatched = []
        if args.resources:
            requested = read_resource_list(_read(args.resources))
            allg = dict(before)
            if after:
                allg.update(after)
            keep_keys, unmatched = _match_keys(requested, allg)

        rows, normalize_rate, single = compute_delta(
            before, after, before_days, after_days, keep_keys, args.decreases_only)
        sys.stderr.write(f"[info] mode={'run-rate' if single else 'two-period'} "
                         f"before_rows={len(before_recs)} after_rows={len(after_recs) if after_recs is not None else 0} "
                         f"before_days={before_days} after_days={after_days} "
                         f"normalized_monthly={normalize_rate} currency={currency} "
                         f"line_items={len(rows)} unmatched={len(unmatched)}\n")
        print(render_delta(rows, unmatched, currency, args.format,
                           normalize_rate, single, before_days, after_days))
        return


if __name__ == "__main__":
    main()
