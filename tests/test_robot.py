"""Tests for the reasoning stage: ROBOT under the reasoner limits, and how each failure is reported (A-3, A-6, O-8)."""

import asyncio
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import browl.imports
import browl.loading
import browl.process
import browl.robot
from browl.imports import imports_stage, write_catalog
from browl.jobs import JobContext, Reasoner, Stage, StageError
from browl.ofn import Listing
from browl.process import TAIL_LINES, Completed
from browl.robot import (
    OUT_OF_MEMORY,
    PARSE_ERROR,
    REASONED,
    UNSATISFIABLE_SHOWN,
    RobotOutput,
    failure_reason,
    reason_args,
    reasoning_stage,
    run_robot,
)
from browl.settings import Settings
from tests.conftest import DATA, find_robot_jar
from tests.helpers import StaticServer, make_app, make_settings

pytestmark = pytest.mark.anyio

REASONING = DATA / "reasoning"
NS = "http://example.org/s/"
OUTPUT_ARG = 'while [ $# -gt 0 ]; do [ "$1" = --output ] && out=$2; shift; done'
"""Shell that sets `out` to the fake ROBOT's `--output` argument."""


def output_of(*lines: str) -> RobotOutput:
    """Return what's kept of the output lines."""
    output = RobotOutput()
    for line in lines:
        output.add(line)
    return output


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """Return test settings with the tests' robot.jar."""
    return make_settings(ROBOT_JAR=str(find_robot_jar()))


def upload(data_dir: Path, fixture: str) -> Path:
    """Copy a fixture to where the uploading stage would have stored it."""
    path = data_dir / "uploads" / "1" / f"ontology{Path(fixture).suffix}"
    path.parent.mkdir(parents=True)
    path.write_bytes((REASONING / fixture).read_bytes())
    return path


def job(settings: Settings, reasoner: Reasoner, path: Path) -> JobContext:
    """Return a job's context for an upload, with the empty catalog the imports stage writes for one without imports."""
    catalog = path.with_name(browl.imports.CATALOG)
    write_catalog(catalog, {})
    return JobContext(1, settings, reasoner, path, catalog=catalog)


def stored_files(context: JobContext) -> list[str]:
    """Return the names of the files beside the upload, but the catalog."""
    assert context.upload is not None
    return sorted(path.name for path in context.upload.parent.iterdir() if path.name != browl.imports.CATALOG)


async def reason(settings: Settings, data_dir: Path, fixture: str, reasoner: Reasoner = Reasoner.ELK) -> JobContext:
    """Run the reasoning stage on a fixture, returning the job's context."""
    context = job(settings, reasoner, upload(data_dir, fixture))
    await reasoning_stage(context)
    return context


async def axioms(ontology: Path, namespace: str, settings: Settings, tmp_path: Path) -> set[str]:
    """Return an ontology's SubClassOf and EquivalentClasses axioms, but those with owl:Thing, minus the namespace."""
    ofn = tmp_path / "axioms.ofn"
    await run_robot(["convert", "--input", str(ontology), "--output", str(ofn)], settings)
    return {
        line.replace(f"<{namespace}", "").replace(">", "")
        for line in ofn.read_text().splitlines()
        if line.startswith(("SubClassOf", "EquivalentClasses")) and "owl:Thing" not in line
    }


def pigeonhole(path: Path, holes: int) -> Path:
    """Write n+1 pigeons in n holes, which takes HermiT exponential time in n (15 s at 9)."""
    pigeons = range(holes + 1)
    axioms = [f"SubClassOf(:Pigeons ObjectUnionOf({' '.join(f':P{i}_{j}' for j in range(holes))}))" for i in pigeons]
    axioms += [f"DisjointClasses(:P{i}_{j} :P{k}_{j})" for j in range(holes) for i in pigeons for k in pigeons if i < k]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"Prefix(:=<{NS}>)\nOntology(<{NS}php>\n" + "\n".join(axioms) + "\n)\n")
    return path


