#!/usr/bin/env python3
"""Deterministic tests for the Actualize calculator.

Run:  python tests/test_actualize.py        (stdlib unittest, no third-party deps)
      python -m unittest discover -s tests

These assert exact numbers so the "never guess / always backed" guarantees
cannot silently regress. The network is never touched: fetch_retail is tested
via its pure resolve() logic on synthetic catalog records.
"""
import json
import os
import sys
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SAMPLES = os.path.join(ROOT, "samples")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import actualize  # noqa: E402
import fetch_retail  # noqa: E402


def _read(name):
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8-sig") as f:
        return f.read()


def _recs(name):
    rows, _ = actualize.load_rows(_read(name))
    return [actualize.normalize(r) for r in rows]


def _totals(name, group_by="resource"):
    recs = _recs(name)
    out = actualize.render_report(actualize.aggregate(recs, group_by), "USD", "json")
    return json.loads(out)["totals"]


class ReportMath(unittest.TestCase):
    def test_modern_totals(self):
        t = _totals("modern-usage.json")
        self.assertAlmostEqual(t["retail"], 133.32, places=2)
        self.assertAlmostEqual(t["actual"], 103.35, places=2)
        self.assertAlmostEqual(t["discount"], 0.2248, places=3)
        self.assertTrue(t["retailComplete"])  # every row has retail

    def test_partial_retail_total_flagged_incomplete(self):
        # Enrich only the unambiguous meters; the ambiguous one stays UNKNOWN, so the
        # blended total must be flagged incomplete AND its savings/discount left UNKNOWN
        # (never presented as a clean number blended over mismatched row sets).
        recs = _recs("zeroed-retail.json")
        actualize.enrich_retail(recs, actualize.load_retail_map(
            os.path.join(SAMPLES, "retail-map.csv")))
        t = json.loads(actualize.render_report(
            actualize.aggregate(recs, "resource"), "USD", "json"))["totals"]
        self.assertFalse(t["retailComplete"])
        self.assertAlmostEqual(t["retail"], 89.82, places=2)   # only the 2 resolved rows
        self.assertAlmostEqual(t["actual"], 159.85, places=2)  # all 3 rows
        self.assertIsNone(t["savings"])   # retail covers fewer rows than actual -> UNKNOWN
        self.assertIsNone(t["discount"])  # not a real -78% discount

    def test_partial_retail_group_savings_unknown(self):
        # A single group whose rows are partly retail-known must not show a discount.
        recs = [actualize.normalize({"resourceName": "svc", "payGPrice": 10, "quantity": 1,
                                     "costInBillingCurrency": 6, "meterId": "M1"}),
                actualize.normalize({"resourceName": "svc", "payGPrice": 0, "quantity": 1,
                                     "costInBillingCurrency": 4, "meterId": "M2"})]
        li = json.loads(actualize.render_report(
            actualize.aggregate(recs, "resource"), "USD", "json"))["lineItems"][0]
        self.assertAlmostEqual(li["retail"], 10.0, places=2)   # only the retail-known row
        self.assertAlmostEqual(li["actual"], 10.0, places=2)   # both rows
        self.assertIsNone(li["savings"])
        self.assertIsNone(li["discount"])

    def test_mixed_currency_total_not_aggregated(self):
        recs = [actualize.normalize({"resourceName": "a", "payGPrice": 10, "quantity": 1,
                                     "costInBillingCurrency": 6, "billingCurrency": "USD"}),
                actualize.normalize({"resourceName": "b", "payGPrice": 10, "quantity": 1,
                                     "costInBillingCurrency": 6, "billingCurrency": "EUR"})]
        cur = actualize.detect_currency(recs, None)
        self.assertTrue(cur.startswith("MIXED"))
        t = json.loads(actualize.render_report(
            actualize.aggregate(recs, "resource"), cur, "json"))["totals"]
        self.assertIsNone(t["retail"])   # summing across currencies is meaningless
        self.assertIsNone(t["actual"])
        self.assertIsNone(t["savings"])
        self.assertFalse(t["retailComplete"])

    def test_focus_matches_modern(self):
        t = _totals("focus.csv")
        self.assertAlmostEqual(t["retail"], 133.32, places=2)
        self.assertAlmostEqual(t["actual"], 103.35, places=2)

    def test_legacy_retail_unknown(self):
        t = _totals("legacy-usage.json")
        self.assertIsNone(t["retail"])  # no payGPrice -> retail UNKNOWN, never invented
        self.assertIsNotNone(t["actual"])

    def test_query_retail_unknown(self):
        t = _totals("costmgmt-query.json")
        self.assertIsNone(t["retail"])
        self.assertAlmostEqual(t["actual"], 103.35, places=2)


