"""Reference task template: reconcile a month of invoices from a damaged export (DESIGN.md sections 3, 6.4 and 6.10).

Over a mid market company world the task picks a month, damages some of that
month's rows in invoices.csv the way real exports get damaged and asks for the
month's open and overdue totals in a workbook. With two apps it also asks for
a summary email to the CFO listing the invoices whose rows needed fixing. The
difficulty axes map onto the task:

* apps: 1 is the workbook alone; 2 adds the summary email (mail and files are the integrations there are)
* distractors: unrelated mail in the persona's inbox
* corrupted_inputs: damaged amounts among the month's rows
* ambiguity: what the instruction leaves out, which the user simulator answers from the hidden intent
* dependency_depth: from 1 the summary must also state the totals the workbook holds
* oracle_steps: sizes nothing here; the reward uses it to weigh efficiency

The oracle writes the workbook and the sent summary from the guest's Python as
the desktop user, so it checks the environment and the verifiers end to end
without depending on any screen layout.
"""

import csv
import io
import json
import random
import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from rlworld.actions.models import ActionModel, DoneAction, ExecAction
from rlworld.images.linux import GUEST_PYTHON
from rlworld.rewards.checks import check_result, entities_mentioned, parse_number
from rlworld.rewards.records import CheckResult
from rlworld.rewards.verifiers import FunctionVerifier, Verifier, VerifierContext
from rlworld.specs.models import DifficultySpec
from rlworld.tasks.generators import GeneratedTask, TaskGenerationError
from rlworld.worlds.integrations import USER_WORLD_FOLDER
from rlworld.worlds.model import Document, World
from rlworld.worlds.perturbations import INVOICE_EXPORT_PATH, apply_perturbations, corrupt_amount

TEMPLATE_NAME = "tasks.finance.reconcile_invoices"
ORACLE_NAME = "oracles.finance.reconcile_invoices"
VALUES_VERIFIER = "verifiers.excel.reconciliation_values_match"
SUMMARY_VERIFIER = "verifiers.mail.summary_lists_discrepant_invoices"
WORKBOOK = "reconciliation.xlsx"
CFO_ROLE = "Chief financial officer"
TOTALED_STATUSES = ("open", "overdue")
MAX_APPS = 2
HALF_CENT = 0.005
NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")
ORACLE_MESSAGE_ID = "oracle_summary"


def month_label(month: str) -> str:
    return datetime.strptime(f"{month}-01", "%Y-%m-%d").strftime("%B %Y")


def damaged_export(world: World, invoice_ids: set[str], random_generator: random.Random) -> World:
    """The world with those invoices' amounts damaged in the export; the invoice records keep the true amounts."""
    documents = []
    for document in world.documents:
        if document.path == INVOICE_EXPORT_PATH and invoice_ids:
            rows = list(csv.reader(io.StringIO(document.content)))
            amount_column = rows[0].index("amount")
            for row in rows[1:]:
                if row[0] in invoice_ids:
                    row[amount_column] = corrupt_amount(row[amount_column], random_generator)
            buffer = io.StringIO()
            csv.writer(buffer, lineterminator="\n").writerows(rows)
            document = Document(path=document.path, kind="csv", content=buffer.getvalue())
        documents.append(document)
    return world.model_copy(update={"documents": tuple(documents)})


def cell_matches(actual: Any, expected: Any) -> bool:
    """Labels match without case; amounts match to the cent, however the app formatted them."""
    if isinstance(expected, str):
        return isinstance(actual, str) and actual.strip().lower() == expected
    try:
        return abs(parse_number(actual) - expected) < HALF_CENT
    except (TypeError, ValueError):
        return False


def reconciliation_values_match(context: VerifierContext) -> list[CheckResult]:
    truth = context.ground_truth
    workbook = (context.final_state.get("files") or {}).get(truth["workbook"]) or {}
    if workbook.get("kind") != "spreadsheet":
        return [check_result("reconciliation_values", "exact", False,
                             f"{truth['workbook']} is missing or is not a spreadsheet", truth["cells"])]
    cells = workbook["cells"]
    actual = {cell_name: (cells.get(cell_name) or {}).get("value") for cell_name in truth["cells"]}
    mismatches = [f"{cell_name}: expected {expected!r}, got {actual[cell_name]!r}"
                  for cell_name, expected in truth["cells"].items() if not cell_matches(actual[cell_name], expected)]
    return [check_result("reconciliation_values", "exact", not mismatches, "; ".join(mismatches), truth["cells"],
                         actual)]


