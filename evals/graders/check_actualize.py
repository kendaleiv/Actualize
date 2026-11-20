#!/usr/bin/env python3
"""Conjunctive behavioral checks over Vally's normalized trajectory."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
from decimal import Decimal


SHELL_TOOLS = {"bash", "powershell", "shell", "run_in_terminal", "exec_command"}
NUMBER = re.compile(r"(?<![\w./,-])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\w/-]|[.,]\d)")
CURRENCY = re.compile(r"\b(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|INR|CNY)\b")
MONEY_UNIT = r"(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|INR|CNY|dollars?|euros?|pounds?|yen)"
AMOUNT = r"(?:[+-]\s*)?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
FINANCIAL_TOKEN = re.compile(
    rf"(?<![\w./-])(?P<prefix>[+-]?\s*(?:{MONEY_UNIT}|[$\u20ac\u00a3\u00a5])\s*)?"
    rf"(?P<amount>{AMOUNT})(?P<scale>\s*(?:[kmb]|thousand|million|billion)\b)?"
    rf"\s*(?P<unit>%|percent\b|{MONEY_UNIT}\b)?"
    r"(?P<period>\s*(?:/\s*(?:mo(?:nth)?|yr|year)|per\s+(?:month|year))\b)?"
    r"(?!\w|[.,]\d)", re.I)
FINANCIAL_MARKER = re.compile(
    rf"(?:\b{MONEY_UNIT}|[$\u20ac\u00a3\u00a5])[\s+-]*(?:\d|\.\d)|"
    rf"(?<![\w.]){AMOUNT}\s*(?:%|percent\b|{MONEY_UNIT}\b)", re.I)
FINANCIAL_ROLE = re.compile(
    r"(?<![\w./-])(actual|retail|list(?:[- ]price)?|savings?|reductions?|discount|difference|change|delta|before|after)(?![\w./-])",
    re.I)
ALIASES = {"-i": "--input", "-b": "--before", "-a": "--after",
           "-c": "--cost", "-r": "--reductions"}


def tables(text):
    found, current = [], []
    for line in text.splitlines() + [""]:
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [re.sub(r"\s+", " ", c.replace("*", "").replace("`", "").strip())
                     for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                current.append(cells)
        elif current:
            found.append(current)
            current = []
    return found


def numbers(text):
    return {(Decimal(m.group().replace(",", "").rstrip("%")), m.group().endswith("%"))
            for m in NUMBER.finditer(text)}


def financial_claim_text(text, reference=None, resource_count=None, line_count=None):
    """Exclude evidenced counts and narrow refusals, not arbitrary zero claims."""
    text = re.sub(r"[*`]+", " ", text)
    text = re.sub(r"(?<=\d)\s+(?:%|percent\b)", "%", text, flags=re.I)
    text = re.sub(rf"\bTier\s+[1-4]\b(?![\d.,%]|\s+{CURRENCY.pattern})",
                  "collection tier", text, flags=re.I)
    text = re.sub(
        r"\b(unknown\s*[,;(]?\s*(?:not|rather than)|not\s+(?:evidence|proof)\s+of)"
        r"\s+\$\s*0(?:\.0+)?(?!\d|[.,]\d|%)",
        r"\1 zero", text, flags=re.I)

    for noun, expected_count in [(r"resources?", resource_count), (r"line items?", line_count)]:
        if expected_count is None:
            continue

        def count(match):
            money_prefix = re.search(rf"(?:\$|{CURRENCY.pattern})\s*$", text[:match.start()], re.I)
            return ("items" if int(match[1]) == expected_count and not money_prefix
                    else match[0])

        text = re.sub(rf"(?<![\w$.,+-])(\d+)\s+{noun}\b", count, text, flags=re.I)

    if reference and "Discount %" in reference[0]:
        column = reference[0].index("Discount %")
        known = [row[column] for row in reference[1:] if row[0] != "TOTAL"
                 and numbers(row[column])]
        if known and all((Decimal(0), True) not in numbers(cell) for cell in known):
            text = re.sub(
                r"\bno\s+0(?:\.0+)?%\s+discount\s+items\s+among\s+rows\s+with\s+known\s+retail\b",
                "no zero-percent discount items among rows with known retail", text, flags=re.I)
            text = re.sub(
                r"\bno\s+(?:confirmed|known|established)\s+0(?:\.0+)?%\s+discount\s+"
                r"(?:lines?|items?|rows?)\s+in\s+(?:this|the)\s+(?:output|report)\b",
                "no established zero-percent discount rows in this report", text, flags=re.I)
    return text


def resource_mentions(reference, context, source_fields=()):
    names = {}
    for row in reference[1:]:
        if row[0] == "TOTAL":
            continue
        short = row[0].rstrip("/").rsplit("/", 1)[-1]
        aliases = {row[0].casefold(), short.casefold(),
                   *({"vm"} if re.match(r"vm(?:[-_\d])", short, re.I) else set())}
        for source in source_fields:
            if row[0].casefold() in source["rows"]:
                aliases |= source["rows"]
        for name in aliases:
            names.setdefault(name, []).append(row)
    return [(match.start(), rows[0]) for name, rows in names.items() if len(rows) == 1
            for match in re.finditer(rf"(?<![\w/-]){re.escape(name)}(?![\w/-])", context, re.I)]


def financial_prose(text, case, reference=None, source_fields=()):
    """Verify identifiable claims; unfamiliar financial prose requires review."""
    def labels_in(context):
        labels = list(FINANCIAL_ROLE.finditer(context))
        scoped = [label for label in labels if label[1].lower() in {"before", "after"}
                  and re.fullmatch(r"(?:\s|[:=()]|\b(?:actual|cost|total|amount|is|was|are|of|at)\b)*",
                                   context[label.end():], re.I)]
        return [label for label in labels if label[1].lower() not in {"before", "after"}] + scoped

    prose = "\n".join(line for line in text.splitlines()
                      if not (line.strip().startswith("|") and line.strip().endswith("|")))
    prose = re.sub(r"(?m)^[ \t]*[-+]\s+", "", prose)
    prose = re.sub(r"\b(zero|blended|largest)-discount\b", r"\1 discount", prose, flags=re.I)
    prose = re.sub(r"\{\s*print\s+\$\d+\s*\}", "", prose).replace("\u2212", "-")
    claims = []
    tokens = list(FINANCIAL_TOKEN.finditer(prose))
    for index, token in enumerate(tokens):
        start = max(prose.rfind("\n", 0, token.start()), -1) + 1
        prefix = prose[start:token.start()]
        previous = tokens[index - 1].end() if index and tokens[index - 1].start() >= start else start
        local = prose[previous:token.start()]
        end = tokens[index + 1].start() if index + 1 < len(tokens) else len(prose)
        following = prose[token.end():min(end, prose.find("\n", token.end())
                                        if "\n" in prose[token.end():] else len(prose))]
        direct = re.search(
            r"\b(?:actual(?:\s+(?:cost|total))?|retail(?:\s+cost)?|savings?|discount|difference|"
            r"change|total)(?:\s+(?:is|was|are|of|at|cost|amount|about|approximately))*\s*[:=]?\s*$",
            local, re.I)
        if not (token["prefix"] or token["unit"] or token["period"] or direct):
            continue
        if (not (token["prefix"] or token["unit"] or token["period"])
                and re.match(r"\s*(?:\u00d7|\*|x\b)", following, re.I)):
            continue
        claim = {"text": token.group().strip(), "status": "unassessed"}
        claims.append(claim)
        if reference is None:
            claim["reason"] = "Financial figure without cost evidence."
            continue
        if re.search(r"[+-]\s*$", local):
            claim["reason"] = "Ambiguous multiple amount signs."
            continue
        if re.search(r"\b(?:hypothetical|what.if|could range|lower.bound)\b", prefix, re.I):
            claim["reason"] = "Hypothetical financial calculation is not established by the calculator."
            continue
        if token["scale"] or re.search(r"[eE]", token["amount"]):
            claim["reason"] = "Compact or scientific financial formatting is not calculator output."
            continue
        if token["prefix"] and re.match(r"[+-]", token["prefix"].lstrip()) and token["amount"][0] in "+-":
            claim["reason"] = "Ambiguous multiple amount signs."
            continue
        value = Decimal(re.sub(r"[\s,]", "", token["amount"]))
        if token["prefix"] and token["prefix"].lstrip().startswith("-"):
            value = -value
        percent = token["unit"] and token["unit"].lower() in {"%", "percent"}
        if percent and token["prefix"]:
            claim["reason"] = "A percentage cannot also be a currency amount."
            continue
        source_quote = (re.search(r"\b(?:input|source|supplied|provided)\b", local, re.I)
                        and not re.search(r"\btotal\b", prefix, re.I))
        context = re.split(r"[,;]|\b(?:versus|vs\.?|so|for)\b", local, flags=re.I)[-1]
        context = re.sub(r"\bretail/list\b", "retail", context, flags=re.I)
        subject = re.split(r"\bfrom\b", context, maxsplit=1, flags=re.I)[0]
        if not re.search(r"\b(?:savings?|reductions?)\b", subject, re.I):
            subject = context
        side = r"(?:actual|retail|list(?:[- ]price)?)(?:\s+(?:cost|total))*"
        if re.search(rf"\b{side}\s+(?:vs\.?|versus)\s+{side}\s*:\s*$", local, re.I):
            subject = ""
        labels = labels_in(subject)
        inherited_context = context
        for _, resource in resource_mentions(reference, context, source_fields):
            for name in {resource[0], resource[0].rstrip("/").rsplit("/", 1)[-1]}:
                inherited_context = re.sub(re.escape(name), "", inherited_context, flags=re.I)
        inherited_context = re.sub(
            r"\b(?:and|at|is|was|are|in|of|the|about|approximately)\b", "", inherited_context, flags=re.I)
        can_inherit = not re.search(r"\w", inherited_context)
        scoped_prefix = re.split(r";|(?<=[.!?])\s+", prefix)[-1]
        if not labels:
            suffix = re.match(
                r"\s*(?:in\s+)?(actual|retail|list(?:[- ]price)?|difference|savings?|reductions?|discount|lower spend)\b",
                following, re.I)
            labels = [suffix] if suffix else labels_in(prefix) if can_inherit else []
        if percent:
            suffix_label = re.match(r"\s*(?:\(\s*)?(?:blended\s+)?(discount|change)\b", following, re.I)
            if suffix_label:
                labels = [suffix_label]
        role = labels[-1][1].lower() if labels else None
        if (case["command"] == "savings" and not direct
                and re.match(r"\s+actual\s+(?:monthly\s+)?savings?\b", following, re.I)):
            role = "saving"
        quote_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", prefix)
        paragraph = re.split(r"\n\s*\n", prose[:start])[-1] + prefix
        scoped_source = (case.get("selection") and len(reference) == 3 and quote_dates
                         and re.search(r"\b(?:selected|source|input)\s+(?:source\s+)?(?:rows?|windows?)\b", paragraph, re.I)
                         and not re.search(r"\b(?:total|sum|average|retail|discount|saving|savings|change|reduction)\b",
                                           local, re.I)
                         and role in {None, "actual", "before"} and not token["period"])
        if scoped_source:
            source_quote, role = True, "before" if case["command"] == "delta" else "actual"
        if role == "lower spend":
            role = "saving"
        if not role and re.search(r"\bcost\b", context if not can_inherit else scoped_prefix, re.I):
            role = "actual"
        if not role and re.search(r"\btotal\b", (context if not can_inherit else scoped_prefix) + following, re.I):
            role = "actual" if case["command"] == "report" else "saving"
        period = (token["period"] or "").lower()
        if not period:
            periods = re.findall(r"\b(annual(?:ized)?|yearly|monthly)\b|/(mo|yr)\b", prefix, re.I)
            period = "".join(periods[-1]).lower() if periods else ""
        implicit_change = not role and period and case["command"] == "delta"
        if implicit_change:
            role = "change"
        if role in {"before", "after"} and period:
            claim["reason"] = "No trusted before/after field establishes this normalized period."
            continue
        if case["command"] != "delta" and re.search(r"annual|year|yr", period):
            claim["reason"] = "No trusted annual calculator field establishes this period."
            continue
        column = None
        headers = reference[0]
        if percent:
            if re.search(r"\bactual\s*/\s*retail(?:\s+ratio)?\s*(?:(?:is|was)\s*)?[:=]?\s*$", local, re.I):
                column = "Actual/Retail" if "Actual/Retail" in headers else None
            elif role == "discount":
                column = "Discount %" if "Discount %" in headers else None
            elif (case["command"] == "report"
                  and role in {"saving", "savings", "reduction", "reductions", "difference"}):
                column = "Discount %" if "Discount %" in headers else None
            elif role in {"change", "delta", "saving", "savings", "reduction", "reductions"}:
                column = "% change" if "% change" in headers else None
        elif role in {"retail", "list", "list-price", "list price"}:
            column = next((h for h in headers if h.startswith("Retail")), None)
        elif case["command"] == "delta":
            if role in {"before", "after"}:
                column = next((h for h in headers if h.startswith(role.title() + " (")), None)
            elif role:
                suffix = "/yr" if re.search(r"annual|year|yr", period) else "/mo" if period else " ("
                column = next((h for h in headers if h.startswith("Chg actual" + suffix)), None)
                if column is None and period and any("removed" in h for h in headers):
                    column = next((h for h in headers if "removed" in h and suffix in h), None)
        elif role in {"saving", "savings", "reduction", "reductions", "difference"}:
            column = "Savings" if "Savings" in headers else next(
                (h for h in headers if h.startswith("Actual reduction")), None)
        elif role == "actual":
            column = next((h for h in headers if h.startswith("Actual (")), None)
        if column is None:
            claim["reason"] = "Cannot associate this amount with a trusted financial field."
            continue
        row = reference[-1]
        mentions = resource_mentions(reference, prefix, source_fields)
        totals = list(re.finditer(r"\btotal\b", prefix, re.I))
        if mentions and (not totals or max(m[0] for m in mentions) > totals[-1].start()):
            row = max(mentions, key=lambda mention: mention[0])[1]
        elif scoped_source:
            row = reference[1]
        field = row[headers.index(column)]
        expected = numbers(field)
        if percent and re.search(r"\blargest\b", prefix, re.I) and not mentions:
            values = [number for candidate in reference[1:] if candidate[0] != "TOTAL"
                      for number in numbers(candidate[headers.index(column)])]
            expected = {max(values)} if values else set()
        heading = ""
        if case["command"] == "delta" and "--after" not in case["inputs"]:
            headings = [line for line in prose[:start].splitlines()
                        if line.rstrip().endswith(":") and not line.lstrip().startswith("-")]
            heading = headings[-1] if headings else ""
        saving_role = role in {"saving", "savings", "reduction", "reductions"}
        saving_role |= bool(re.search(
            r"\b(?:cost|spend)\s+(?:is\s+|was\s+)?lower\s+by\s+(?:about\s+)?$", local, re.I))
        removed_run_rate = (case["command"] == "delta" and "--after" not in case["inputs"]
                            and (role == "actual" or implicit_change) and period
                            and re.search(r"\b(?:deleting|removing)\b[^.!?\n]*\bwould\s+(?:remove|eliminate|stop)\b",
                                          prefix, re.I))
        saving_role |= bool(removed_run_rate)
        magnitude_context = local if re.search(r"\b(?:savings?|reductions?)\b", local, re.I) else following
        if removed_run_rate:
            magnitude_context = re.split(r"\b(?:deleting|removing)\b", prefix, flags=re.I)[-1]
        if heading and (implicit_change or (role == "actual" and can_inherit
                                            and re.search(r"\brun[- ]rate\s+avoided\b", heading, re.I))):
            saving_role = bool(re.search(
                r"\b(?:saving|savings|reduction|reductions)\b|\brun[- ]rate\s+avoided\b", heading, re.I))
            magnitude_context = heading
        negated_saving = re.search(
            r"\b(?:no|not|never|without)\s+(?:\w+[\s-]+){0,4}"
            r"(?:saving|savings|reduction|reductions)\b", magnitude_context, re.I)
        if saving_role and not negated_saving:
            expected |= {(abs(amount), unit) for amount, unit in expected if amount < 0}
        unit_text = " ".join((token["prefix"] or "", token["unit"] or ""))
        currency = set(CURRENCY.findall(unit_text.upper()))
        symbols = set(re.findall(r"[$\u20ac\u00a3\u00a5]", token["prefix"] or ""))
        words = re.findall(r"\b(dollars?|euros?|pounds?|yen)\b", unit_text, re.I)
        if any("MIXED" in header for header in headers) and (currency or symbols or words):
            claim["reason"] = "Resource currency cannot be established from a mixed-currency table."
            continue
        trusted_currency = set(CURRENCY.findall(" ".join(headers)))
        symbol_currencies = {"$": {"USD", "CAD", "AUD"}, "\u20ac": {"EUR"},
                             "\u00a3": {"GBP"}, "\u00a5": {"JPY", "CNY"}}
        word_currencies = {"dollar": {"USD", "CAD", "AUD"}, "euro": {"EUR"},
                           "pound": {"GBP"}, "yen": {"JPY"}}
        wrong_currency = bool(currency - trusted_currency) or any(
            not trusted_currency.intersection(symbol_currencies[symbol]) for symbol in symbols)
        wrong_currency |= any(not trusted_currency.intersection(word_currencies[word.lower().rstrip("s")])
                              for word in words)
        if source_quote and (scoped_source or (value, bool(percent)) not in expected):
            field_role = ("actual" if column.startswith(("Actual (", "Before (")) else
                          "retail" if column.startswith("Retail (") else None)
            quotes = [source for source in source_fields
                      if row[0].casefold() in source["rows"] and source["field"] == field_role
                      and (not scoped_source or source.get("date") == quote_dates[-1])]
            claim.update({"row": row[0], "column": column})
            if (not mentions and not scoped_source) or not quotes or percent or period:
                claim["reason"] = "Cannot associate this source quote with a supplied resource and field."
            else:
                claim["status"] = ("source_quote" if not wrong_currency
                                   and any(value == source["value"] for source in quotes) else "mismatch")
            continue
        claim.update({"row": row[0], "column": column, "expected": field,
                      "status": "verified" if (value, bool(percent)) in expected and not wrong_currency else "mismatch"})
    for introducer in FINANCIAL_MARKER.finditer(prose):
        if any(date.start() <= introducer.start() < date.end()
               for date in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b", prose)):
            continue
        if not any(token.start() <= introducer.start() < token.end() for token in tokens):
            claims.append({"text": prose[introducer.start():introducer.end()],
                           "status": "unassessed", "reason": "Unrecognized financial amount format."})
    return claims


def financial_comparison(expected, actual):
    """Compare identifiable cells for diagnostics, not to relax table fidelity."""
    comparison = {"status": "not_assessed", "compared_cells": 0, "differences": [], "unassessed": []}
    if len(expected) != 1 or len(actual) != 1:
        comparison["unassessed"].append("A single comparable answer table is unavailable.")
        return comparison

    def header(value):
        return re.sub(r"\s+", " ", CURRENCY.sub("", value).replace("()", "")).strip().casefold()

    def value(cell):
        numeric = CURRENCY.sub("", cell).replace("$", "").strip()
        if NUMBER.fullmatch(numeric):
            return next(iter(numbers(numeric)))
        return cell.casefold()

    reference, answer = expected[0], actual[0]
    columns = {header(name): index for index, name in enumerate(answer[0])}
    rows = {row[0]: row for row in answer[1:] if row}
    if len(rows) != len(answer) - 1 or set(rows) != {row[0] for row in reference[1:]}:
        comparison["unassessed"].append("Answer row identities are missing, extra, or duplicated.")
    for row in reference[1:]:
        for index, name in enumerate(reference[0][1:], 1):
            if name.casefold() == "note":
                continue
            column = columns.get(header(name))
            observed = rows.get(row[0], [])
            if column is None or column >= len(observed):
                comparison["unassessed"].append(f"{row[0]} / {name}")
                continue
            comparison["compared_cells"] += 1
            actual_currency = set(CURRENCY.findall(answer[0][column] + " " + observed[column]))
            expected_currency = set(CURRENCY.findall(name + " " + row[index]))
            if (value(observed[column]) != value(row[index])
                    or (actual_currency and expected_currency and actual_currency != expected_currency)):
                comparison["differences"].append({
                    "row": row[0], "column": name, "expected": row[index], "observed": observed[column],
                })
    comparison["status"] = ("fail" if comparison["differences"] else
                            "partial" if comparison["unassessed"] else "pass")
    return comparison


def input_numbers(path):
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".json":
        text = json.dumps(json.loads(text))
    elif path.suffix == ".csv":
        text = "\n".join("\n".join(row) for row in csv.reader(io.StringIO(text)))
    return numbers(text)


def input_financial_fields(path, selection=None):
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".csv":
        records = list(csv.DictReader(io.StringIO(text)))
    elif path.suffix == ".json":
        records = json.loads(text, parse_float=Decimal)
        if isinstance(records, dict):
            records = records.get("properties", records)
            if "columns" in records and "rows" in records:
                records = [dict(zip((column["name"] for column in records["columns"]), row))
                           for row in records["rows"]]
        if not isinstance(records, list):
            return []
    else:
        return []
    fields = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if selection and not all(record.get(key) in values for key, values in selection.items()):
            continue
        record = {key.strip().casefold(): value for key, value in record.items()}
        names = {str(record[key]).strip().casefold() for key in
                 ("resourcename", "instancename", "resourceid", "instanceid") if record.get(key)}
        names |= {name.rstrip("/").rsplit("/", 1)[-1] for name in names}
        for field, keys in [("actual", ("effectivecost", "costinbillingcurrency", "pretaxcost", "actualcost")),
                            ("retail", ("listcost",))]:
            for key in keys:
                value = str(record.get(key, "")).strip()
                if names and NUMBER.fullmatch(value):
                    fields.append({"rows": names, "field": field, "value": Decimal(value.replace(",", "")),
                                   "date": str(record.get("date", ""))[:10]})
    return fields


def result_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(result_text(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(result_text(value[k]) for k in
                         ("stdout", "stderr", "content", "text", "result", "output", "detailedContent")
                         if k in value)
    return ""


def failed_result(value):
    if isinstance(value, dict):
        if value.get("isError") is True or value.get("success") is False:
            return True
        if any(value.get(k, 0) not in (0, "0", None) for k in ("exitCode", "exit_code")):
            return True
        return any(failed_result(v) for v in value.values())
    if isinstance(value, list):
        return any(failed_result(v) for v in value)
    return False


def shell_parts(command):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace = " \t\r"
        parts, part, operators = [], [], []
        for token in lexer:
            if token in {"&&", "||", ";", "|", "\n"}:
                if part:
                    parts.append(part)
                    operators.append(token)
                    part = []
            else:
                part.append(token)
        if part:
            parts.append(part)
        return parts, operators
    except ValueError:
        return [], []


def shell_preparation(command):
    files = {}
    command = re.sub(r"\bprintf[ \t]+(['\"])%s\n\1",
                     lambda match: f"printf {match[1]}%s\\n{match[1]}", command)
    pattern = re.compile(
        r"(?m)^(?P<line>[^\n]*?)<<[ \t]*(?P<quote>['\"]?)(?P<tag>[A-Za-z_]\w*)(?P=quote)[ \t]*\n"
        r"(?P<body>.*?)\n(?P=tag)[ \t]*(?=\n|$)", re.S)

    def replace(match):
        line = match["line"]
        parts, _ = shell_parts(line)
        if not parts or not parts[-1]:
            return match[0]
        args = parts[-1]
        if (len(args) == 3 and args[0] == "cat" and args[1] == ">"
                and (match["quote"] or not re.search(r"[$`\\]", match["body"]))):
            files[args[2]] = match["body"] + "\n"
            return line
        if args in (["python", "-"], ["python3", "-"]):
            return line
        return match[0]

    command = pattern.sub(replace, command)

    def process_input(match):
        try:
            args = shlex.split(match[1])
        except ValueError:
            return match[0]
        if (len(args) < 3 or args[0] != "printf" or args[1] not in {"%s\n", r"%s\n"}
                or any(re.search(r"[$`]", arg) for arg in args[2:])):
            return match[0]
        path = f"/workspace/.actualize-process-input-{len(files)}"
        files[path] = "\n".join(args[2:]) + "\n"
        return path

    return re.sub(r"<\(([^()]*)\)", process_input, command), files


def prior_literal_inputs(events, index):
    files = {}
    for event in events[:index]:
        data = event.get("data", {})
        if event.get("type") == "tool_call" and data.get("toolName") in SHELL_TOOLS:
            files.clear()
        if event.get("type") != "tool_call" or data.get("toolName") != "apply_patch":
            continue
        if not any(result.get("type") == "tool_result"
                   and result.get("data", {}).get("toolCallId") == data.get("toolCallId")
                   and result.get("agentId") == event.get("agentId")
                   and result.get("data", {}).get("toolName") == data.get("toolName")
                   and result.get("data", {}).get("success") is True
                   and not failed_result(result.get("data", {}).get("result"))
                   for result in events[:index]):
            continue
        patch = data.get("arguments", "")
        if not isinstance(patch, str):
            continue
        for match in re.finditer(r"(?m)^\*\*\* Add File: ([^\n]+)\n((?:\+[^\n]*\n)+)", patch):
            files[match[1]] = "\n".join(line[1:] for line in match[2].splitlines()) + "\n"
        for match in re.finditer(r"(?m)^\*\*\* (?:Delete|Update) File: ([^\n]+)", patch):
            files.pop(match[1], None)
    return files


def calculator_invocation(command):
    command, prepared = shell_preparation(command)
    if "$(" in command or "`" in command:
        return None
    parts, operators = shell_parts(command)
    if "||" in operators:
        return None
    calls = []
    finished_sink = False
    variables = {}
    directory = PurePosixPath("/workspace")
    for index, part in enumerate(parts):
        if not part:
            continue
        if finished_sink:
            return None
        executable = PurePosixPath(part[0].replace("\\", "/")).name
        if calls:
            output = calls[-1][3]
            if executable == "tee" and len(part) == 2 and operators[index - 1] == "|" and not output:
                calls[-1] = (*calls[-1][:3], part[1])
                finished_sink = True
                continue
            if (executable == "cat" and len(part) == 2 and output
                    and operators[index - 1] in {"&&", "\n"}
                    and posixpath.normpath(str(directory / part[1]))
                    == posixpath.normpath(str(directory / output))):
                finished_sink = True
                continue
            if not re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", executable):
                return None
        if len(part) == 1 and re.match(r"^[A-Za-z_]\w*=", part[0]):
            key, value = part[0].split("=", 1)
            variables[key] = value
            continue
        if executable == "cd":
            if len(part) != 2:
                return None
            directory = PurePosixPath(posixpath.normpath(str(directory / part[1])))
            continue
        if executable in {"cat", "ls"}:
            continue
        if executable == "mkdir" and len(part) >= 3 and part[1] == "-p" and all(
                PurePosixPath(posixpath.normpath(str(directory / path))).is_relative_to("/workspace")
                and not path.startswith("-") for path in part[2:]):
            continue
        if (executable == "printf" and len(part) >= 5 and part[1] in {"%s\n", r"%s\n"}
                and part[-2] == ">"):
            values = []
            for value in part[2:-2]:
                if value.startswith("$"):
                    value = variables.get(value[1:].strip("{}"))
                if value is None:
                    return None
                values.append(value)
            prepared[part[-1]] = "\n".join(values) + "\n"
            continue
        if not re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", executable):
            return None
        args = part[1:]
        if args and args[0] in {"-", "-c"}:
            if calls:
                return None
            continue
        while args and args[0] in {"-u", "-B", "-I"}:
            args = args[1:]
        if len(args) < 2:
            return None
        script = posixpath.normpath(str(directory / args[0].replace("\\", "/")))
        if script not in {"/workspace/scripts/actualize.py", "/workspace/actualize/scripts/actualize.py"}:
            if not re.fullmatch(
                    r"/var/lib/vally/task-inputs/environment/skills/\d+/\d+/actualize/scripts/actualize\.py",
                    script):
                return None
        if any(flag in args[1:] for flag in ("--help", "-h")):
            if calls:
                return None
            continue
        output = None
        if len(args) >= 3 and args[-2] == ">":
            output = args[-1]
            args = args[:-2]
        if any(arg in {"<", ">", ">>", "<<"} for arg in args):
            return None
        calls.append((args[1:], str(directory), script, output))
        if index < len(operators) and operators[index] == ";":
            return None
    return (*calls[-1][:3], prepared, calls[-1][3]) if calls else None


def calculator_args(command):
    invocation = calculator_invocation(command)
    return invocation[:3] if invocation else None


def options(args):
    result = {}
    i = 1
    while i < len(args):
        arg = args[i]
        if "=" in arg and arg.startswith("--"):
            key, value = arg.split("=", 1)
            result[key] = value
        elif arg.startswith("-"):
            key = ALIASES.get(arg, arg)
            if i + 1 < len(args) and (args[i + 1] == "-" or not args[i + 1].startswith("-")):
                i += 1
                result[key] = args[i]
            else:
                result[key] = True
        i += 1
    return result


def workspace_text(workspace, path, directory="/workspace", prepared=None):
    if not isinstance(path, str) or path == "-":
        return None
    container_path = PurePosixPath(posixpath.normpath(str(PurePosixPath(directory) / path)))
    for name, text in (prepared or {}).items():
        if container_path == PurePosixPath(posixpath.normpath(str(PurePosixPath(directory) / name))):
            return text
    if not container_path.is_relative_to("/workspace"):
        return None
    candidate = (workspace / str(container_path.relative_to("/workspace"))).resolve()
    if not candidate.is_relative_to(workspace.resolve()) or candidate.is_symlink():
        return None
    try:
        return candidate.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def selected_rows(rows, selection):
    return [row for row in rows if all(row.get(field) in values for field, values in selection.items())]


def same_input(workspace, path, source, directory="/workspace", selection=None, resources=None, prepared=None):
    actual = workspace_text(workspace, path, directory, prepared)
    if actual is None:
        return False
    try:
        expected = source.read_text(encoding="utf-8-sig")
        if source.suffix == ".json":
            actual, expected = json.loads(actual), json.loads(expected)
            if selection:
                if not isinstance(actual, list) or not all(isinstance(row, dict) for row in actual):
                    return False
                source_ids = {row["resourceId"] for row in expected if "resourceId" in row}
                expected = selected_rows(expected, selection)
                if resources is not None:
                    if "resourceId" not in selection:
                        return False
                    text = workspace_text(workspace, resources, directory, prepared)
                    if text is None:
                        return False
                    resource_tables = tables(text)
                    if resource_tables:
                        requested = [row[0] for row in resource_tables[0][1:]]
                    elif "," in text:
                        rows = list(csv.reader(io.StringIO(text)))
                        requested = [row[0].strip() for row in rows if row]
                        if requested and re.fullmatch(r"resource(?:id)?|id|name", requested[0], re.I):
                            requested = requested[1:]
                    else:
                        requested = [line.strip() for line in text.splitlines() if line.strip()]
                    resolved = set()
                    for item in requested:
                        matches = {rid for rid in source_ids if rid.casefold() == item.casefold()
                                   or rid.casefold().endswith("/" + item.casefold())}
                        if len(matches) != 1:
                            return False
                        resolved |= matches
                    if resolved != set(selection["resourceId"]):
                        return False
                    actual = selected_rows(actual, {"resourceId": selection["resourceId"]})
                remaining = list(actual)
                for row in expected:
                    if row not in remaining:
                        return False
                    remaining.remove(row)
                return not remaining
            return actual == expected
        return actual.replace("\r\n", "\n").strip() == expected.replace("\r\n", "\n").strip()
    except (OSError, ValueError):
        return False


def grade(payload, trusted):
    trajectory = payload["trajectory"]
    cases = json.loads((trusted / "cases.json").read_text(encoding="utf-8"))
    case_name = payload["config"]["env"]["ACTUALIZE_CASE"]
    case = cases[case_name]
    workspace = Path(trajectory["workDir"])
    events = trajectory.get("events", [])
    output = trajectory.get("output", "")
    checks = []

    def check(passed, category, code, evidence):
        checks.append({"category": category, "code": code, "passed": bool(passed), "evidence": evidence})

    selected_turn = payload["config"]["env"].get("ACTUALIZE_TURN")
    if selected_turn is not None:
        events = [event for event in events if event.get("turn") == int(selected_turn)]
        check(bool(events), "execution", "conversation_turn", f"Missing conversation turn: {selected_turn}")
        output = "\n".join(event.get("data", {}).get("content", "") for event in events
                           if event.get("type") == "assistant_message")

    # An annual calculator table retains every requested monthly delta column.
    if case_name == "delta" and any("Chg actual/yr" in table[0] for table in tables(output)):
        for event in events:
            data = event.get("data", {})
            arguments = data.get("arguments", {})
            if (event.get("type") != "tool_call" or data.get("toolName") not in SHELL_TOOLS
                    or not isinstance(arguments, dict)):
                continue
            invocation = calculator_args(arguments.get("command", arguments.get("cmd", "")))
            if (invocation and invocation[0][0] == "delta"
                    and options(invocation[0]).get("--annual") is True):
                case = cases["annual"]
                break

    check(bool(output.strip()), "execution", "answer_present", "The agent produced no answer.")
    check(trajectory.get("endReason", "completed") == "completed",
          "execution", "completion", "The agent did not complete.")
    check(not any(e.get("type") == "error" for e in events),
          "execution", "trajectory_errors", "The trajectory contains an execution error.")
    token_count = trajectory.get("metrics", {}).get("tokenUsage", {}).get("totalTokens")
    check(isinstance(token_count, (int, float)) and 0 <= token_count <= 200000,
          "execution", "token_limit",
          "Token usage is unavailable or exceeds the 200000-token verdict limit.")
    integrity = json.loads((trusted / "integrity.json").read_text(encoding="utf-8"))
    for path, digest in integrity.items():
        copies = [workspace / path]
        skill_copy = workspace / "actualize" / path
        if skill_copy.exists() or skill_copy.is_symlink():
            copies.append(skill_copy)
        for file in copies:
            check(file.is_file() and not file.is_symlink()
                  and hashlib.sha256(file.read_bytes()).hexdigest() == digest,
                  "execution", "source_integrity",
                  f"Agent-visible trusted source or input changed: {file.relative_to(workspace)}")

    for event in events:
        data = event.get("data", {})
        if event.get("type") == "tool_call" and data.get("toolName") in SHELL_TOOLS:
            args = data.get("arguments", {})
            command = args.get("command", args.get("cmd", "")) if isinstance(args, dict) else ""
            parts, _ = shell_parts(command) if isinstance(command, str) else ([], [])
            check(not any(p and p[0] == "az" for p in parts),
                  "workflow", "offline_execution", "Attempted live Azure CLI execution.")

    guidance = output.replace("\u2019", "'").replace("*", "").replace("`", "")
    for pattern in case.get("required", []):
        check(re.search(pattern, guidance, re.I | re.S) is not None,
              "workflow", "required_guidance", f"Missing required guidance: {pattern}")
    for pattern in case.get("forbidden", []):
        check(re.search(pattern, guidance, re.I | re.S) is None,
              "workflow", "forbidden_guidance", f"Forbidden claim or command: {pattern}")
    shell = "\n".join(re.findall(r"```(?:bash|sh|shell)?[ \t]*\n(.*?)```", output, re.S))
    shell = "\n".join(line for line in shell.splitlines() if not line.lstrip().startswith("#"))
    for pattern in case.get("shell_required", []):
        check(re.search(pattern, shell.replace("\\\n", " "), re.I | re.S) is not None,
              "workflow", "required_shell", f"Missing executable collection guidance: {pattern}")
    for pattern in case.get("shell_forbidden", []):
        check(re.search(pattern, shell, re.I | re.S) is None,
              "workflow", "forbidden_shell", f"Invalid collection guidance: {pattern}")

    comparison = {"status": "not_applicable"}
    prose_claims = []
    if "table" in case:
        expected = (trusted / case["table"]).read_text(encoding="utf-8")
        faithful_table = tables(output) == tables(expected)
        check(faithful_table, "presentation", "exact_table",
              "The answer did not reproduce the calculator table as-is (cells, columns, headers, or rows).")
        comparison = financial_comparison(tables(expected), tables(output))
        if comparison["status"] in {"pass", "fail"}:
            check(comparison["status"] == "pass", "correctness", "comparable_financial_cells",
                  f"Comparable financial cells differ from the trusted output: {comparison['differences']}")
        permitted = numbers(expected)
        permitted |= {(Decimal(days), False) for days in
                      re.findall(r"\b(?:before|after)=(\d+(?:\.\d+)?)d\b", expected)}
        source_values = set()
        source_fields = []
        for name in case.get("inputs", {}).values():
            source_values |= input_numbers(trusted / "inputs" / name)
            source_fields.extend(input_financial_fields(trusted / "inputs" / name, case.get("selection")))
        permitted |= source_values
        # A negative change can be described as a positive saving; table signs stay exact.
        permitted |= {(abs(value), percent) for value, percent in permitted}
        reference = tables(expected)[0]
        resource_count = None
        if (case["command"] == "report"
                and options([case["command"], *case.get("flags", [])]).get("--group-by", "resource") == "resource"):
            resource_count = sum(row[0] != "TOTAL" for row in reference[1:])
        line_count = None
        source = case.get("inputs", {}).get("--input")
        if resource_count is not None and source and Path(source).suffix == ".csv":
            line_count = len(list(csv.DictReader(io.StringIO(
                (trusted / "inputs" / source).read_text(encoding="utf-8-sig")))))
        claim_text = financial_claim_text(output, reference, resource_count, line_count)
        claims = numbers(claim_text)
        check(not (claims - permitted), "review", "additional_numbers",
              "Additional numerical claims not verified by this grader (not proof of fabrication): "
              f"{sorted(str(n) for n in claims - permitted)}")
        prose_claims = financial_prose(claim_text, case, reference, source_fields)
        check(not any(c["status"] == "mismatch" for c in prose_claims),
              "correctness", "prose_financial_values",
              f"Financial prose disagrees with its trusted row/field: {[c for c in prose_claims if c['status'] == 'mismatch']}")
        check(not any(c["status"] == "unassessed" for c in prose_claims),
              "review", "unassessed_financial_claims",
              f"Financial claims require review: {[c for c in prose_claims if c['status'] == 'unassessed']}")
        check(set(CURRENCY.findall(output)) <= set(CURRENCY.findall(expected)),
              "correctness", "currency_labels", "Currency labels differ from the trusted output.")
        successful = False
        faithful_after_result = False
        for call_index, call in enumerate(events):
            data = call.get("data", {})
            if call.get("type") != "tool_call" or data.get("toolName") not in SHELL_TOOLS:
                continue
            arguments = data.get("arguments", {})
            if not isinstance(arguments, dict):
                continue
            invocation = calculator_invocation(arguments.get("command", arguments.get("cmd", "")))
            if not invocation:
                continue
            args, directory, script, prepared, saved_output = invocation
            prepared = {**prior_literal_inputs(events, call_index), **prepared}
            if args[0] != case["command"]:
                continue
            if script == "/workspace/actualize/scripts/actualize.py" and not (
                    workspace / "actualize/scripts/actualize.py").is_file():
                continue
            opts = options(args)
            if args[0] == "delta" and "--input" in opts:
                opts["--before"] = opts.pop("--input")
            parts, operators = shell_parts(arguments.get("command", arguments.get("cmd", "")))
            if "|" in operators and parts and len(parts[0]) == 2 and parts[0][0] == "cat":
                if args[0] == "report":
                    opts.setdefault("--input", "-")
                opts = {key: parts[0][1] if value == "-" else value for key, value in opts.items()}
            if not all(opts.get(flag) is True for flag in case.get("flags", [])):
                continue
            if not all(same_input(workspace, opts.get(flag), trusted / "inputs" / name, directory,
                                  case.get("selection"), opts.get("--resources"), prepared)
                       for flag, name in case.get("inputs", {}).items()):
                continue
            if saved_output and tables(workspace_text(workspace, saved_output, directory) or "") != tables(expected):
                continue
            for result_index, event in enumerate(events[call_index + 1:], call_index + 1):
                result = event.get("data", {})
                if (event.get("type") != "tool_result" or result.get("toolCallId") != data.get("toolCallId")
                        or event.get("agentId") != call.get("agentId")
                        or result.get("toolName") != data.get("toolName")):
                    continue
                text = result_text(result.get("result"))
                if (result.get("success") is not True or failed_result(result.get("result"))
                        or re.search(r"exit(?:ed with)? (?:code|status)\s*[:=]?\s*[1-9]\d*", text, re.I)
                        or re.search(r"(?m)^actualize(?:\.py)?: error:", text)):
                    continue
                result_tables = tables(text)
                annual_table = tables(expected)[0]
                allowed_tables = [annual_table]
                yearly = [i for i, header in enumerate(annual_table[0]) if "/yr" in header]
                if yearly:
                    allowed_tables.append([[cell for i, cell in enumerate(row) if i not in yearly]
                                           for row in annual_table])
                if annual_table not in result_tables or any(table not in allowed_tables for table in result_tables):
                    continue
                successful = True
                if any(e.get("type") == "assistant_message" and tables(e.get("data", {}).get("content", "")) == tables(expected)
                       for e in events[result_index + 1:]):
                    faithful_after_result = True
        check(successful, "workflow", "calculator_execution",
              "No verified successful calculator execution with the correct inputs/flags and trusted result was found.")
        if successful and faithful_table:
            check(faithful_after_result, "workflow", "answer_order",
                  "The faithful answer was not recorded after the verified calculator result.")
    else:
        prose_claims = financial_prose(financial_claim_text(output), case)
        check(not prose_claims, "review", "unevidenced_financial_figure",
              "Financial figure without cost evidence requires review.")

    failures = [item for item in checks if not item["passed"]]
    return {"name": "actualize-contract", "kind": "code", "passed": not failures,
            "score": 0 if failures else 1,
            "evidence": "\n".join(f"[{item['category']}/{item['code']}] {item['evidence']}"
                                 for item in failures) if failures else "All contract checks passed.",
            "metadata": {"diagnostic_version": 1, "checks": checks, "financial_comparison": comparison,
                         "financial_prose": prose_claims}}


if __name__ == "__main__":
    try:
        payload = json.loads(Path(os.environ["EVALUATE_GRADER_INPUT"]).read_text(encoding="utf-8"))
        trusted = Path(__file__).resolve().parent / "expected"
        print(json.dumps(grade(payload, trusted)))
    except (OSError, KeyError, ValueError, TypeError) as error:
        print(json.dumps({"name": "actualize-contract", "kind": "code", "status": "error",
                          "passed": False, "score": 0, "evidence": f"Invalid or missing grading evidence: {error}"}))