class NeverGuessRules(unittest.TestCase):
    def test_zeroed_retail_becomes_unknown(self):
        # payGPrice 0 with positive actual must NOT be reported as a real $0 list price.
        rec = actualize.normalize({"payGPrice": 0, "quantity": 10,
                                 "costInBillingCurrency": 5, "meterId": "abc"})
        self.assertIsNone(rec["retailCost"])
        self.assertEqual(rec["actualCost"], 5)
        self.assertIn("zeroed", (rec["basisRetail"] or ""))

    def test_enrich_only_fills_missing(self):
        recs = [actualize.normalize({"payGPrice": 0, "quantity": 10,
                                   "costInBillingCurrency": 5, "meterId": "M1"})]
        n = actualize.enrich_retail(recs, {"m1": 0.4})
        self.assertEqual(n, 1)
        self.assertAlmostEqual(recs[0]["retailCost"], 4.0, places=6)
        self.assertIn("RetailPricesAPI", recs[0]["basisRetail"])

    def test_enrich_never_overwrites_real_retail(self):
        recs = [actualize.normalize({"payGPrice": 2, "quantity": 10,
                                   "costInBillingCurrency": 5, "meterId": "M1"})]
        before = recs[0]["retailCost"]  # 20.0 from payGPrice*quantity
        actualize.enrich_retail(recs, {"m1": 99.0})
        self.assertEqual(recs[0]["retailCost"], before)

    def test_ambiguous_meter_left_unknown(self):
        # A map row with a blank price (ambiguous) must not enrich.
        p = os.path.join(tempfile.gettempdir(), "gaac_test_map.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("meterId,retailPrice,status\nM1,,ambiguous\n")
        try:
            rmap = actualize.load_retail_map(p)
        finally:
            os.remove(p)
        self.assertNotIn("m1", rmap)  # blank price -> not in map -> stays UNKNOWN

    def test_raw_api_json_ambiguous_meter_not_loaded(self):
        # A raw Retail Prices API list where one meterId has several distinct
        # base-tier Consumption prices must NOT silently adopt the first one.
        p = os.path.join(tempfile.gettempdir(), "gaac_test_raw.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump([
                {"meterId": "AMB", "retailPrice": 5.0, "type": "Consumption", "tierMinimumUnits": 0},
                {"meterId": "AMB", "retailPrice": 40.0, "type": "Consumption", "tierMinimumUnits": 0},
                {"meterId": "OK", "retailPrice": 0.15, "type": "Consumption", "tierMinimumUnits": 0},
                {"meterId": "OK", "retailPrice": 0.15, "type": "Consumption", "tierMinimumUnits": 0},
            ], f)
        try:
            rmap = actualize.load_retail_map(p)
        finally:
            os.remove(p)
        self.assertNotIn("amb", rmap)          # ambiguous -> left UNKNOWN
        self.assertAlmostEqual(rmap["ok"], 0.15, places=6)  # single distinct price -> ok

    def test_enrich_skips_mismatched_currency(self):
        # A USD retail map must not enrich a EUR-billed row (currency mixing).
        eur = actualize.normalize({"payGPrice": 0, "quantity": 10, "meterId": "M1",
                                   "costInBillingCurrency": 5, "billingCurrency": "EUR"})
        usd = actualize.normalize({"payGPrice": 0, "quantity": 10, "meterId": "M1",
                                   "costInBillingCurrency": 5, "billingCurrency": "USD"})
        n = actualize.enrich_retail([eur, usd], {"m1": 0.4}, map_currency="USD")
        self.assertEqual(n, 1)                 # only the USD row enriched
        self.assertIsNone(eur["retailCost"])   # EUR row left UNKNOWN
        self.assertAlmostEqual(usd["retailCost"], 4.0, places=6)


