import threading
from dataclasses import replace

from support.authoring_fakes import (
    RECORDED_ACTIONS,
    SHAPES,
    SNAPSHOT,
    OracleAgent,
    authoring_bench,
    generated_task,
    record_alternative,
    record_near_miss,
)

from rlworld.actions.models import DoneAction, ExecAction, action_payload
from rlworld.images.linux import GUEST_PYTHON
from rlworld.preflight.preflight import IssueCollector, PreflightContext, check_gates
from rlworld.services.catalogs import CatalogEntry, CatalogService
from rlworld.services.gate_reports import GateRecipe, GateRecord, GateReports
from rlworld.services.gate_reruns import GateReruns, newest_snapshots
from rlworld.specs import spec_digest
from rlworld.tasks.finance import ReconcileInvoices, oracle_script
from rlworld.tasks.gates import GateReport, GateResult
from rlworld.tasks.registry import default_task_registry

IMAGE = "noble_calc_v1"
POOL = "linux_dev"
REBUILT = "sha256:" + "11" * 32
NEW_VERSION = "sha256:" + "22" * 32
FAILED_BEFORE = GateReport("sha256:task", (GateResult("oracle", False, "the oracle does not pass"),))


def reruns_for(bench, reports, tmp_path, catalog=None):
    return GateReruns(bench.experiments, default_task_registry(), reports, bench.fleet, bench.review, SHAPES,
                      catalog_service=catalog, solvability=OracleAgent(tmp_path / "agent"), clock=float)


def authored_record(bench, reports):
    """A task authored with GUI recordings, whose gates passed and became its report."""
    session_id = bench.service.start(bench.digest, "ada", "finance_lab").session_id
    record_alternative(bench, session_id)
    record_near_miss(bench, session_id)
    assert bench.service.run_gates(session_id).gate_status == "passed"
    return reports.latest()[0]


def scripted_record(bench, reports):
    """A task whose recipe replays exec solutions: the oracle as an alternative, the right total in C1 as a miss."""
    session_id = bench.service.start(bench.digest, "ada", "finance_lab").session_id
    task = generated_task(bench, session_id)
    moved = (f"{oracle_script(task)}\nworkbook.active['C1'] = workbook.active['B1'].value\n"
             "workbook.active['B1'] = None\nworkbook.save(world / 'files' / 'reconciliation.xlsx')")
    near_miss = [action_payload(ExecAction(command=f"{GUEST_PYTHON} - <<'MISS'\n{moved}\nMISS")),
                 action_payload(DoneAction())]
    document = bench.experiments.spec(bench.digest)[1]
    recipe = GateRecipe(spec_digest=bench.digest, task_index=0,
                        alternatives=[[action_payload(action) for action in ReconcileInvoices().oracle(task)]],
                        near_misses=[near_miss])
    record = GateRecord(task_spec_digest=spec_digest(document.task), template=document.task.generator,
                        task_id="task_before", image=IMAGE, image_digest=spec_digest(document.image), pool=POOL,
                        snapshot=SNAPSHOT.digest, report=FAILED_BEFORE, recipe=recipe, ran_at=0.0,
                        source="authoring")
    reports.record(record)
    return record


def test_authored_gates_become_the_tasks_report_for_preflight(example_payload, tmp_path):
    reports = GateReports()
    bench = authoring_bench(example_payload, tmp_path, solvability=OracleAgent(tmp_path / "agent"),
                            gate_reports=reports)
    document = bench.experiments.spec(bench.digest)[1]
    collector = IssueCollector()
    check_gates(document, PreflightContext(gate_reports=reports), collector)
    assert [issue.code for issue in collector.issues] == ["ungated_task"]
    record = authored_record(bench, reports)
    assert (record.task_spec_digest, record.source, record.image, record.pool, record.snapshot) == (
        spec_digest(document.task), "authoring", IMAGE, POOL, SNAPSHOT.digest)
    assert record.image_digest == spec_digest(document.image) and record.report.passed
    assert record.recipe == GateRecipe(spec_digest=bench.digest, task_index=0, oracle=None,
                                       alternatives=[RECORDED_ACTIONS], near_misses=[RECORDED_ACTIONS])
    assert reports[record.task_spec_digest] is record.report and list(reports) == [record.task_spec_digest]
    assert len(reports) == 1 and reports.get("sha256:other") is None
    collector = IssueCollector()
    check_gates(document, PreflightContext(gate_reports=reports), collector)
    assert collector.issues == []