def stated_numbers(text: str) -> list[float]:
    numbers = []
    for match in NUMBER_PATTERN.findall(text):
        try:
            numbers.append(parse_number(match))
        except ValueError:
            continue
    return numbers


def totals_stated(body: str, totals: dict[str, float]) -> CheckResult:
    numbers = stated_numbers(body)
    missing = [f"{status} {total:.2f}" for status, total in totals.items()
               if not any(abs(number - total) < HALF_CENT for number in numbers)]
    return check_result("summary_states_totals", "semantic", not missing, f"the summary leaves out {missing}", totals)


def summary_lists_discrepant_invoices(context: VerifierContext) -> list[CheckResult]:
    """Only mail the episode sent counts; with one app the task has no summary to check."""
    truth = context.ground_truth
    recipient = truth["summary_recipient"]
    if recipient is None:
        return []
    already_sent = {message["id"] for message in (context.initial_state.get("mail") or {}).get("sent", [])}
    summaries = [message for message in (context.final_state.get("mail") or {}).get("sent", [])
                 if message["id"] not in already_sent and recipient in message.get("to", [])]
    if not summaries:
        return [check_result("summary_sent", "semantic", False, f"no summary was sent to {recipient}", recipient)]
    body = "\n".join(message["body"] for message in summaries)
    checks = [entities_mentioned("summary_lists_discrepant_invoices", body, truth["discrepant_invoice_ids"])]
    if truth["summary_states_totals"]:
        checks.append(totals_stated(body, truth["totals"]))
    return checks


def oracle_script(task: GeneratedTask) -> str:
    """Python the desktop user runs: the workbook, then for two app tasks the summary in the sent folder."""
    truth = task.ground_truth
    lines = ["import json, pathlib", "import openpyxl",
             f"world = pathlib.Path.home() / {USER_WORLD_FOLDER!r}",
             "workbook = openpyxl.Workbook()",
             f"for cell_name, value in json.loads({json.dumps(truth['cells'])!r}).items():",
             "    workbook.active[cell_name] = value",
             f"workbook.save(world / 'files' / {truth['workbook']!r})"]
    if truth["summary_recipient"] is not None:
        body = (f"The {month_label(truth['month'])} reconciliation is done. Invoices whose export rows needed fixing: "
                f"{', '.join(truth['discrepant_invoice_ids']) or 'none'}.")
        if truth["summary_states_totals"]:
            body += f" Open total {truth['totals']['open']:.2f}, overdue total {truth['totals']['overdue']:.2f}."
        lines += ["from datetime import datetime, timezone",
                  "from email.message import EmailMessage",
                  "from email.utils import format_datetime",
                  "message = EmailMessage()",
                  f"message['From'] = {truth['persona_email']!r}",
                  f"message['To'] = {truth['summary_recipient']!r}",
                  f"message['Subject'] = {'Invoice reconciliation for ' + month_label(truth['month'])!r}",
                  "message['Date'] = format_datetime(datetime.now(timezone.utc))",
                  f"message.set_content({body!r})",
                  f"sent = world / 'mail' / {truth['persona_email']!r} / 'sent'",
                  "sent.mkdir(parents=True, exist_ok=True)",
                  f"(sent / {ORACLE_MESSAGE_ID + '.eml'!r}).write_bytes(message.as_bytes())"]
    return "\n".join(lines)