class FetchRetailResolve(unittest.TestCase):
    @staticmethod
    def rec(price, sku, tmin=0, typ="Consumption", uom="1 Hour"):
        return {"retailPrice": price, "skuName": sku, "tierMinimumUnits": tmin,
                "type": typ, "unitOfMeasure": uom}

    def test_single_price_ok(self):
        price, uom, status, skus = fetch_retail.resolve([self.rec(0.15, "1 vCore")])
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(price, 0.15, places=6)

    def test_multiple_prices_ambiguous(self):
        price, uom, status, skus = fetch_retail.resolve(
            [self.rec(0.15, "1 vCore"), self.rec(4.87, "32 vCore")])
        self.assertEqual(status, "ambiguous")
        self.assertIsNone(price)  # refuses to guess

    def test_reservation_only_notfound(self):
        price, uom, status, skus = fetch_retail.resolve(
            [self.rec(867.0, "vCore", typ="Reservation")])
        self.assertEqual(status, "notfound")

    def test_same_price_multiple_skus_ok(self):
        # "vCore" and "1 vCore" both 0.15 -> single distinct price -> ok.
        price, uom, status, skus = fetch_retail.resolve(
            [self.rec(0.15, "vCore"), self.rec(0.15, "1 vCore")])
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(price, 0.15, places=6)