def test_an_image_bump_reruns_the_gates_on_the_new_snapshot(example_payload, tmp_path):
    reports = GateReports()
    bench = authoring_bench(example_payload, tmp_path, solvability=OracleAgent(tmp_path / "author_agent"),
                            gate_reports=reports)
    record = authored_record(bench, reports)
    reruns = reruns_for(bench, reports, tmp_path)
    assert reruns.poll() == []
    bench.fleet.publish_snapshot(IMAGE, record.image_digest, POOL, SNAPSHOT.digest, "2026-10-03T12:00:00Z")
    assert reruns.poll() == []
    leases_before = len(bench.fleet.requests)
    bench.fleet.publish_snapshot(IMAGE, record.image_digest, POOL, REBUILT, "2026-10-04T09:30:00.123456789Z")
    rerun = reruns.poll()
    assert [updated.snapshot for updated in rerun] == [REBUILT]
    updated = reports.latest()[0]
    assert (updated.source, updated.snapshot, updated.recipe) == ("image_bump", REBUILT, record.recipe)
    rerun_leases = bench.fleet.requests[leases_before:]
    assert {(request.run_id, request.priority, request.endpoint, request.snapshot.digest)
            for request in rerun_leases} == {("gate_rerun_000001", "eval", "gates", REBUILT)}
    assert len(rerun_leases) == 3 + 1 + 1
    results = {result.name: result for result in updated.report.results}
    assert results["oracle"].passed and results["near_miss"].passed and results["solvability"].passed
    assert not results["alternative_solution"].passed and not updated.report.passed
    item = bench.review.list_items(queue="review")[0]
    assert (item.action, item.source, item.created_by) == ("gate_failure", "gates", "gate_reruns")
    assert item.title == f"tasks.finance.reconcile_invoices task {updated.task_id} fails its gates on {IMAGE}"
    assert item.payload["previous_snapshot"] == SNAPSHOT.digest and "alternative_solution" in item.note
    assert reruns.poll() == []


def test_reruns_replay_recorded_solutions_and_pass_while_they_still_work(example_payload, tmp_path):
    reports = GateReports()
    bench = authoring_bench(example_payload, tmp_path, gate_reports=reports)
    record = scripted_record(bench, reports)
    bench.fleet.publish_snapshot(IMAGE, record.image_digest, POOL, REBUILT, "2026-10-04T09:30:00Z")
    updated = reruns_for(bench, reports, tmp_path).poll()[0]
    assert updated.report.passed, updated.report.review_reasons
    assert updated.task_id != "task_before" and reports[record.task_spec_digest].passed
    alternative = next(result for result in updated.report.results if result.name == "alternative_solution")
    assert [attempt.name for attempt in alternative.attempts] == ["alternative_0"]
    assert bench.review.list_items() == []


def test_a_new_image_version_reruns_the_task_on_that_image(example_payload, tmp_path):
    reports = GateReports()
    bench = authoring_bench(example_payload, tmp_path, gate_reports=reports)
    record = scripted_record(bench, reports)
    document = bench.experiments.spec(bench.digest)[1]
    new_image = document.image.model_copy(update={"display": document.image.display.model_copy(
        update={"width": 1920, "height": 1080})})
    bench.fleet.publish_snapshot(IMAGE, record.image_digest, POOL, SNAPSHOT.digest, "2026-10-03T12:00:00Z")
    bench.fleet.publish_snapshot(IMAGE, spec_digest(new_image), POOL, NEW_VERSION, "2026-10-05T08:00:00Z",
                                 image_spec=new_image.model_dump(mode="json"))
    bench.fleet.publish_snapshot("windows_excel_v2", "sha256:" + "33" * 32, "windows", "sha256:" + "44" * 32,
                                 "2026-10-06T08:00:00Z")
    leases_before = len(bench.fleet.requests)
    updated = reruns_for(bench, reports, tmp_path).poll()
    assert [(item.image_digest, item.snapshot) for item in updated] == [(spec_digest(new_image), NEW_VERSION)]
    assert {request.snapshot.digest for request in bench.fleet.requests[leases_before:]} == {NEW_VERSION}
    assert updated[0].task_spec_digest == record.task_spec_digest


