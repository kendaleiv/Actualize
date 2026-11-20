"""Offline behavioral grader controls; no agent, model, network or containers."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
module = importlib.util.spec_from_file_location("check_actualize", ROOT / "evals/graders/check_actualize.py")
grader = importlib.util.module_from_spec(module)
module.loader.exec_module(grader)


class BehavioralGrader(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.work = base / "workspace"
        self.trusted = base / "trusted"
        shutil.copytree(ROOT / "evals/expected", self.trusted)
        shutil.copytree(ROOT / "samples", self.trusted / "inputs/samples")
        shutil.copytree(ROOT / "evals/fixtures", self.trusted / "inputs/fixtures")
        self.work.mkdir()
        integrity = {}
        for name in ("actualize.py", "fetch_retail.py"):
            dest = self.work / "scripts" / name
            dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(ROOT / "scripts" / name, dest)
            integrity[f"scripts/{name}"] = hashlib.sha256(dest.read_bytes()).hexdigest()
        (self.trusted / "integrity.json").write_text(json.dumps(integrity), encoding="utf-8")
        self.cases = json.loads((self.trusted / "cases.json").read_text(encoding="utf-8"))

    def payload(self, case="standard"):
        config = self.cases[case]
        command = ["python3", "scripts/actualize.py", config.get("command", "report")]
        for flag, name in config.get("inputs", {}).items():
            dest = self.work / "inputs" / Path(name).name
            dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(self.trusted / "inputs" / name, dest)
            if config.get("selection"):
                rows = json.loads(dest.read_text(encoding="utf-8"))
                dest.write_text(json.dumps(grader.selected_rows(rows, config["selection"])), encoding="utf-8")
            command.extend([flag, f"inputs/{dest.name}"])
        command.extend(config.get("flags", []))
        text = (self.trusted / config["table"]).read_text(encoding="utf-8") if "table" in config else ""
        if case == "enrichment":
            text += "\nThe SQL meter is ambiguous."
        if case == "default-currency":
            text += "\nAmounts assumed USD."
        events = [
            {"type": "tool_call", "data": {"toolName": "bash", "toolCallId": "calc",
                                         "arguments": {"command": " ".join(command)}}},
            {"type": "tool_result", "data": {"toolName": "bash", "toolCallId": "calc", "success": True,
                                           "result": {"stdout": text, "exitCode": 0}}},
            {"type": "assistant_message", "data": {"content": text}},
        ]
        return {"config": {"env": {"ACTUALIZE_CASE": case}},
                "trajectory": {"events": events, "output": text, "workDir": str(self.work),
                               "endReason": "completed", "metrics": {"tokenUsage": {"totalTokens": 1000}}}}

    def test_reviewed_expectations_match_fixture_calculator(self):
        for name, config in self.cases.items():
            if "table" not in config:
                continue
            with self.subTest(case=name):
                payload = self.payload(name)
                command = [sys.executable, str(ROOT / "scripts/actualize.py"), config["command"]]
                for flag, source in config["inputs"].items():
                    path = (self.work / "inputs" / Path(source).name if config.get("selection")
                            else self.trusted / "inputs" / source)
                    command.extend([flag, str(path)])
                command.extend(config.get("flags", []))
                completed = subprocess.run(command, capture_output=True, text=True, check=True)
                self.assertEqual(grader.tables(completed.stdout), grader.tables(payload["trajectory"]["output"]))
                self.assertTrue(grader.grade(payload, self.trusted)["passed"])

    def test_scoped_inputs_reject_wrong_rows_costs_and_missing_records(self):
        for case in ("identity", "sql-resize", "sql-removal", "cosmos-region", "deployment-result"):
            for mutation in ("whole-export", "wrong-cost", "missing-row", "duplicate-row"):
                with self.subTest(case=case, mutation=mutation):
                    payload = self.payload(case)
                    self.assertTrue(grader.grade(payload, self.trusted)["passed"])
                    source = self.cases[case]["inputs"]["--before"]
                    path = self.work / "inputs" / Path(source).name
                    rows = json.loads(path.read_text(encoding="utf-8"))
                    if mutation == "whole-export":
                        rows = json.loads((self.trusted / "inputs" / source).read_text(encoding="utf-8"))
                    elif mutation == "wrong-cost":
                        rows[0]["costInBillingCurrency"] += 1
                    elif mutation == "missing-row":
                        rows.pop()
                    else:
                        rows.append(rows[0])
                    path.write_text(json.dumps(rows), encoding="utf-8")
                    self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_scoped_inputs_accept_reordering_and_calculator_resource_lists(self):
        for case in ("identity", "sql-resize", "sql-removal", "deployment-result"):
            payload = self.payload(case)
            for flag, source in self.cases[case]["inputs"].items():
                shutil.copyfile(self.trusted / "inputs" / source, self.work / "inputs" / Path(source).name)
            ids = self.cases[case]["selection"]["resourceId"]
            resources = self.work / "resources.txt"
            for text in ("\n".join(ids), "\n".join(ids).lower(),
                         "| ResourceId |\n|---|\n" + "\n".join(f"| {rid} |" for rid in ids)):
                with self.subTest(case=case, resources=text):
                    resources.write_text(text, encoding="utf-8")
                    payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = (
                        payload["trajectory"]["events"][0]["data"]["arguments"]["command"].split(" --resources")[0]
                        + " --resources resources.txt")
                    self.assertTrue(grader.grade(payload, self.trusted)["passed"])
            resources.write_text("\n".join(ids + ["app-production"]), encoding="utf-8")
            self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        payload = self.payload("cosmos-region")
        path = self.work / "inputs/workflow-before.json"
        rows = list(reversed(json.loads(path.read_text(encoding="utf-8"))))
        rows[0]["costInBillingCurrency"] = float(rows[0]["costInBillingCurrency"])
        path.write_text(json.dumps(rows), encoding="utf-8")
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])

    def test_recovery_commands_must_be_in_executable_guidance(self):
        examples = {
            "graph-recovery": "az account set --subscription 0000\naz graph query -q \"resourcechanges | where properties.targetResourceId =~ '$DBID'\" --query data -o json",
            "deployment-query": (
                "BEFORE_FROM=2026-04-01; BEFORE_TO=2026-04-02\nAFTER_FROM=2026-06-01; AFTER_TO=2026-06-02\n"
                "az rest --method post --url https://management.azure.com/subscriptions/0000/providers/Microsoft.CostManagement/query "
                "--body '{\"type\":\"AmortizedCost\",\"timePeriod\":{\"from\":\"2026-04-01\",\"to\":\"2026-04-02\"},"
                "\"dataset\":{\"granularity\":\"Daily\",\"filter\":{\"Dimensions\":{\"Name\":\"ResourceId\","
                "\"Operator\":\"In\",\"Values\":[\"/subscriptions/0000/resourceGroups/demo/providers/Microsoft.Sql/servers/sql-east/databases/app-db\","
                "\"/subscriptions/0000/resourceGroups/demo/providers/Microsoft.Sql/servers/sql-west/databases/app-db\"]}}}}'"),
            "async-recovery": "az rest --body '{\"type\":\"AmortizedCost\"}'",
        }
        for case, command in examples.items():
            with self.subTest(case=case):
                payload = self.payload(case)
                payload["trajectory"]["output"] = "The Location header is absent.\n```bash\n" + command + "\n```"
                self.assertTrue(grader.grade(payload, self.trusted)["passed"])
                payload["trajectory"]["output"] = payload["trajectory"]["output"].replace("```bash", "").replace("```", "")
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        payload = self.payload("graph-recovery")
        payload["trajectory"]["output"] = "```bash\n" + examples["graph-recovery"].replace(
            "properties.targetResourceId", "resourceId") + "\n```"
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        payload = self.payload("async-recovery")
        payload["trajectory"]["output"] = (
            "Location header absent.\n```bash\n" + examples["async-recovery"]
            + "\nuntil RESP=$(curl \"$LOC\"); do sleep 10; done\n```")
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_baseline_only_cannot_establish_realized_savings(self):
        payload = self.payload("realized-removal")
        payload["trajectory"]["output"] = (
            "This is a run-rate projection, not measured savings. "
            "We need post-deletion cost data to establish realized savings.")
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["output"] += " These are guaranteed cash savings."
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_pasted_input_can_be_verified_from_a_literal_temporary_heredoc(self):
        payload = self.payload("query")
        source = (self.trusted / "inputs/samples/costmgmt-query.json").read_text(encoding="utf-8")
        command = "cat > /tmp/pasted.json <<'JSON'\n" + source + "\nJSON\n"
        command += "python scripts/actualize.py report --input /tmp/pasted.json"
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = command
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = command.replace("<<'JSON'", "<< 'JSON'")
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        for bad in (command.replace("42.8544", "999.0"),
                    command.replace("python scripts/actualize.py", "echo python scripts/actualize.py"),
                    command.replace("\npython scripts", "\nfalse && python scripts")):
            with self.subTest(command=bad):
                payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = bad
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        for prepare in ("python - <<'PY'\nprint('prepared')\nPY\n",
                        "python -c \"print('prepared')\" && ",
                        "python scripts/actualize.py report --help && python -c \"print('prepared')\"\n"):
            payload = self.payload()
            payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = (
                prepare + payload["trajectory"]["events"][0]["data"]["arguments"]["command"])
            self.assertTrue(grader.grade(payload, self.trusted)["passed"])

    def test_post_calculator_commands_cannot_mask_failed_execution(self):
        for suffix in [
            "python -c \"print('fabricated table')\"",
            "python - <<'PY'\nprint('fabricated table')\nPY",
            "cat fabricated.md",
            "ls",
            "RESULT=0",
            "printf '%s\\n' data > inputs/modern-usage.json",
            "python scripts/actualize.py report --help",
        ]:
            with self.subTest(suffix=suffix):
                payload = self.payload()
                arguments = payload["trajectory"]["events"][0]["data"]["arguments"]
                arguments["command"] += " --invalid\n" + suffix
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_removal_prose_uses_the_matching_month_or_year_column(self):
        for text, passed in [
            ("Annual reduction: 4,017.75 USD/year.", True),
            ("Annual reduction: 334.81 USD/year.", False),
            ("Monthly reduction: 334.81 USD/month.", True),
            ("Monthly reduction: 4,017.75 USD/month.", False),
        ]:
            with self.subTest(text=text):
                payload = self.payload("cosmos-region")
                payload["trajectory"]["output"] += "\n" + text
                self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)

    def test_retained_calculator_output_can_be_teed_or_redirected_and_read(self):
        for suffix in (" | tee result.md", " > result.md && cat result.md", " > result.md\ncat result.md"):
            with self.subTest(suffix=suffix):
                payload = self.payload()
                command = payload["trajectory"]["events"][0]["data"]["arguments"]["command"]
                output = payload["trajectory"]["output"]
                saved = self.work / "result.md"
                saved.write_text(output, encoding="utf-8")
                payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = command + suffix
                self.assertTrue(grader.grade(payload, self.trusted)["passed"])
                saved.write_text(output.replace("103.35", "999.00"), encoding="utf-8")
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])
                saved.write_text(output, encoding="utf-8")
                payload["trajectory"]["events"][1]["data"]["result"]["stderr"] = "actualize.py: error: invalid flag"
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        for suffix in (" | tee -a result.md", " > result.md && cat other.md",
                       " | tee result.md\npython -c \"print('fabricated')\""):
            payload = self.payload()
            payload["trajectory"]["events"][0]["data"]["arguments"]["command"] += suffix
            self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_multiline_printf_and_directory_creation_can_prepare_a_resource_list(self):
        payload = self.payload("identity")
        rid = self.cases["identity"]["selection"]["resourceId"][0]
        command = payload["trajectory"]["events"][0]["data"]["arguments"]["command"]
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = (
            f"mkdir -p evidence && printf '%s\n' '{rid}' > evidence/resources.txt && "
            "python - <<'PY'\nprint('prepared')\nPY\n" + command + " --resources evidence/resources.txt")
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = (
            payload["trajectory"]["events"][0]["data"]["arguments"]["command"].replace("app-prod'", "app-production'"))
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_annualized_prose_and_actual_savings_suffix_keep_their_financial_field(self):
        for case, text, passed in [
            ("sql-resize", "Monthly actual change of -365.25 USD and an annualized run-rate change of -4,383.00 USD.", True),
            ("sql-resize", "Monthly actual change of -365.25 USD and an annualized run-rate change of -365.25 USD.", False),
            ("savings", "The proposed monthly retail cuts yield 51.35 USD actual monthly savings.", True),
            ("savings", "The proposed monthly retail cuts yield 80.62 USD actual monthly savings.", False),
        ]:
            with self.subTest(text=text):
                payload = self.payload(case)
                payload["trajectory"]["output"] += "\n" + text
                self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)

    def test_stopped_or_avoided_run_rate_can_describe_reduction_magnitudes(self):
        for text, passed in [
            ("Run-rate avoided if both resources are deleted:\n\nThe stoppable run-rate is 700.06 USD/month and 8,400.75 USD/year (reduction magnitudes).", True),
            ("Run-rate avoided if both resources are deleted:\n\nThis is measured actual cost data, converted into a projected run-rate. So the stoppable run-rate is 700.06 USD/month and 8,400.75 USD/year (reduction magnitudes).", True),
            ("Projected run-rate reduction (actual amortized baseline): deleting both scoped resources would stop an estimated $700.06/month and $8,400.75/year of actual run-rate cost.", True),
            ("Projected run-rate reduction (actual amortized baseline, offline): deleting both scoped resources would stop an estimated $700.06/month and $8,400.75/year of actual run-rate cost.", True),
            ("Monthly actual change is 700.06 USD/month.", False),
            ("Run-rate avoided if both resources are deleted:\n\nThe stoppable run-rate is 8,400.75 USD/month.", False),
        ]:
            with self.subTest(text=text):
                payload = self.payload("sql-removal")
                payload["trajectory"]["output"] += "\n" + text
                self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)

    def test_scoped_resource_file_can_be_captured_from_printf(self):
        payload = self.payload("identity")
        for source in self.cases["identity"]["inputs"].values():
            shutil.copyfile(self.trusted / "inputs" / source, self.work / "inputs" / Path(source).name)
        rid = self.cases["identity"]["selection"]["resourceId"][0]
        command = payload["trajectory"]["events"][0]["data"]["arguments"]["command"]
        command = f"RID='{rid}'; printf '%s\\n' \"$RID\" > /tmp/resources.txt && " + command
        command += " --resources /tmp/resources.txt"
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = command
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = command.replace("app-prod'", "app-production'")
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = (
            payload["trajectory"]["events"][0]["data"]["arguments"]["command"].split(" && ")[-1]
            .replace("--resources /tmp/resources.txt", f"--resources <(printf '%s\\n' '{rid}')"))
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])

    def test_literal_resource_patch_is_bound_to_the_calculation_time(self):
        payload = self.payload("identity")
        for source in self.cases["identity"]["inputs"].values():
            shutil.copyfile(self.trusted / "inputs" / source, self.work / "inputs" / Path(source).name)
        rid = self.cases["identity"]["selection"]["resourceId"][0]
        events = payload["trajectory"]["events"]
        events[0]["data"]["arguments"]["command"] += " --resources resources.txt"
        patch = [
            {"type": "tool_call", "data": {"toolName": "apply_patch", "toolCallId": "patch",
             "arguments": f"*** Begin Patch\n*** Add File: resources.txt\n+{rid}\n*** End Patch\n"}},
            {"type": "tool_result", "data": {"toolName": "apply_patch", "toolCallId": "patch",
             "success": True, "result": "Created"}},
        ]
        payload["trajectory"]["events"] = patch + events
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["events"][1]["data"]["success"] = False
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_annual_execution_can_follow_a_matching_monthly_preview(self):
        payload = self.payload("cosmos-region")
        command = payload["trajectory"]["events"][0]["data"]["arguments"]["command"]
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = command.replace(" --annual", "") + "\n" + command
        table = grader.tables(payload["trajectory"]["output"])[0]
        year = next(i for i, header in enumerate(table[0]) if "/yr" in header)
        monthly = "\n".join("| " + " | ".join(cell for i, cell in enumerate(row) if i != year) + " |"
                            for row in table)
        result = payload["trajectory"]["events"][1]["data"]["result"]
        original = result["stdout"]
        result["stdout"] = monthly + "\n\n" + original
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        result["stdout"] = monthly.replace("-334.81", "-999.00") + "\n\n" + original
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_selected_source_quotes_bind_the_date_scope_field_and_value(self):
        for quote, passed in [
            ("Selected source rows: 2026-04-01 USD 11.00, 2026-04-02 USD 11.00.", True),
            ("Selected source rows:\n- 2026-04-01 costInBillingCurrency: 11 USD.", True),
            ("Selected source rows: 2026-04-01 USD 22.00.", False),
            ("Selected source rows: 2026-04-01 USD 4.00.", False),
            ("Selected source rows: 2026-04-03 USD 11.00.", False),
            ("Selected source rows: 2026-04-01 EUR 11.00.", False),
            ("Selected source rows: 2026-04-01 retail 11 USD.", False),
        ]:
            with self.subTest(quote=quote):
                payload = self.payload("cosmos-region")
                payload["trajectory"]["output"] += "\n\n" + quote
                self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)
        for text, passed in [
            ("Source windows and values preserved from input data: before 2026-04-01 and 2026-04-02 at 12 + 12 USD; after 2026-06-01 and 2026-06-02 at 4 + 4 USD.", True),
            ("Source windows and values preserved from input data: before 2026-04-01 and 2026-04-02 at 12 + 24 USD.", False),
        ]:
            payload = self.payload("identity")
            payload["trajectory"]["output"] += "\n\n" + text
            self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)

    def test_removing_a_run_rate_is_a_positive_saving_not_a_positive_change(self):
        for text, passed in [
            ("Deleting both scoped resources would remove a monthly actual run-rate of 700.06 USD and an annual actual run-rate of 8,400.75 USD.", True),
            ("Projected run-rate reduction (not measured realized savings): deleting both scoped resources would remove a monthly actual run-rate of 700.06 USD and an annual actual run-rate of 8,400.75 USD.", True),
            ("Deleting both scoped resources would remove a monthly actual run-rate of 8,400.75 USD.", False),
            ("The monthly actual change is +700.06 USD.", False),
        ]:
            payload = self.payload("sql-removal")
            payload["trajectory"]["output"] += "\n" + text
            self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)

    def test_corrupted_answers_fail(self):
        mutations = [
            lambda t: t["output"].replace("42.85", "44.85"),
            lambda t: t["output"].replace("USD", "EUR"),
            lambda t: "\n".join(line for line in t["output"].splitlines() if "vm-app01" not in line),
            lambda t: t["output"] + "\nAdditional savings: $999.00 per year.",
            lambda t: t["output"] + "\nAdditional savings: $9,999.99.",
            lambda t: t["output"].replace("| 40.0% |", "| 0.4% |"),
            lambda t: t["output"] + "\n| Extra total | 1.00 |",
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = self.payload()
                payload["trajectory"]["output"] = mutate(payload["trajectory"])
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_presentation_failure_does_not_imply_failed_calculator_execution(self):
        payload = self.payload()
        payload["trajectory"]["output"] = "\n".join(
            "|".join(line.split("|")[:-2]) + "|" if line.startswith("|") else line
            for line in payload["trajectory"]["output"].splitlines())
        result = grader.grade(payload, self.trusted)
        failed = [check for check in result["metadata"]["checks"] if not check["passed"]]
        self.assertFalse(result["passed"])
        self.assertEqual([(check["category"], check["code"]) for check in failed],
                         [("presentation", "exact_table")])
        self.assertEqual(result["metadata"]["financial_comparison"]["status"], "pass")
        self.assertNotIn("No verified successful calculator", result["evidence"])

    def test_changed_financial_cells_are_separate_from_execution_and_presentation(self):
        for case, before, after in [
            ("standard", "42.85", "44.85"),
            ("standard", "40.0%", "0.4%"),
            ("standard", "USD", "EUR"),
            ("annual", "-7,670.25", "+7,670.25"),
            ("missing-actuals", "UNKNOWN", "0.00"),
        ]:
            with self.subTest(case=case, change=after):
                payload = self.payload(case)
                payload["trajectory"]["output"] = payload["trajectory"]["output"].replace(before, after)
                result = grader.grade(payload, self.trusted)
                failed = [check for check in result["metadata"]["checks"] if not check["passed"]]
                self.assertFalse(result["passed"])
                self.assertIn("correctness", {check["category"] for check in failed})
                self.assertNotIn("workflow", {check["category"] for check in failed})
                self.assertEqual(result["metadata"]["financial_comparison"]["status"], "fail")

    def test_missing_financial_content_is_not_reported_as_verified(self):
        for output in ["Amounts are explained in prose.", "| Resource | Actual |\n|---|---|\n| vm-app01 | 42.85 |"]:
            with self.subTest(output=output):
                payload = self.payload()
                payload["trajectory"]["output"] = output
                result = grader.grade(payload, self.trusted)
                self.assertFalse(result["passed"])
                self.assertIn(result["metadata"]["financial_comparison"]["status"], {"partial", "not_assessed"})
                failed = [check for check in result["metadata"]["checks"] if not check["passed"]]
                self.assertNotIn("workflow", {check["category"] for check in failed})

    def test_hypothetical_numbers_require_review_without_being_called_fabricated(self):
        payload = self.payload("enrichment")
        payload["trajectory"]["output"] += "\nHypothetical SQL range: $111.60 to $9,061.92."
        result = grader.grade(payload, self.trusted)
        failed = [check for check in result["metadata"]["checks"] if not check["passed"]]
        self.assertFalse(result["passed"])
        self.assertEqual({(check["category"], check["code"]) for check in failed},
                         {("review", "additional_numbers"), ("review", "unassessed_financial_claims")})
        self.assertEqual(result["metadata"]["financial_comparison"]["status"], "pass")
        self.assertIn("not proof of fabrication", result["evidence"])

    def test_correct_input_sum_without_calculator_still_fails_the_contract(self):
        payload = self.payload("query")
        payload["trajectory"]["output"] = (
            "The supplied actual costs are 42.8544, 17.0000 and 43.5000 USD. "
            "Their total is 103.3544 USD; retail is UNKNOWN.")
        payload["trajectory"]["events"] = [
            {"type": "assistant_message", "data": {"content": payload["trajectory"]["output"]}}]
        result = grader.grade(payload, self.trusted)
        failed = {check["code"] for check in result["metadata"]["checks"] if not check["passed"]}
        self.assertFalse(result["passed"])
        self.assertEqual(failed, {"exact_table", "additional_numbers", "calculator_execution",
                                  "prose_financial_values"})
        self.assertEqual(result["metadata"]["financial_comparison"]["status"], "not_assessed")
        self.assertIn("not proof of fabrication", result["evidence"])

    def test_resource_counts_do_not_permit_the_same_number_as_money_or_percent(self):
        for case in ("query", "focus"):
            payload = self.payload(case)
            payload["trajectory"]["output"] += "\nScope: 3 resources."
            with self.subTest(case=case):
                self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        for text, passed in [("Scope: 3 line items.", True), ("Scope: 999 line items.", False),
                             ("Scope: $3 line items.", False), ("Scope: USD 3 line items.", False)]:
            payload = self.payload("focus")
            payload["trajectory"]["output"] += "\n" + text
            self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)
        for text in [
            "Scope: 999 resources.", "Across 3 resources; extra cost $3.",
            "Across 3 resources; extra discount 3%.", "Across 3 resources; cost 3 USD.",
            "$3 resources.", "$ 3 resources.", "USD 3 resources.",
            "Across 3 resources; extra cost USD**3**.",
        ]:
            with self.subTest(text=text):
                payload = self.payload("query")
                payload["trajectory"]["output"] += "\n" + text
                result = grader.grade(payload, self.trusted)
                self.assertFalse(result["passed"])
                self.assertIn("additional_numbers", result["evidence"])

    def test_financial_amount_formats_cannot_bypass_review(self):
        for text in [
            "The annual saving is $999k.", "Expected annual cost is about $7.67k lower.",
            "The annual saving is USD 999 million.", "The annual saving is $1e6.",
            "| Additional annual saving is $999k.",
        ]:
            with self.subTest(text=text):
                payload = self.payload("annual")
                payload["trajectory"]["output"] += "\n" + text
                result = grader.grade(payload, self.trusted)
                self.assertFalse(result["passed"])
                self.assertIn("unassessed_financial_claims", result["evidence"])

    def test_financial_headlines_require_the_correct_row_and_field(self):
        for text, passed in [
            ("Actual total is 103.35 USD.", True),
            ("Actual total is USD 103.35.", True),
            ("Actual total is 42.85 USD.", False),
            ("Actual total is 42.85.", False),
            ("Actual total is -$-103.35.", False),
            ("Actual total is --103.35 USD.", False),
            ("Actual total is -+103.35 USD.", False),
            ("Actual total is $-+103.35.", False),
            ("Actual total is - 103.35 USD.", False),
            ("Actual total is \u2212 103.35 USD.", False),
            ("Actual total is + 103.35 USD.", True),
            ("- 103.35 USD actual total.", True),
            ("- $103.35 actual total.", True),
            ("Actual total is 103.35 eur.", False),
            ("Actual total is EUR103.35USD.", False),
            ("Actual total is USD103.35EUR.", False),
            ("Actual total is \u20ac103.35.", False),
            ("Actual total is 103.35 euros.", False),
            ("Actual total is pounds 103.35.", False),
            ("Actual total is 103.35 yen.", False),
            ("Actual total is 103.35 dollars.", True),
            ("Actual total is 103.35 USD/year.", False),
            ("Retail total is 103.35 USD.", False),
            ("vm-app01 actual cost is $42.85.", True),
            ("vm-app01 actual cost is $17.00.", False),
            ("vm-app01 retail cost is $42.85.", False),
            ("vm-app01 retail cost is $71.42.", True),
            ("The total discount is 40.0%.", False),
            ("The total discount is 22.5%.", True),
            ("The total discount is 22.5 percent.", True),
            ("The total discount is 22.5 %.", True),
            ("The total discount is $22.5%.", False),
            ("The total discount is USD 22.5%.", False),
            ("Largest discount: vm-app01 at 40.0%. Zero-discount: egress-westus at 0.0%.", True),
            ("The VAT rate is 22.5%.", False),
            ("The VAT rate is 22.5% (not a discount).", False),
            ("The VAT rate is 22.5% (not a blended discount).", False),
            ("The total discount is 22.5%; the VAT rate is 22.5%.", False),
            ("The total discount is 22.5%, the VAT rate is 22.5%.", False),
            ("vm-app01 tax rate is 40.0%.", False),
            ("Retail is 22.5% lower than actual.", False),
        ]:
            with self.subTest(text=text):
                payload = self.payload()
                payload["trajectory"]["output"] += "\n" + text
                result = grader.grade(payload, self.trusted)
                self.assertEqual(result["passed"], passed, result["evidence"])

    def test_unknown_actual_cannot_be_zero_in_prose_even_if_input_contains_zero(self):
        payload = self.payload("missing-actuals")
        payload["trajectory"]["output"] += "\nActual total is $0."
        result = grader.grade(payload, self.trusted)
        self.assertFalse(result["passed"])
        self.assertIn("prose_financial_values", result["evidence"])

    def test_financial_context_preserves_field_and_period_in_natural_summaries(self):
        for case, text, passed in [
            ("standard", "Actual total was $103.35 vs list-price total $133.32 (USD), a $29.97 difference.", True),
            ("standard", "Actual total was $103.35 vs list-price total $133.32 (USD), a $42.85 difference.", False),
            ("standard", "Total actual vs retail: $103.35 actual vs $133.32 retail, so $29.97 savings.", True),
            ("standard", "Total actual vs retail: $133.32 actual vs $103.35 retail, so $29.97 savings.", False),
            ("standard", "Actual total vs list total: 103.35 USD actual vs 133.32 USD retail.", True),
            ("standard", "Actual total vs list total: 133.32 USD actual vs 103.35 USD retail.", False),
            ("standard", "Actual total was 103.35 USD versus retail/list 133.32 USD.", True),
            ("standard", "Actual was $103.35 vs list $133.32, a $29.97 difference (22.5% lower actual).", True),
            ("standard", "Actual was $103.35 vs list $133.32 (savings $29.97, 22.5%).", True),
            ("standard", "Total actual is $103.35 vs $133.32 retail (22.5% blended discount).", True),
            ("savings", "Actual monthly savings from the proposed retail cuts are $51.35 (USD).", True),
            ("savings", "Actual monthly savings from the proposed retail cuts are $80.62 (USD).", False),
            ("removal", "Monthly actual run-rate reduction from inputs/delta-before.json: USD 974.00/mo total; vm1 USD 608.75/mo.", True),
            ("removal", "Monthly actual run-rate reduction from inputs/delta-before.json: USD 96.00/mo total.", False),
            ("removal", "Monthly actual run-rate reduction:\n\n- vm1: $608.75/mo\n- stor1: $304.38/mo", True),
            ("removal", "Monthly actual run-rate reduction:\n\n- vm1: $974.00/mo", False),
            ("annual", "Actual yearly cost change after deployment: -7,670.25 USD/yr.", True),
            ("annual", "Actual yearly cost change after deployment: 33.00 USD/yr.", False),
            ("annual", "Expected actual cost is lower by about 7,670.25 USD per year.", True),
            ("annual", "Expected actual cost is not lower by about 7,670.25 USD per year.", False),
            ("annual", "Before actual cost was $96.00.", True),
            ("annual", "After actual cost was $96.00.", False),
        ]:
            with self.subTest(case=case, text=text):
                payload = self.payload(case)
                payload["trajectory"]["output"] += "\n" + text
                result = grader.grade(payload, self.trusted)
                self.assertEqual(result["passed"], passed, result["evidence"])

    def test_annual_formula_multiplier_without_cost_evidence_is_not_a_money_claim(self):
        payload = self.payload()
        payload["config"]["env"]["ACTUALIZE_CASE"] = "missing-window"
        payload["trajectory"]["output"] = (
            "I need the monthly period first. Annual saving = 12 x monthly actual spend.")
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["output"] += "\nAnnual saving = $12 x monthly actual spend."
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_evidenced_absence_of_zero_discount_is_not_a_new_discount(self):
        payload = self.payload("enrichment")
        payload["trajectory"]["output"] += (
            "\nThere are **no 0% discount items** among rows with known retail.")
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["output"] += "\nThe SQL discount is 0%."
        result = grader.grade(payload, self.trusted)
        self.assertFalse(result["passed"])
        self.assertIn("additional_numbers", result["evidence"])
        payload = self.payload("enrichment")
        payload["trajectory"]["output"] += "\nThere is no confirmed 0% discount line in this output."
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["output"] += "\nThe SQL discount is 0%."
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        payload = self.payload("standard")
        payload["trajectory"]["output"] += "\nThere is no confirmed 0% discount line in this output."
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_collection_tier_labels_do_not_authorize_financial_numbers(self):
        for text, passed in [
            ("Use Tier 1/Tier 3 export commands.", True),
            ("Use Tier 4 for the retail cross-check.", True),
            ("Tier 3 export; extra savings $3.", False),
            ("Tier 3% discount.", False),
            ("Tier 3.00 USD.", False),
            ("Tier 3 USD.", False),
            ("Tier 99 export.", False),
        ]:
            with self.subTest(text=text):
                payload = self.payload("legacy")
                payload["trajectory"]["output"] += "\n" + text
                self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)

    def test_directory_listings_do_not_hide_verified_calculator_execution(self):
        for prefix, passed in [
            ("ls -1 /workspace && ls -1 /workspace/inputs && ", True),
            ("ls -1 /workspace || ", False),
            ("ls -1 /workspace && echo ", False),
        ]:
            with self.subTest(prefix=prefix):
                payload = self.payload("legacy")
                call = payload["trajectory"]["events"][0]["data"]["arguments"]
                call["command"] = prefix + call["command"]
                self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)
        payload = self.payload("legacy")
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = "ls -1 /workspace"
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_monthly_delta_accepts_only_verified_annual_output_with_projection_guidance(self):
        payload = self.payload("annual")
        payload["config"]["env"]["ACTUALIZE_CASE"] = "delta"
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        annual = self.payload("annual")["trajectory"]["events"][:2]
        for event in annual:
            event["data"]["toolCallId"] = "annual"
        monthly = self.payload("delta")
        monthly["trajectory"]["events"] = annual + monthly["trajectory"]["events"]
        self.assertTrue(grader.grade(monthly, self.trusted)["passed"])
        for mutation in ("wrong-amount", "missing-flag", "missing-guidance", "missing-execution"):
            with self.subTest(mutation=mutation):
                payload = self.payload("annual")
                payload["config"]["env"]["ACTUALIZE_CASE"] = "delta"
                if mutation == "wrong-amount":
                    payload["trajectory"]["output"] = payload["trajectory"]["output"].replace(
                        "-7,670.25", "-7,671.25")
                elif mutation == "missing-flag":
                    call = payload["trajectory"]["events"][0]["data"]["arguments"]
                    call["command"] = call["command"].replace(" --annual", "")
                elif mutation == "missing-guidance":
                    payload["trajectory"]["output"] = "\n".join(
                        line for line in payload["trajectory"]["output"].splitlines()
                        if line.startswith("|"))
                else:
                    payload["trajectory"]["events"] = []
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_unknown_cost_refusals_do_not_permit_zero_or_nonzero_claims(self):
        for text, passed in [
            ("Actual cost is **unknown**, not **$0**.", True),
            ("Actual cost is unknown, not $0.00.", True),
            ("Null is not evidence of $0; actual cost is UNKNOWN.", True),
            ("Actual cost is unknown, not $0.01.", False),
            ("Actual cost is not $0; the actual remains UNKNOWN.", False),
            ("Actual cost is UNKNOWN. The cost is $0.", False),
            ("Actual cost is unknown, not $0. The cost is $0.", False),
        ]:
            with self.subTest(text=text):
                payload = self.payload("null-recovery")
                payload["trajectory"]["output"] = "Costs are null; use az rest with AmortizedCost. " + text
                self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)

    def test_combining_currencies_without_calculator_remains_a_failure(self):
        payload = self.payload("mixed-currency")
        payload["trajectory"]["output"] = (
            "Label everything USD: combined actual is 12.00 USD and retail is 20.00 USD.")
        payload["trajectory"]["events"] = []
        result = grader.grade(payload, self.trusted)
        self.assertFalse(result["passed"])
        failed = {check["code"] for check in result["metadata"]["checks"] if not check["passed"]}
        self.assertTrue({"exact_table", "required_guidance", "calculator_execution"} <= failed)

    def test_correct_removal_summary_does_not_replace_the_full_table(self):
        payload = self.payload("removal")
        payload["trajectory"]["output"] = (
            "| Resource | Reduction/mo (USD) |\n|---|---:|\n"
            "| vm1 | 608.75 |\n| stor1 | 304.38 |\n| kv1 | 60.88 |\n| TOTAL | 974.00 |\n\n"
            "With reservation coverage, cash savings may lag until commitments expire.")
        result = grader.grade(payload, self.trusted)
        self.assertFalse(result["passed"])
        failed = [check["code"] for check in result["metadata"]["checks"] if not check["passed"]]
        self.assertEqual(failed, ["exact_table"])

    def test_subscription_recovery_needs_the_switch_command_not_only_prose(self):
        payload = self.payload("scope-recovery")
        guidance = (
            "Resolve the resource subscription using Resource Graph with --query data. "
            "Switch to that subscription before retrying the cost query.")
        payload["trajectory"]["output"] = guidance
        result = grader.grade(payload, self.trusted)
        failed = [check["code"] for check in result["metadata"]["checks"] if not check["passed"]]
        self.assertFalse(result["passed"])
        self.assertEqual(set(failed), {"required_guidance"})
        payload["trajectory"]["output"] += (
            '\n```bash\naz account set --subscription "$SUB"\n'
            'az account show --query id -o tsv\n```')
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["output"] = payload["trajectory"]["output"].replace(
            "az account show --query id -o tsv\n", "")
        result = grader.grade(payload, self.trusted)
        self.assertFalse(result["passed"])
        self.assertIn(r"account\s+show", result["evidence"])

    def test_missing_failed_or_nonexecuting_calls_fail(self):
        for command in [
            "echo 'python3 scripts/actualize.py report --input inputs/modern-usage.json'",
            "false && python3 scripts/actualize.py report --input inputs/modern-usage.json",
            "python3 scripts/actualize.py meters --input inputs/modern-usage.json",
        ]:
            with self.subTest(command=command):
                payload = self.payload()
                payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = command
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        for mutation in ("missing", "tool-error", "process-error", "wrong-tool", "wrong-agent", "late"):
            with self.subTest(mutation=mutation):
                payload = self.payload()
                events = payload["trajectory"]["events"]
                if mutation == "missing":
                    events.pop(0)
                elif mutation == "tool-error":
                    events[1]["data"]["success"] = False
                elif mutation == "process-error":
                    events[1]["data"]["result"]["exitCode"] = 1
                elif mutation == "wrong-tool":
                    events[0]["data"]["toolName"] = "read_file"
                elif mutation == "wrong-agent":
                    events[1]["agentId"] = "another-agent"
                else:
                    events.insert(0, events.pop())
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])
                if mutation == "late":
                    failed = [check["code"] for check in grader.grade(payload, self.trusted)["metadata"]["checks"]
                              if not check["passed"]]
                    self.assertIn("answer_order", failed)
                    self.assertNotIn("calculator_execution", failed)

    def test_wrong_inputs_and_modified_source_fail(self):
        payload = self.payload()
        (self.work / "inputs/modern-usage.json").write_text("[]", encoding="utf-8")
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        payload = self.payload()
        (self.work / "scripts/actualize.py").write_text("print('fake')", encoding="utf-8")
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_annual_flag_and_projection_cannot_be_omitted(self):
        payload = self.payload("annual")
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = (
            payload["trajectory"]["events"][0]["data"]["arguments"]["command"].replace(" --annual", ""))
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        payload = self.payload("annual")
        payload["trajectory"]["output"] = payload["trajectory"]["output"].split("\n>")[0]
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_saving_magnitude_can_describe_a_negative_change(self):
        for text in [
            "The projected annual saving is $7,670.25.",
            "Annual saving is $7,670.25, not immediate cash savings.",
        ]:
            with self.subTest(text=text):
                payload = self.payload("annual")
                payload["trajectory"]["output"] += "\n" + text
                self.assertTrue(grader.grade(payload, self.trusted)["passed"])

    def test_negated_savings_do_not_reverse_the_signed_change(self):
        for text in [
            "The projected annual cost change is +$7,670.25, not a saving.",
            "There are no savings: the projected annual cost change is +$7,670.25.",
            "The projected annual cost increased by $7,670.25 (not a reduction).",
            "There are no annual savings of $7,670.25.",
            "No projected annual saving is $7,670.25.",
        ]:
            with self.subTest(text=text):
                payload = self.payload("annual")
                payload["trajectory"]["output"] += "\n" + text
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_percentage_claims_use_their_own_metric(self):
        for text, passed in [
            ("vm-app01 Actual/Retail ratio is 60.0%.", True),
            ("vm-app01 Actual/Retail ratio is 40.0%.", False),
            ("vm-app01 discount is 60.0%.", False),
            ("vm-app01 tax rate is 60.0%.", False),
        ]:
            with self.subTest(text=text):
                payload = self.payload("savings")
                payload["trajectory"]["output"] += "\n" + text
                self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)

    def test_source_quotes_require_the_supplied_resource_and_field(self):
        for case, text, passed in [
            ("standard", "vm-app01 actual input cost is 17.00 USD.", False),
            ("standard", "The VM input actual cost is 744.00 USD.", False),
            ("standard", "vm-app01 actual input cost is 42.8544 USD.", True),
            ("standard", "vm-app01 retail input cost is 42.8544 USD.", False),
            ("standard", "stproddata01 actual input cost is 17.00 USD.", True),
            ("query", "vm-app01 actual input cost is 42.8544 USD.", True),
            ("standard", "/subscriptions/0000/resourceGroups/rg-prod/providers/Microsoft.Compute/"
             "virtualMachines/vm-app01 actual input cost is 42.8544 USD.", True),
            ("standard", "/subscriptions/other/resourceGroups/rg-prod/providers/Microsoft.Compute/"
             "virtualMachines/vm-app01 actual input cost is 42.8544 USD.", False),
            ("focus", "vm-app01 list cost input was 71.424 USD.", True),
            ("focus", "vm-app01 actual input cost is 71.424 USD.", False),
            ("focus", "vm-app01 list cost input was 71.424 EUR.", False),
        ]:
            with self.subTest(case=case, text=text):
                payload = self.payload(case)
                payload["trajectory"]["output"] += "\n" + text
                result = grader.grade(payload, self.trusted)
                self.assertEqual(result["passed"], passed, result["evidence"])

    def test_source_precision_and_quantities_remain_valid_prose(self):
        for case, text in [("focus", "The VM list cost input was 71.424."),
                           ("standard", "The VM quantity was 744 hours.")]:
            with self.subTest(case=case):
                payload = self.payload(case)
                payload["trajectory"]["output"] += "\n" + text
                self.assertTrue(grader.grade(payload, self.trusted)["passed"])

    def test_calculator_window_days_can_be_explained(self):
        payload = self.payload("delta")
        payload["trajectory"]["output"] += "\nEach measured window is 3 days."
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])

    def test_missing_actuals_are_not_zero(self):
        payload = self.payload("missing-actuals")
        payload["trajectory"]["output"] = payload["trajectory"]["output"].replace("UNKNOWN", "0.00")
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_currency_default_requires_disclosure(self):
        payload = self.payload("default-currency")
        explanation = ("Assumption made: the input has no currency field, "
                       "so currency is labeled USD by default.")
        payload["trajectory"]["output"] = payload["trajectory"]["output"].replace("Amounts assumed USD.", explanation)
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["output"] = payload["trajectory"]["output"].replace(explanation, "")
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_equivalent_shell_and_presentation_are_accepted(self):
        for command in [
            'python -B "scripts/actualize.py" report --input=inputs/modern-usage.json',
            "cd /workspace && python3 scripts/actualize.py report -i inputs/modern-usage.json",
            "cat inputs/modern-usage.json | python3 scripts/actualize.py report --input -",
            "cat inputs/modern-usage.json | python3 scripts/actualize.py report",
        ]:
            with self.subTest(command=command):
                payload = self.payload()
                payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = command
                payload["trajectory"]["output"] = payload["trajectory"]["output"].replace("| ", "|   ").replace("**", "")
                self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload = self.payload("annual")
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = (
            payload["trajectory"]["events"][0]["data"]["arguments"]["command"].replace("--before", "--input"))
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])

    def test_native_skill_paths_respect_working_directory_and_integrity(self):
        shutil.copytree(self.work / "scripts", self.work / "actualize/scripts")
        for command, passed in [
            ("python3 /workspace/actualize/scripts/actualize.py report --input /workspace/inputs/modern-usage.json", True),
            ("cd /workspace && python3 actualize/scripts/actualize.py report --input inputs/modern-usage.json", True),
            ("cd /workspace/actualize && python3 scripts/actualize.py report --input ../inputs/modern-usage.json", True),
            ("cd /workspace/actualize && python3 scripts/actualize.py report --input inputs/modern-usage.json", False),
        ]:
            with self.subTest(command=command):
                payload = self.payload()
                payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = command
                self.assertEqual(grader.grade(payload, self.trusted)["passed"], passed)
        payload = self.payload()
        payload["trajectory"]["events"][0]["data"]["arguments"]["command"] = (
            "python3 actualize/scripts/actualize.py report --input inputs/modern-usage.json")
        (self.work / "actualize/scripts/actualize.py").write_text("print('fake')", encoding="utf-8")
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        (self.work / "actualize/scripts/actualize.py").unlink()
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_equivalent_projection_caveat_is_accepted(self):
        payload = self.payload("annual")
        payload["trajectory"]["output"] = payload["trajectory"]["output"].split("\n>")[0]
        payload["trajectory"]["output"] += "\nThis is a projection, not a measured historical yearly total."
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])

    def test_live_azure_execution_fails_even_if_tool_fails(self):
        payload = self.payload()
        payload["trajectory"]["events"].insert(0, {
            "type": "tool_call", "data": {"toolName": "bash", "toolCallId": "azure",
                                        "arguments": {"command": "az account show"}}})
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_missing_usage_timeout_and_token_excess_fail(self):
        for count in (None, 200001):
            payload = self.payload()
            payload["trajectory"]["metrics"]["tokenUsage"]["totalTokens"] = count
            self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        payload = self.payload()
        payload["trajectory"]["endReason"] = "agent_timeout"
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_guidance_without_evidence_cannot_add_financial_claims(self):
        payload = self.payload("missing-window")
        payload["trajectory"]["output"] = "I need the date window and number of days."
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        for figure in ["$1200", "USD 1200", "$-1200", "-$1200", "1200 EUR", "GBP 1200",
                       "$999k", "1200 per year", "1200 dollars", "12 percent"]:
            with self.subTest(figure=figure):
                payload["trajectory"]["output"] = (
                    "I need the date window and number of days. Your annual saving is " + figure + ".")
                self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_turn_scoping_cannot_hide_an_earlier_violation(self):
        payload = self.payload("missing-window")
        payload["config"]["env"]["ACTUALIZE_TURN"] = "0"
        events = payload["trajectory"]["events"] = [
            {"type": "assistant_message", "turn": 0,
             "data": {"content": "I can\u2019t assume a month; provide its time basis."}},
            {"type": "assistant_message", "turn": 1,
             "data": {"content": "I need dates, but your annual saving is $1200."}},
        ]
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        events[0]["data"], events[1]["data"] = events[1]["data"], events[0]["data"]
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])
        payload["config"]["env"]["ACTUALIZE_TURN"] = "1"
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["config"]["env"]["ACTUALIZE_TURN"] = "2"
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])

    def test_null_cost_refusal_and_awk_fields_are_not_financial_claims(self):
        payload = self.payload("null-recovery")
        payload["trajectory"]["output"] = (
            "**No, that is not evidence of zero spend.** Costs are null; use "
            "az rest with AmortizedCost.\n```bash\nSUB=$(echo \"$RID\" | awk -F/ '{print $3}')\n```")
        self.assertTrue(grader.grade(payload, self.trusted)["passed"])
        payload["trajectory"]["output"] += "\nThe cost is $3."
        self.assertFalse(grader.grade(payload, self.trusted)["passed"])


if __name__ == "__main__":
    unittest.main()