class DeltaMath(unittest.TestCase):
    def setUp(self):
        self.before = _recs("delta-before.json")
        self.after = _recs("delta-after.json")

    def test_infer_days(self):
        self.assertEqual(actualize.infer_days(self.before), 3)
        self.assertEqual(actualize.infer_days(self.after), 3)

    def test_two_period(self):
        b = actualize.resource_actuals(self.before, "resource")
        a = actualize.resource_actuals(self.after, "resource")
        rows, norm, single = actualize.compute_delta(b, a, 3, 3, None, False)
        self.assertFalse(single)
        self.assertTrue(norm)
        byitem = {r["item"]: r for r in rows}
        self.assertEqual(byitem["vm1"]["status"], "REMOVED")
        self.assertAlmostEqual(byitem["vm1"]["deltaRaw"], -60.0, places=2)
        self.assertAlmostEqual(byitem["vm1"]["deltaMonth"],
                               -60.0 / 3 * actualize.DAYS_PER_MONTH, places=2)
        self.assertEqual(byitem["stor1"]["status"], "CHANGED")
        self.assertAlmostEqual(byitem["stor1"]["deltaRaw"], -18.0, places=2)
        self.assertEqual(byitem["kv1"]["status"], "UNCHANGED")
        self.assertEqual(byitem["cache1"]["status"], "ADDED")
        self.assertAlmostEqual(byitem["cache1"]["deltaRaw"], 15.0, places=2)

    def test_run_rate(self):
        b = actualize.resource_actuals(self.before, "resource")
        rows, norm, single = actualize.compute_delta(b, None, 3, None, None, False)
        self.assertTrue(single)
        total = sum(r["deltaMonth"] for r in rows if r["deltaMonth"] is not None)
        self.assertAlmostEqual(total, -96.0 / 3 * actualize.DAYS_PER_MONTH, places=2)

    def test_decreases_only_excludes_added_and_unchanged(self):
        b = actualize.resource_actuals(self.before, "resource")
        a = actualize.resource_actuals(self.after, "resource")
        rows, _, _ = actualize.compute_delta(b, a, 3, 3, None, True)
        self.assertEqual({r["item"] for r in rows}, {"vm1", "stor1"})

    def test_unequal_windows_same_runrate_is_unchanged(self):
        # 30-day $150 vs 15-day $75 is the SAME monthly run-rate. The normalized
        # delta must be ~0 (UNCHANGED) and excluded from --decreases-only, even
        # though the raw window totals differ (150 -> 75).
        before = {"r1": {"label": "r1", "actual": 150.0}}
        after = {"r1": {"label": "r1", "actual": 75.0}}
        rows, norm, _ = actualize.compute_delta(before, after, 30, 15, None, False)
        self.assertTrue(norm)
        r = rows[0]
        self.assertEqual(r["status"], "UNCHANGED")
        self.assertAlmostEqual(r["deltaMonth"], 0.0, places=6)
        self.assertAlmostEqual(r["pct"], 0.0, places=6)
        drows, _, _ = actualize.compute_delta(before, after, 30, 15, None, True)
        self.assertEqual(drows, [])  # not a real reduction in run-rate terms

    def test_resource_filter_not_found(self):
        b = actualize.resource_actuals(self.before, "resource")
        a = actualize.resource_actuals(self.after, "resource")
        allg = dict(b); allg.update(a)
        matched, unmatched = actualize._match_keys(["vm1", "nonexistent-thing"], allg)
        self.assertIn("nonexistent-thing", unmatched)
        self.assertTrue(any("vm1" in str(k) for k in matched))

    def test_resource_list_plain_keeps_first_line(self):
        # A plain one-per-line list must NOT drop the first line as a header.
        self.assertEqual(actualize.read_resource_list("vm1\nstor1"), ["vm1", "stor1"])

    def test_resource_list_table_skips_header(self):
        # A table with an id/resource header column skips the header row.
        got = actualize.read_resource_list("| ResourceId |\n|---|\n| vm1 |\n| stor1 |")
        self.assertEqual(got, ["vm1", "stor1"])

    def test_resource_list_headerless_csv_keeps_first(self):
        got = actualize.read_resource_list("vm1,note-a\nstor1,note-b")
        self.assertEqual(got, ["vm1", "stor1"])


class SavingsMode(unittest.TestCase):
    def test_unmatched_excluded(self):
        recs = _recs("modern-usage.json")
        idx = actualize.build_ratio_index(recs)
        ratio, basis = actualize.match_ratio("some-unmanaged-thing", idx)
        self.assertIsNone(ratio)  # no backing data -> UNMATCHED, never guessed

    def _idx(self, pairs):
        # pairs: list of (resourceName, retail_unit, actual) at quantity 1
        recs = [actualize.normalize({"resourceId": "/s/" + n, "resourceName": n,
                                     "payGPrice": r, "quantity": 1,
                                     "costInBillingCurrency": a}) for n, r, a in pairs]
        return actualize.build_ratio_index(recs)

    def test_substring_ambiguous_different_ratios_unmatched(self):
        # "db" matches db-prod (60%) and db-test (30%): different discounts -> refuse.
        idx = self._idx([("db-prod", 100, 60), ("db-test", 100, 30)])
        ratio, basis = actualize.match_ratio("db", idx)
        self.assertIsNone(ratio)
        self.assertIn("ambiguous", basis)

    def test_substring_same_ratio_ok(self):
        # Same ratio under multiple matches is safe -- result is identical either way.
        idx = self._idx([("db-prod", 100, 60), ("db-test", 100, 60)])
        ratio, basis = actualize.match_ratio("db", idx)
        self.assertAlmostEqual(ratio, 0.6, places=6)

    def test_exact_wins_over_substring(self):
        # "vm1" must resolve to vm1, never drag in vm10 via substring.
        idx = self._idx([("vm1", 100, 60), ("vm10", 100, 30)])
        ratio, basis = actualize.match_ratio("vm1", idx)
        self.assertEqual(basis, "exact")
        self.assertAlmostEqual(ratio, 0.6, places=6)