class ReconcileInvoices:
    name = TEMPLATE_NAME
    version = "1"
    oracle_name = ORACLE_NAME
    description = ("Total a month's open and overdue invoices from a damaged export into a workbook; with mail, "
                   "send the CFO a summary of the rows that needed fixing.")
    world_generators = frozenset({"worlds.mid_market_company"})

    def generate(self, world: World, difficulty: DifficultySpec, seed: int) -> GeneratedTask:
        if difficulty.apps > MAX_APPS:
            raise TaskGenerationError(f"{TEMPLATE_NAME} spans at most {MAX_APPS} apps, mail and files")
        random_generator = random.Random(seed)
        invoices_per_month = Counter(invoice.issued_on.strftime("%Y-%m") for invoice in world.invoices)
        busy_months = sorted(month for month, count in invoices_per_month.items() if count >= 2)
        months = busy_months or sorted(invoices_per_month)
        if not months:
            raise TaskGenerationError("the world has no invoices to reconcile")
        month = random_generator.choice(months)
        in_month = [invoice for invoice in world.invoices if invoice.issued_on.strftime("%Y-%m") == month]
        damaged = random_generator.sample(in_month, min(difficulty.corrupted_inputs, len(in_month)))
        perturbed, _ = apply_perturbations(world, difficulty.distractors, 0, seed)
        task_world = damaged_export(perturbed, {invoice.id for invoice in damaged}, random_generator)
        totals = {status: round(sum(invoice.amount for invoice in in_month if invoice.status == status), 2)
                  for status in TOTALED_STATUSES}
        cfo = next(person for person in world.people if person.role == CFO_ROLE)
        with_summary = difficulty.apps >= 2
        ground_truth = {
            "month": month,
            "workbook": WORKBOOK,
            "totals": totals,
            "cells": {"A1": "open", "B1": totals["open"], "A2": "overdue", "B2": totals["overdue"]},
            "discrepant_invoice_ids": sorted(invoice.id for invoice in damaged),
            "summary_recipient": cfo.email if with_summary else None,
            "summary_states_totals": with_summary and difficulty.dependency_depth >= 1,
            "persona_email": world.agent_persona.email,
        }
        instruction, hidden_intent = self.wording(difficulty, ground_truth, cfo.full_name)
        return GeneratedTask(instruction=instruction, world=task_world, ground_truth=ground_truth,
                             hidden_intent=hidden_intent)

    def wording(self, difficulty: DifficultySpec, truth: dict[str, Any], cfo_name: str) -> tuple[str, str]:
        """What the instruction leaves out grows with ambiguity; the hidden intent holds exactly that."""
        ambiguity, month = difficulty.ambiguity, month_label(truth["month"])
        files = f"~/{USER_WORLD_FOLDER}/files"
        scope = f"issued in {month}" if ambiguity == "none" else "for the period"
        instruction = (f"Reconcile the invoices {scope} using {files}/invoices.csv, where some amounts are damaged. "
                       f"In the first sheet of {files}/{truth['workbook']} put open in A1 with the open total in B1 "
                       "and overdue in A2 with the overdue total in B2.")
        hidden = [] if ambiguity == "none" else [f"Only invoices issued in {month} count."]
        if truth["summary_recipient"] is not None:
            recipients = {"none": f"{cfo_name} ({truth['summary_recipient']})", "low": f"{cfo_name}",
                          "medium": "the CFO", "high": "finance leadership"}
            totals_clause = " and both totals" if truth["summary_states_totals"] else ""
            instruction += (f" Then email {recipients[ambiguity]} a summary listing every invoice whose row needed "
                            f"fixing{totals_clause}.")
            if ambiguity in ("medium", "high"):
                hidden.append(f"The summary goes only to {cfo_name}, the CFO, at {truth['summary_recipient']}.")
        return instruction, " ".join(hidden)

    def oracle(self, task: GeneratedTask) -> list[ActionModel]:
        return [ExecAction(command=f"{GUEST_PYTHON} - <<'RLWORLD_ORACLE'\n{oracle_script(task)}\nRLWORLD_ORACLE",
                           timeout_s=120),
                DoneAction()]

    def verifiers(self) -> Sequence[Verifier]:
        return (FunctionVerifier(VALUES_VERIFIER, self.version, "exact", reconciliation_values_match),
                FunctionVerifier(SUMMARY_VERIFIER, self.version, "semantic", summary_lists_discrepant_invoices))