def test_reruns_that_cannot_run_go_to_review_once(example_payload, tmp_path):
    reports = GateReports()
    bench = authoring_bench(example_payload, tmp_path, gate_reports=reports)
    record = scripted_record(bench, reports)
    broken = replace(record, recipe=record.recipe.model_copy(update={"spec_digest": "sha256:gone"}))
    reports.record(broken)
    bench.fleet.publish_snapshot(IMAGE, record.image_digest, POOL, REBUILT, "2026-10-04T09:30:00Z")
    reruns = reruns_for(bench, reports, tmp_path)
    assert reruns.poll() == [] and reruns.poll() == []
    items = bench.review.list_items()
    assert len(items) == 1 and items[0].title == f"tasks.finance.reconcile_invoices gates could not rerun on {IMAGE}"
    assert items[0].note.startswith("UnknownRecordError") and items[0].payload["snapshot"] == REBUILT
    assert reports.latest()[0] is broken


def test_the_images_catalog_follows_the_snapshot_index(example_payload, tmp_path):
    reports = GateReports()
    bench = authoring_bench(example_payload, tmp_path, gate_reports=reports)
    catalog = CatalogService(bench.experiments.documents)
    catalog.publish(CatalogEntry(kind="images", name=IMAGE, version="v1", description="Ubuntu noble with Calc"))
    bench.fleet.publish_snapshot(IMAGE, "sha256:" + "aa" * 32, POOL, REBUILT, "2026-10-04T09:30:00Z")
    bench.fleet.publish_snapshot(IMAGE, "sha256:" + "aa" * 32, "linux_gpu", NEW_VERSION, "2026-10-04T10:00:00Z")
    bench.fleet.publish_snapshot("windows_excel_v2", "sha256:" + "33" * 32, "windows", "sha256:" + "44" * 32,
                                 "2026-10-06T08:00:00Z")
    reruns_for(bench, reports, tmp_path, catalog).poll()
    entries = {entry.name: entry for entry in catalog.entries("images")}
    noble = entries[IMAGE]
    assert noble.description == "Ubuntu noble with Calc" and noble.version == "sha256:" + "aa" * 32
    assert noble.details["snapshots"] == {"linux_dev": REBUILT, "linux_gpu": NEW_VERSION}
    assert entries["windows_excel_v2"].details["snapshots"] == {"windows": "sha256:" + "44" * 32}


def test_the_newest_snapshot_of_an_image_wins_per_pool():
    currents = [
        {"image": IMAGE, "image_digest": "sha256:old", "pool": POOL, "snapshot": "a",
         "updated_at": "2026-10-04T09:30:00.900000001Z"},
        {"image": IMAGE, "image_digest": "sha256:new", "pool": POOL, "snapshot": "b",
         "updated_at": "2026-10-04T09:30:01Z"},
        {"image": IMAGE, "image_digest": "sha256:old", "pool": "linux_gpu", "snapshot": "c",
         "updated_at": "2026-10-01T00:00:00Z"},
    ]
    newest = newest_snapshots(currents)
    assert {key: current["snapshot"] for key, current in newest.items()} == {(IMAGE, POOL): "b",
                                                                           (IMAGE, "linux_gpu"): "c"}


class StoppingIndex:
    """A snapshot index that asks the watcher to stop once it has been polled."""

    def __init__(self, stop):
        self.stop = stop
        self.polls = 0

    def current_snapshots(self):
        self.polls += 1
        self.stop.set()
        return []


def test_the_watcher_polls_until_stopped(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path)
    stop = threading.Event()
    index = StoppingIndex(stop)
    watcher = GateReruns(bench.experiments, default_task_registry(), GateReports(), index, bench.review, SHAPES)
    watcher.watch(stop, 0.001)
    assert index.polls == 1