class MatchKeysExactFirst(unittest.TestCase):
    def test_delta_resource_filter_exact_first(self):
        recs = [actualize.normalize({"resourceId": "/s/vm1", "resourceName": "vm1",
                                     "costInBillingCurrency": 10}),
                actualize.normalize({"resourceId": "/s/vm10", "resourceName": "vm10",
                                     "costInBillingCurrency": 99})]
        groups = actualize.resource_actuals(recs, "resource")
        matched, unmatched = actualize._match_keys(["vm1"], groups)
        matched_names = {groups[k]["resourceName"] for k in matched}
        self.assertEqual(matched_names, {"vm1"})  # vm10 excluded
        self.assertEqual(unmatched, [])


class HardeningRegressions(unittest.TestCase):
    """Locks the six never-guess gaps found in the second review pass."""

    # -- A: currency must not be mixed WITHIN a single group ------------------
    def test_report_group_mixed_currency_not_aggregated(self):
        recs = [actualize.normalize({"resourceName": "x", "serviceName": "X",
                                     "payGPrice": 10, "quantity": 1,
                                     "costInBillingCurrency": 6, "billingCurrency": "USD"}),
                actualize.normalize({"resourceName": "a", "serviceName": "Shared",
                                     "payGPrice": 10, "quantity": 1,
                                     "costInBillingCurrency": 5, "billingCurrency": "USD"}),
                actualize.normalize({"resourceName": "b", "serviceName": "Shared",
                                     "payGPrice": 10, "quantity": 1,
                                     "costInBillingCurrency": 5, "billingCurrency": "EUR"})]
        cur = actualize.detect_currency(recs, None)
        li = {i["item"]: i for i in json.loads(
            actualize.render_report(actualize.aggregate(recs, "service"), cur, "json"))["lineItems"]}
        # The single-currency group still gets a real discount...
        self.assertIsNotNone(li["X"]["discount"])
        # ...but the group that mixes USD+EUR is UNKNOWN, not a blended discount.
        self.assertIsNone(li["Shared"]["retail"])
        self.assertIsNone(li["Shared"]["actual"])
        self.assertIsNone(li["Shared"]["savings"])
        self.assertIsNone(li["Shared"]["discount"])

    def test_delta_group_mixed_currency_unknown(self):
        b = [actualize.normalize({"serviceName": "S", "resourceName": "a", "date": "2026-06-01",
                                  "costInBillingCurrency": 100, "billingCurrency": "USD"}),
             actualize.normalize({"serviceName": "S", "resourceName": "b", "date": "2026-06-01",
                                  "costInBillingCurrency": 100, "billingCurrency": "EUR"})]
        a = [actualize.normalize({"serviceName": "S", "resourceName": "a", "date": "2026-06-01",
                                  "costInBillingCurrency": 10, "billingCurrency": "USD"}),
             actualize.normalize({"serviceName": "S", "resourceName": "b", "date": "2026-06-01",
                                  "costInBillingCurrency": 10, "billingCurrency": "EUR"})]
        rows, _, _ = actualize.compute_delta(
            actualize.resource_actuals(b, "service"),
            actualize.resource_actuals(a, "service"), 1, 1, None, False)
        r = rows[0]
        self.assertTrue(r["status"].startswith("UNKNOWN"))
        self.assertIsNone(r["deltaRaw"])
        self.assertIsNone(r["pct"])

    # -- B: savings ratio needs COMPLETE retail+actual coverage --------------
    def test_savings_incomplete_coverage_unmatched(self):
        # "db" has retail on 1 of 2 rows but actual on both: ratio would mix row
        # sets, so it must be UNMATCHED rather than a fabricated 1.0 (0% discount).
        recs = [actualize.normalize({"resourceName": "db", "payGPrice": 100, "quantity": 1,
                                     "costInBillingCurrency": 60}),
                actualize.normalize({"resourceName": "db", "payGPrice": 0, "quantity": 1,
                                     "costInBillingCurrency": 40, "meterId": "M2"})]
        ratio, basis = actualize.match_ratio("db", actualize.build_ratio_index(recs))
        self.assertIsNone(ratio)
        self.assertIn("incomplete", basis)

    # -- C: substring must respect token boundaries --------------------------
    def test_boundary_contains(self):
        self.assertFalse(actualize._boundary_contains("vm1", "vm10"))
        self.assertTrue(actualize._boundary_contains("vm-app01", "/subscriptions/x/vm-app01"))
        self.assertTrue(actualize._boundary_contains("db", "db-prod"))

    def test_match_ratio_prefix_collision_unmatched(self):
        # vm1 is absent; it must NOT borrow vm10's ratio via substring.
        idx = actualize.build_ratio_index([actualize.normalize(
            {"resourceName": "vm10", "payGPrice": 100, "quantity": 1, "costInBillingCurrency": 30})])
        ratio, _ = actualize.match_ratio("vm1", idx)
        self.assertIsNone(ratio)

    def test_match_keys_prefix_collision_unmatched(self):
        recs = [actualize.normalize({"resourceId": "/s/vm10", "resourceName": "vm10",
                                     "costInBillingCurrency": 99})]
        matched, unmatched = actualize._match_keys(["vm1"], actualize.resource_actuals(recs, "resource"))
        self.assertEqual(matched, set())
        self.assertEqual(unmatched, ["vm1"])

    # -- D: unknown actual cost must not be treated as $0 --------------------
    def test_delta_unknown_actual_not_zero(self):
        before = [actualize.normalize({"resourceName": "r1", "date": "2026-01-01",
                                       "billingCurrency": "USD"})]  # no cost -> actual unknown
        after = [actualize.normalize({"resourceName": "r1", "date": "2026-01-02",
                                      "costInBillingCurrency": 10, "billingCurrency": "USD"})]
        rows, _, _ = actualize.compute_delta(
            actualize.resource_actuals(before, "resource"),
            actualize.resource_actuals(after, "resource"), 1, 1, None, False)
        r = rows[0]
        self.assertTrue(r["status"].startswith("UNKNOWN"))
        self.assertIsNone(r["before"])       # not fabricated as 0
        self.assertIsNone(r["deltaRaw"])

    # -- E: paginated raw retail JSON is incomplete -> adopt nothing ---------
    def test_paginated_retail_json_not_loaded(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"Items": [{"meterId": "M1", "retailPrice": 1.0,
                                  "type": "Consumption", "tierMinimumUnits": 0}],
                       "NextPageLink": "https://example/next"}, f)
            path = f.name
        try:
            self.assertEqual(actualize.load_retail_map(path), {})
        finally:
            os.unlink(path)

    # -- F: no-run-rate note must not claim "No usage dates found" -----------
    def test_delta_no_runrate_note_wording(self):
        before = {"r1": {"label": "r1", "actual": 100.0}}
        after = {"r1": {"label": "r1", "actual": 80.0}}
        rows, norm, single = actualize.compute_delta(before, after, None, None, None, False)
        self.assertFalse(norm)
        out = actualize.render_delta(rows, [], "USD", "md", norm, single, None, None)
        self.assertNotIn("No usage dates found", out)
        self.assertIn("Window length is unknown", out)