def fake_java(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Make `JAVA_HOME/bin/java` a shell script, to test the process handling without a JVM."""
    java = tmp_path / "fake-java" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text(f"#!/bin/sh\n{script}\n")
    java.chmod(java.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("JAVA_HOME", str(java.parent.parent))


def running_in_group(pgid: int) -> list[str]:
    """Return the `ps` lines of the processes still running in the process group, ignoring zombies.

    A zombie has exited, but stays in its group until its parent reaps it: here a child of the killed shell, which is
    reparented to a process that may never reap it (as on GitHub's runners), so `os.killpg(pgid, 0)` still succeeds.
    """
    ps = subprocess.run(["ps", "-A", "-o", "pgid=,stat=,pid=,command="], check=True, capture_output=True, text=True)
    return [line for line in ps.stdout.splitlines() if int(line.split()[0]) == pgid and line.split()[1][0] != "Z"]


@pytest.mark.parametrize(
    ("status", "lines", "timed_out", "expected"),
    [
        (-15, [OUT_OF_MEMORY], True, "time limit: reasoning took longer than REASONER_TIMEOUT_S (3600 s)"),
        (3, ["Terminating due to java.lang.OutOfMemoryError: Java heap space"], False, "memory limit: "),
        (1, [OUT_OF_MEMORY], False, "reasoner failed (ROBOT exit status 1)"),  # needs status 3 too
        (1, ['Exception in thread "main" java.lang.StackOverflowError'], False, "stack overflow"),
        (1, ["ERROR org.obolibrary.robot.ReasonerHelper - The ontology is inconsistent"], False, "inconsistent"),
        (1, [f"{PARSE_ERROR} Could not load a valid ontology"], False, "parse error: ROBOT couldn't read"),
        (1, ["Use the -vvv option to show the stack trace."], False, "reasoner failed (ROBOT exit status 1):\nUse"),
    ],
)
def test_failure_reason(status: int, lines: list[str], timed_out: bool, expected: str, data_dir: Path) -> None:
    """Check that each kind of failure is told apart by S-2's signatures, in S-2's order."""
    completed = Completed(status, timed_out, tail=tuple(lines))
    assert failure_reason(completed, output_of(*lines), make_settings()).startswith(expected)


def test_unsatisfiable_reason(data_dir: Path) -> None:
    """Check that an unsatisfiable-classes failure names the first classes and counts the rest."""
    lines = [f"ERROR ReasonerHelper - There are {UNSATISFIABLE_SHOWN + 2} unsatisfiable classes in the ontology."]
    lines += [f"ERROR ReasonerHelper -     unsatisfiable: {NS}C{i}" for i in range(UNSATISFIABLE_SHOWN + 2)]
    reason = failure_reason(Completed(1, tail=tuple(lines)), output_of(*lines), make_settings())
    shown = ", ".join(f"{NS}C{i}" for i in range(UNSATISFIABLE_SHOWN))
    assert reason == f"{UNSATISFIABLE_SHOWN + 2} unsatisfiable classes: {shown} and 2 more"


async def test_tail() -> None:
    """Check that only a process's last lines are kept, and that its stdout, read as chunks, isn't among them."""
    chunks: list[bytes] = []
    script = f'echo out; i=0; while [ $i -lt {TAIL_LINES + 5} ]; do echo "line $i" >&2; i=$((i+1)); done'
    completed = await browl.process.run_process(["sh", "-c", script], timeout_s=10, on_stdout=chunks.append)
    assert b"".join(chunks) == b"out\n"
    assert len(completed.tail) == TAIL_LINES
    assert completed.tail[0] == "line 5"
    assert completed.output.endswith(f"line {TAIL_LINES + 4}")


async def test_tool_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a tool gets only the app's `ENVIRONMENT` variables, and those the caller adds."""
    monkeypatch.setenv("BROWL_SECRET", "s3cret")
    monkeypatch.setenv("LANG", "C")
    lines: list[str] = []
    command = ["sh", "-c", 'echo "secret=$BROWL_SECRET lang=$LANG added=$ADDED"']
    completed = await browl.process.run_process(command, lines.append, timeout_s=10, env={"ADDED": "yes"})
    assert completed.ok
    assert lines == ["secret= lang=C added=yes"]


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_robot_fetches_nothing(site: StaticServer, settings: Settings, data_dir: Path) -> None:
    """Check that ROBOT fails on an import missing from the catalog, naming it, and never requests it."""
    url, requested = site.url, site.requested
    path = data_dir / "uploads" / "1" / "ontology.ttl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"<http://example.org/o> a owl:Ontology ; owl:imports <{url}/missing> .\n"
    )
    context = job(settings, Reasoner.ELK, path)
    with pytest.raises(StageError, match=f"Could not load imported ontology: <{url}/missing>"):
        await reasoning_stage(context)
    assert requested == []


async def test_long_lines_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Check that a long output line is cut short."""
    fake_java(tmp_path, monkeypatch, "printf '%010000d\n'; exit 1")
    with pytest.raises(StageError) as raised:
        await run_robot([], settings)
    assert str(raised.value).endswith("\n" + "0" * browl.process.MAX_LINE_CHARS)


def test_reason_args(data_dir: Path) -> None:
    """Check that materialize runs only when properties are configured, and the shortcuts query always."""
    source, out = Path("in.ofn"), Path("out.ttl")
    plain = reason_args(Reasoner.HERMIT, source, out, make_settings(), Path("catalog.xml"))
    assert plain[:7] == ["reason", "--catalog", "catalog.xml", "--input", "in.ofn", "--reasoner", "HermiT"]
    assert "materialize" not in plain
    assert plain[-5:] == ["query", "--update", str(browl.robot.SHORTCUTS_QUERY), "--output", "out.ttl"]
    properties = [f"{NS}part_of", f"{NS}has_part"]
    settings = make_settings(MATERIALIZE_PROPERTIES=properties)
    materialized = reason_args(Reasoner.ELK, source, out, settings, Path("catalog.xml"))
    start = materialized.index("materialize")
    assert materialized[start : start + 7] == [
        "materialize",
        "--reasoner",
        "ELK",
        "--term",
        properties[0],
        "--term",
        properties[1],
    ]


async def test_no_upload_skips(data_dir: Path) -> None:
    """Check that a job with a reasoner but nothing uploaded (`JobRunner.start`) skips reasoning."""
    context = JobContext(1, make_settings(ROBOT_JAR="/nonexistent"), Reasoner.ELK)
    await reasoning_stage(context)
    assert context.reasoned is context.ontology is None


async def test_none_skips(data_dir: Path) -> None:
    """Check that with no reasoner chosen the stage leaves an RDF upload alone, as the ontology served."""
    path = data_dir / "uploads" / "1" / "ontology.ttl"
    path.parent.mkdir(parents=True)
    path.write_text("<http://example.org/a> a <http://example.org/B> .\n")
    context = job(make_settings(ROBOT_JAR="/nonexistent"), Reasoner.NONE, path)
    await reasoning_stage(context)
    assert context.reasoned is None
    assert context.ontology == context.upload


async def test_output_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Check that output is read whole, however long its lines and whether or not it ends in a newline."""
    fake_java(
        tmp_path,
        monkeypatch,
        f"printf 'start\\n%0100000d\\nThere are 1 unsatisfiable classes\\nunsatisfiable: {NS}A'; exit 1",
    )
    with pytest.raises(StageError, match=f"^1 unsatisfiable classes: {NS}A$"):
        await run_robot([], settings)


async def test_output_split_characters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Check that a multi-byte character split between reads is decoded whole."""
    fake_java(tmp_path, monkeypatch, f"printf 'There are 1 unsatisfiable classes\\nunsatisfiable: {NS}Bär→'; exit 1")
    monkeypatch.setattr(browl.process, "READ_BYTES", 1)
    with pytest.raises(StageError, match=f"^1 unsatisfiable classes: {NS}Bär→$"):
        await run_robot([], settings)


async def test_stops_group_on_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Check that a cancelled run stops ROBOT's whole process group, with SIGKILL if SIGTERM is ignored."""
    pid_file = tmp_path / "pid"
    fake_java(tmp_path, monkeypatch, f"trap '' TERM; sleep 60 & echo $$ > {pid_file}; wait")
    monkeypatch.setattr(browl.process, "TERM_GRACE_S", 0.2)
    task = asyncio.create_task(run_robot([], settings))
    while not pid_file.exists() or not pid_file.read_text():
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert running_in_group(int(pid_file.read_text())) == []


async def test_stops_group_on_second_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Check that a run cancelled again while its process group is being stopped still waits for it to stop."""
    pid_file = tmp_path / "pid"
    fake_java(tmp_path, monkeypatch, f"trap '' TERM; sleep 60 & echo $$ > {pid_file}; wait")
    monkeypatch.setattr(browl.process, "TERM_GRACE_S", 1.0)
    task = asyncio.create_task(run_robot([], settings))
    while not pid_file.exists() or not pid_file.read_text():
        await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.2)  # SIGTERM sent and ignored
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert running_in_group(int(pid_file.read_text())) == []


async def test_failed_run_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings, data_dir: Path
) -> None:
    """Check that a run that fails after writing its output leaves neither a partial nor a reasoned file."""
    fake_java(tmp_path, monkeypatch, f'{OUTPUT_ARG}; echo x > "$out"; echo boom; exit 1')
    context = job(settings, Reasoner.ELK, upload(data_dir, "ok.ofn"))
    with pytest.raises(StageError, match=r"^reasoner failed \(ROBOT exit status 1\):\nboom$"):
        await reasoning_stage(context)
    assert context.reasoned is None
    assert stored_files(context) == ["ontology.ofn"]


async def test_time_limit_stops_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """Check that a run past the time limit is stopped, its whole process group too, and leaves no output."""
    pid_file = tmp_path / "pid"
    fake_java(
        tmp_path, monkeypatch, f"{OUTPUT_ARG}; echo x > \"$out\"; trap '' TERM; sleep 60 & echo $$ > {pid_file}; wait"
    )
    monkeypatch.setattr(browl.process, "TERM_GRACE_S", 0.2)
    settings = make_settings(REASONER_TIMEOUT_S=1)
    context = job(settings, Reasoner.ELK, upload(data_dir, "ok.ofn"))
    with pytest.raises(StageError, match=r"^time limit: "):
        await reasoning_stage(context)
    assert running_in_group(int(pid_file.read_text())) == []
    assert stored_files(context) == ["ontology.ofn"]


async def test_stops_group_after_robot_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """Check that a process ROBOT started is stopped, even once ROBOT itself has exited."""
    pid_file = tmp_path / "pid"
    fake_java(tmp_path, monkeypatch, f"echo $$ > {pid_file}; sleep 60 &")  # the sleep holds the output open
    with pytest.raises(StageError, match=r"^time limit: "):
        await run_robot([], make_settings(REASONER_TIMEOUT_S=1))
    assert running_in_group(int(pid_file.read_text())) == []


async def test_low_space_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """Check that ROBOT is stopped, leaving no output, once the data volume's free space falls below the margin."""
    pid_file = tmp_path / "pid"
    fake_java(tmp_path, monkeypatch, f'{OUTPUT_ARG}; echo x > "$out"; sleep 60 & echo $$ > {pid_file}; wait')
    margin = browl.process.FREE_SPACE_MARGIN_BYTES
    # Space runs low once the fake ROBOT has started.
    monkeypatch.setattr(
        browl.process, "free_bytes", lambda path: margin - bool(pid_file.exists() and pid_file.read_text())
    )
    monkeypatch.setattr(browl.process, "SPACE_CHECK_S", 0.05)
    context = job(make_settings(), Reasoner.ELK, upload(data_dir, "ok.ofn"))
    with pytest.raises(
        StageError, match=r"^disk space: a volume fell below 1,000,000,000 bytes free while reasoning ran$"
    ):
        await reasoning_stage(context)
    assert running_in_group(int(pid_file.read_text())) == []
    assert stored_files(context) == ["ontology.ofn"]


def test_free_bytes(tmp_path: Path) -> None:
    """Check that the free space reported is the file system's."""
    assert 0 < browl.process.free_bytes(tmp_path) <= shutil.disk_usage(tmp_path).total


async def test_no_catalog_fails(data_dir: Path) -> None:
    """Check that ROBOT never runs without the catalog, which keeps it from resolving imports itself."""
    context = JobContext(1, make_settings(), Reasoner.ELK, upload(data_dir, "ok.ofn"))
    with pytest.raises(StageError, match="no catalog"):
        await reasoning_stage(context)


@pytest.mark.parametrize(
    ("script", "stage", "reason"),
    [
        (f'{OUTPUT_ARG}; echo x > "$out"', Stage.READY, None),
        ("echo boom; exit 2", Stage.REASONING, "reasoner failed (ROBOT exit status 2):\nboom"),
    ],
)
def test_app_runs_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_dir: Path, script: str, stage: Stage, reason: str | None
) -> None:
    """Check that the app's uploads run the reasoning stage, and a failure is recorded with ROBOT's output (A-6)."""
    fake_java(tmp_path, monkeypatch, script)

    async def no_imports(*args: object) -> Listing:
        return Listing([])

    async def no_loading(*args: object, **kwargs: object) -> None:
        pass

    # The other stages run Jena's tools, which would run the fake Java too.
    monkeypatch.setattr(browl.imports, "list_imports", no_imports)
    monkeypatch.setattr(browl.loading, "loading_stage", no_loading)
    app = make_app(real_stages=True)
    with TestClient(app) as client:
        files = {"ontology": ("o.ttl", b"@prefix ex: <http://example.org/> .\nex:a a ex:B .\n")}
        response = client.post("/_browl/admin/upload", data={"reasoner": "elk"}, files=files, follow_redirects=False)
        assert response.status_code == 303
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
    state = app.state.jobs.state
    assert (state.stage, state.reason) == (stage, reason)
    assert (data_dir / "uploads" / "1" / REASONED).exists() is (reason is None)


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
@pytest.mark.parametrize("reasoner", [Reasoner.ELK, Reasoner.HERMIT])
async def test_success(settings: Settings, data_dir: Path, tmp_path: Path, reasoner: Reasoner) -> None:
    """Check that a reasoned ontology replaces the upload as the one served, with inferred parents."""
    context = await reason(settings, data_dir, "ok.ofn", reasoner)
    assert context.reasoned == context.ontology == context.upload.with_name(REASONED)
    assert context.upload.with_name(browl.robot.PARTIAL).exists() is False
    assert "SubClassOf(Dog Pet)" in await axioms(context.reasoned, "http://example.org/s2/", settings, tmp_path)


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_materialize_and_shortcuts(settings: Settings, data_dir: Path, tmp_path: Path) -> None:
    """Check reasoning's result: materialized restrictions added, shortcuts removed, all else and equivalences kept."""
    settings = make_settings(ROBOT_JAR=str(find_robot_jar()), MATERIALIZE_PROPERTIES=[f"{NS}part_of"])
    context = await reason(settings, data_dir, "shortcuts.ofn")
    some = "SubClassOf({} ObjectSomeValuesFrom({} {}))".format
    assert await axioms(context.reasoned, NS, settings, tmp_path) == {
        "SubClassOf(A B)",
        "SubClassOf(B C)",
        "SubClassOf(D E)",
        some("E", "part_of", "F"),
        some("G", "part_of", "H"),
        some("H", "part_of", "I"),
        "SubClassOf(J L)",
        some("J", "part_of", "K"),
        "SubClassOf(K L)",
        some("M", "has_part", "N"),
        some("M", "has_part", "O"),
        some("N", "has_part", "O"),
        some("T", "part_of", "T"),
        some("X", "part_of", "T"),
        "EquivalentClasses(Q ObjectIntersectionOf(P ObjectSomeValuesFrom(part_of R)))",
        "SubClassOf(Q P)",
        some("Q", "part_of", "R"),
        "EquivalentClasses(U V)",
        some("Cyc1", "part_of", "Cyc2"),
        some("Cyc2", "part_of", "Cyc1"),
        some("Cyc1", "part_of", "CycT"),
        some("Cyc2", "part_of", "CycT"),
        some("Loop", "part_of", "Loop"),
        some("Loop", "part_of", "LoopT"),
        "SubClassOf(Ch1 Ch2)",
        "SubClassOf(Ch2 Ch3)",
        "SubClassOf(Ch3 Ch4)",
        "SubClassOf(D2 E2)",
        some("E2", "part_of", "F"),
        some("D2", "part_of", "F2"),
        "SubClassOf(D3 E3)",
        some("E3", "part_of", "F"),
        some("D3", "has_part", "F"),
        some("G2", "part_of", "H2"),
        some("H2", "part_of", "I2"),
        some("G2", "part_of", "I3"),
        some("G3", "part_of", "H3"),
        some("H3", "has_part", "I4"),
        some("G3", "part_of", "I4"),
    }


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
@pytest.mark.parametrize(
    ("fixture", "reasoner", "expected"),
    [
        ("inconsistent.ofn", Reasoner.ELK, "inconsistent ontology"),
        ("inconsistent.ofn", Reasoner.HERMIT, "inconsistent ontology"),
        ("unsatisfiable.ofn", Reasoner.ELK, "1 unsatisfiable classes: http://example.org/s2/Chimera"),
        ("unsatisfiable.ofn", Reasoner.HERMIT, "1 unsatisfiable classes: http://example.org/s2/Chimera"),
        ("parse-error.ttl", Reasoner.ELK, "parse error: ROBOT couldn't read the ontology:\n"),
    ],
)
async def test_logical_failures(
    settings: Settings, data_dir: Path, fixture: str, reasoner: Reasoner, expected: str
) -> None:
    """Check that an inconsistent or incoherent ontology, or one ROBOT can't parse, fails saying so, with no output."""
    with pytest.raises(StageError) as raised:
        await reason(settings, data_dir, fixture, reasoner)
    assert str(raised.value).startswith(expected)
    names = sorted(p.name for p in (data_dir / "uploads" / "1").iterdir())
    assert names == [browl.imports.CATALOG, f"ontology{Path(fixture).suffix}"]


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_reasons_over_converted(settings: Settings, data_dir: Path, tmp_path: Path) -> None:
    """Check that an OWL Functional Syntax upload is reasoned over as the Turtle the imports stage converted it to."""
    context = JobContext(1, settings, Reasoner.ELK, upload(data_dir, "ok.ofn"))
    await imports_stage(context)
    assert context.converted is not None
    context.upload.unlink()  # only the converted file is read from here on
    await reasoning_stage(context)
    assert context.reasoned == context.ontology
    assert "SubClassOf(Dog Pet)" in await axioms(context.reasoned, "http://example.org/s2/", settings, tmp_path)


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_time_limit(data_dir: Path) -> None:
    """Check that a run past REASONER_TIMEOUT_S is killed, and the job records the time limit."""
    settings = make_settings(ROBOT_JAR=str(find_robot_jar()), REASONER_TIMEOUT_S=1)
    context = job(settings, Reasoner.HERMIT, pigeonhole(data_dir / "uploads" / "1" / "ontology.ofn", 9))
    started = asyncio.get_running_loop().time()
    with pytest.raises(StageError, match=r"^time limit: reasoning took longer than REASONER_TIMEOUT_S \(1 s\)$"):
        await reasoning_stage(context)
    assert asyncio.get_running_loop().time() - started < 5


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_memory_limit(data_dir: Path) -> None:
    """Check that a heap breach ends ROBOT, and the job records the memory limit."""
    settings = make_settings(ROBOT_JAR=str(find_robot_jar()), REASONER_MAX_HEAP="8m")
    with pytest.raises(StageError, match=r"^memory limit: reasoning needed more than REASONER_MAX_HEAP \(8m\)$"):
        await reason(settings, data_dir, "ok.ofn")
