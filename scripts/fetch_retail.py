#!/usr/bin/env python3
"""fetch_retail.py - fetch published RETAIL list prices from the Azure Retail
Prices API (anonymous, no auth) for a set of meterIds, so that cost data which
omits or zeroes PayGPrice can be enriched with retail for comparison.

NEVER-GUESS RULE
----------------
A single Azure `meterId` can be reused across MANY SKUs (e.g. SQL "1 vCore" .. "80
vCore" all share one meterId at different unit prices). Picking one arbitrarily
would be a guess. This tool therefore:
  * uses a retail unit price ONLY when a meterId resolves to exactly ONE distinct
    Consumption unit price (tierMinimumUnits == 0)  -> status "ok"
  * marks a meterId that resolves to several distinct prices as "ambiguous" and
    emits NO price for it (leave retail UNKNOWN; a human must disambiguate by SKU)
  * marks "notfound" when the catalog returns no base Consumption record.

Input (choose one):
  --meters-file PATH   one meterId per line
  --input COSTCSV      a Cost Details CSV; distinct billed meterIds are extracted
  (stdin)              one meterId per line if neither flag is given

Output:
  --out PATH           write CSV (default: stdout). Columns:
                         meterId,retailPrice,unitOfMeasure,status,skuCount,prices
  A human-readable summary of ok / ambiguous / notfound goes to stderr.
  Rows with status "ok" carry a retailPrice; ambiguous/notfound rows leave it
  blank so actualize.py --retail-map will keep retail UNKNOWN (never guess).

Usage:
  python fetch_retail.py --input costdetails.csv --out retail-map.csv
  python fetch_retail.py --meters-file meters.txt --currency USD --out map.csv
"""
import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request

API = "https://prices.azure.com/api/retail/prices"


def read_meterids(args):
    ids = []
    if args.meters_file:
        with open(args.meters_file, encoding="utf-8-sig") as f:
            ids = [ln.strip() for ln in f if ln.strip()]
    elif args.input:
        ids = list(meterids_from_costcsv(args.input))
    else:
        ids = [ln.strip() for ln in sys.stdin if ln.strip()]
    # de-dup, preserve order
    seen, out = set(), []
    for m in ids:
        lm = m.lower()
        if lm not in seen:
            seen.add(lm)
            out.append(m)
    return out


def meterids_from_costcsv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        hdr = r.fieldnames or []

        def col(name):
            for h in hdr:
                if h.lower() == name.lower():
                    return h
            return None

        c_mid = col("meterId")
        c_cost = col("costInBillingCurrency") or col("cost") or col("BilledCost")
        if not c_mid:
            return set()
        out = set()
        for row in r:
            mid = (row.get(c_mid) or "").strip()
            try:
                cost = float(row.get(c_cost) or 0) if c_cost else 1
            except ValueError:
                cost = 0
            if mid and cost > 0:
                out.add(mid)
        return out


def http_get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < 4:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return None


def fetch_batch(meterids, currency):
    """Return {meterId(lower): [records]} for a batch of meterIds."""
    ors = " or ".join("meterId eq '%s'" % m for m in meterids)
    flt = "priceType eq 'Consumption' and (%s)" % ors
    url = "%s?currencyCode='%s'&$filter=%s" % (
        API, currency, urllib.parse.quote(flt, safe="()'"))
    out = {}
    page = 0
    while url and page < 50:
        data = http_get(url)
        if not data:
            break
        for it in data.get("Items", []):
            mid = (it.get("meterId") or "").strip().lower()
            if mid:
                out.setdefault(mid, []).append(it)
        url = data.get("NextPageLink")
        page += 1
    return out


def resolve(records):
    """Given retail records for one meterId, return (price, uom, status, skus).
    ok = exactly one distinct base (tierMinimumUnits==0) Consumption price."""
    base = [r for r in records
            if str(r.get("type", "")).lower() == "consumption"
            and float(r.get("tierMinimumUnits", 0) or 0) == 0]
    if not base:
        return (None, None, "notfound", [])
    # distinct unit prices
    by_price = {}
    for r in base:
        p = round(float(r.get("retailPrice", 0) or 0), 10)
        by_price.setdefault(p, []).append(r.get("skuName") or r.get("productName") or "")
    if len(by_price) == 1:
        p = next(iter(by_price))
        uom = base[0].get("unitOfMeasure")
        return (p, uom, "ok", sorted(set(by_price[p])))
    # ambiguous: several distinct prices under one meterId
    skus = sorted({"%s=%s" % (s, pr) for pr, ss in by_price.items() for s in ss})
    return (None, base[0].get("unitOfMeasure"), "ambiguous", skus)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch retail list prices by meterId (never guesses on ambiguous meters).")
    ap.add_argument("--meters-file")
    ap.add_argument("--input", help="Cost Details CSV to extract billed meterIds from")
    ap.add_argument("--currency", default="USD",
                    help="Currency for the Retail Prices API lookup (default USD, which is also the "
                         "API default). Enrich the resulting map with a matching --retail-currency.")
    ap.add_argument("--batch", type=int, default=15)
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    meterids = read_meterids(args)
    if not meterids:
        sys.stderr.write("No meterIds provided.\n")
        return 2
    sys.stderr.write("Fetching retail for %d meterIds (batch=%d)...\n" % (len(meterids), args.batch))

    resolved = {}
    for i in range(0, len(meterids), args.batch):
        batch = meterids[i:i + args.batch]
        found = fetch_batch(batch, args.currency)
        for m in batch:
            resolved[m] = resolve(found.get(m.lower(), []))
        sys.stderr.write("  %d/%d\r" % (min(i + args.batch, len(meterids)), len(meterids)))
    sys.stderr.write("\n")

    ok = amb = nf = 0
    rows = []
    for m in meterids:
        price, uom, status, skus = resolved[m]
        if status == "ok":
            ok += 1
        elif status == "ambiguous":
            amb += 1
        else:
            nf += 1
        rows.append({
            "meterId": m,
            "retailPrice": "" if price is None else ("%.10g" % price),
            "unitOfMeasure": uom or "",
            "status": status,
            "skuCount": len(skus),
            "prices": " | ".join(skus)[:400],
        })

    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    w = csv.DictWriter(out, fieldnames=["meterId", "retailPrice", "unitOfMeasure", "status", "skuCount", "prices"])
    w.writeheader()
    for r in rows:
        w.writerow(r)
    if args.out:
        out.close()

    sys.stderr.write("Resolved: %d ok, %d ambiguous (retail left UNKNOWN - disambiguate by SKU), %d notfound.\n" % (ok, amb, nf))
    if amb:
        sys.stderr.write("Ambiguous meterIds (one meterId, several SKU prices) - NOT guessed:\n")
        for r in rows:
            if r["status"] == "ambiguous":
                sys.stderr.write("  %s : %s\n" % (r["meterId"], r["prices"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