class ThirdReviewRegressions(unittest.TestCase):
    """Locks the never-guess gaps found in the third review pass."""

    # -- 1: a substring label that also hits an incomplete sibling is ambiguous
    def test_savings_substring_incomplete_sibling_unmatched(self):
        # "vm" matches vm-1 (complete, 50%) AND vm-2 (no retail): the label could
        # mean either resource, so refuse rather than use vm-1's ratio.
        recs = [actualize.normalize({"resourceName": "vm-1", "payGPrice": 100,
                                     "quantity": 1, "costInBillingCurrency": 50}),
                actualize.normalize({"resourceName": "vm-2",
                                     "costInBillingCurrency": 20})]  # no retail
        ratio, basis = actualize.match_ratio("vm", actualize.build_ratio_index(recs))
        self.assertIsNone(ratio)
        self.assertIn("ambiguous", basis)

    # -- 2: a UNKNOWN delta row must not leak its known side into the TOTAL ----
    def test_delta_unknown_row_excluded_from_total(self):
        before = [actualize.normalize({"resourceName": "vm1", "date": "2026-01-01",
                                       "billingCurrency": "USD"}),  # actual unknown
                  actualize.normalize({"resourceName": "vm1", "date": "2026-01-02",
                                       "costInBillingCurrency": 10, "billingCurrency": "USD"}),
                  actualize.normalize({"resourceName": "vm2", "date": "2026-01-01",
                                       "costInBillingCurrency": 30, "billingCurrency": "USD"})]
        after = [actualize.normalize({"resourceName": "vm1", "date": "2026-02-01",
                                      "costInBillingCurrency": 5, "billingCurrency": "USD"}),
                 actualize.normalize({"resourceName": "vm2", "date": "2026-02-01",
                                      "costInBillingCurrency": 20, "billingCurrency": "USD"})]
        rows, norm, single = actualize.compute_delta(
            actualize.resource_actuals(before, "resource"),
            actualize.resource_actuals(after, "resource"), 1, 1, None, False)
        data = json.loads(actualize.render_delta(rows, [], "USD", "json", norm, single, 1, 1))
        vm1 = next(r for r in rows if r["item"] == "vm1")
        self.assertTrue(vm1["status"].startswith("UNKNOWN"))
        # TOTAL reflects only the fully-known vm2 (30 -> 20); vm1's stray after=5 is out.
        self.assertEqual(data["totals"]["before"], 30.0)
        self.assertEqual(data["totals"]["after"], 20.0)
        self.assertEqual(data["totals"]["deltaActual"], -10.0)

    # -- 3: savings ratio index must not blend currencies ---------------------
    def test_savings_ratio_mixed_currency_refused(self):
        recs = [actualize.normalize({"serviceName": "Compute", "resourceName": "usd-vm",
                                     "payGPrice": 100, "quantity": 1,
                                     "costInBillingCurrency": 50, "billingCurrency": "USD"}),
                actualize.normalize({"serviceName": "Compute", "resourceName": "eur-vm",
                                     "payGPrice": 100, "quantity": 1,
                                     "costInBillingCurrency": 90, "billingCurrency": "EUR"})]
        ratio, basis = actualize.match_ratio("Compute", actualize.build_ratio_index(recs))
        self.assertIsNone(ratio)
        self.assertIn("currencies", basis)

    # -- 4: --currency override must not hide genuinely mixed source data -----
    def test_currency_override_does_not_hide_mixed_source(self):
        recs = [actualize.normalize({"resourceName": "usd", "payGPrice": 10, "quantity": 1,
                                     "costInBillingCurrency": 8, "billingCurrency": "USD"}),
                actualize.normalize({"resourceName": "eur", "payGPrice": 20, "quantity": 1,
                                     "costInBillingCurrency": 15, "billingCurrency": "EUR"})]
        self.assertTrue(actualize.detect_currency(recs, "USD").startswith("MIXED"))

    # -- F: single-period no-date note must not mention an 'after' period -----
    def test_delta_single_no_date_note_wording(self):
        before = {"r1": {"label": "r1", "actual": 100.0}}
        rows, norm, single = actualize.compute_delta(before, None, None, None, None, False)
        self.assertTrue(single)
        self.assertFalse(norm)
        out = actualize.render_delta(rows, [], "USD", "md", norm, single, None, None)
        self.assertNotIn("after=", out)
        self.assertNotIn("--after-days", out)
        self.assertIn("Window length (days) is unknown", out)

    # -- deep-review Obs1: meters must not blend cross-currency "actual so far"
    def test_meters_mixed_currency_not_blended(self):
        data = json.dumps([
            {"meterId": "M-X", "meterName": "m", "costInBillingCurrency": 100, "billingCurrency": "USD"},
            {"meterId": "M-X", "meterName": "m", "costInBillingCurrency": 100, "billingCurrency": "EUR"}])
        script = os.path.join(ROOT, "scripts", "actualize.py")
        out = subprocess.run([sys.executable, script, "meters", "--input", "-"],
                             input=data, capture_output=True, text=True).stdout
        self.assertIn("UNKNOWN (mixed)", out)
        self.assertNotIn("200.00", out)  # 100 USD + 100 EUR must not be summed

    # -- deep-review Obs2: all-UNKNOWN delta TOTAL must read UNKNOWN, not 0.00 --
    def test_delta_all_unknown_total_is_unknown(self):
        before = [actualize.normalize({"resourceName": "vm1", "date": "2026-01-01",
                                       "billingCurrency": "USD"}),
                  actualize.normalize({"resourceName": "vm1", "date": "2026-01-02",
                                       "costInBillingCurrency": 10, "billingCurrency": "USD"})]
        after = [actualize.normalize({"resourceName": "vm1", "date": "2026-02-01",
                                      "billingCurrency": "USD"}),
                 actualize.normalize({"resourceName": "vm1", "date": "2026-02-02",
                                      "costInBillingCurrency": 5, "billingCurrency": "USD"})]
        rows, norm, single = actualize.compute_delta(
            actualize.resource_actuals(before, "resource"),
            actualize.resource_actuals(after, "resource"), 2, 2, None, False)
        data = json.loads(actualize.render_delta(rows, [], "USD", "json", norm, single, 2, 2))
        self.assertTrue(all(r["status"].startswith("UNKNOWN") for r in rows))
        self.assertIsNone(data["totals"]["before"])
        self.assertIsNone(data["totals"]["after"])
        self.assertIsNone(data["totals"]["deltaActual"])


class DefaultCurrency(unittest.TestCase):
    """USD is the labeling default only when the data has no currency and none is
    given; a detected currency always wins and mixed data is never coerced."""

    def test_default_is_usd(self):
        self.assertEqual(actualize.DEFAULT_CURRENCY, "USD")

    def test_default_used_when_absent(self):
        recs = [actualize.normalize({"resourceName": "vm1", "payGPrice": 10,
                                     "quantity": 1, "costInBillingCurrency": 8})]  # no currency
        self.assertEqual(actualize.detect_currency(recs, None), actualize.DEFAULT_CURRENCY)

    def test_detected_currency_wins_over_default(self):
        recs = [actualize.normalize({"resourceName": "vm1", "payGPrice": 10, "quantity": 1,
                                     "costInBillingCurrency": 8, "billingCurrency": "EUR"})]
        self.assertEqual(actualize.detect_currency(recs, None), "EUR")

    def test_mixed_not_coerced_to_default(self):
        recs = [actualize.normalize({"resourceName": "a", "costInBillingCurrency": 8,
                                     "billingCurrency": "USD"}),
                actualize.normalize({"resourceName": "b", "costInBillingCurrency": 8,
                                     "billingCurrency": "EUR"})]
        self.assertTrue(actualize.detect_currency(recs, actualize.DEFAULT_CURRENCY).startswith("MIXED"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
