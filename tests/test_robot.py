"""Tests for the reasoning stage: ROBOT under the reasoner limits, and how each failure is reported (A-3, A-6, O-8)."""

import asyncio
import os
import stat
from pathlib import Path

import pytest

import browl.robot
from browl.jobs import JobContext, Reasoner, StageError
from browl.robot import (
    OUT_OF_MEMORY,
    PARSE_ERROR,
    REASONED,
    TAIL_LINES,
    UNSATISFIABLE_SHOWN,
    RobotOutput,
    failure_reason,
    reason_args,
    reasoning_stage,
    run_robot,
)
from browl.settings import Settings
from tests.conftest import DATA, find_robot_jar
from tests.helpers import make_settings

pytestmark = pytest.mark.anyio

REASONING = DATA / "reasoning"
NS = "http://example.org/s/"


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


async def reason(settings: Settings, data_dir: Path, fixture: str, reasoner: Reasoner = Reasoner.ELK) -> JobContext:
    """Run the reasoning stage on a fixture, returning the job's context."""
    context = JobContext(1, settings, reasoner, upload(data_dir, fixture))
    await reasoning_stage(context)
    return context


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


def gone(pid: int) -> bool:
    """Return whether no process is left in the process group."""
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


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
    assert failure_reason(status, output_of(*lines), timed_out, make_settings()).startswith(expected)


def test_unsatisfiable_reason(data_dir: Path) -> None:
    """Check that an unsatisfiable-classes failure names the first classes and counts the rest."""
    lines = [f"ERROR ReasonerHelper - There are {UNSATISFIABLE_SHOWN + 2} unsatisfiable classes in the ontology."]
    lines += [f"ERROR ReasonerHelper -     unsatisfiable: {NS}C{i}" for i in range(UNSATISFIABLE_SHOWN + 2)]
    reason = failure_reason(1, output_of(*lines), False, make_settings())
    shown = ", ".join(f"{NS}C{i}" for i in range(UNSATISFIABLE_SHOWN))
    assert reason == f"{UNSATISFIABLE_SHOWN + 2} unsatisfiable classes: {shown} and 2 more"


def test_tail() -> None:
    """Check that only the last lines are kept, each cut short if long."""
    output = output_of(*(f"line {i}" for i in range(TAIL_LINES + 5)), "x" * 10_000)
    assert len(output.tail) == TAIL_LINES
    assert output.tail[0] == "line 6"
    assert len(output.tail[-1]) == browl.robot.MAX_LINE_CHARS


def test_reason_args(data_dir: Path) -> None:
    """Check that materialize runs only when properties are configured, and the shortcuts query always."""
    source, out = Path("in.ofn"), Path("out.ttl")
    plain = reason_args(Reasoner.HERMIT, source, out, make_settings())
    assert plain[:4] == ["reason", "--input", "in.ofn", "--reasoner"] and plain[4] == "HermiT"
    assert "materialize" not in plain
    assert plain[-5:] == ["query", "--update", str(browl.robot.SHORTCUTS_QUERY), "--output", "out.ttl"]
    properties = [f"{NS}part_of", f"{NS}has_part"]
    materialized = reason_args(Reasoner.ELK, source, out, make_settings(MATERIALIZE_PROPERTIES=properties))
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


async def test_none_skips(data_dir: Path) -> None:
    """Check that with no reasoner chosen the stage does nothing, and the upload is what's served."""
    context = JobContext(1, make_settings(ROBOT_JAR="/nonexistent"), Reasoner.NONE, upload(data_dir, "ok.ofn"))
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


async def test_stops_group_on_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Check that a cancelled run stops ROBOT's whole process group, with SIGKILL if SIGTERM is ignored."""
    pid_file = tmp_path / "pid"
    fake_java(tmp_path, monkeypatch, f"trap '' TERM; sleep 60 & echo $$ > {pid_file}; wait")
    monkeypatch.setattr(browl.robot, "TERM_GRACE_S", 0.2)
    task = asyncio.create_task(run_robot([], settings))
    while not pid_file.exists() or not pid_file.read_text():
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gone(int(pid_file.read_text()))


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
@pytest.mark.parametrize("reasoner", [Reasoner.ELK, Reasoner.HERMIT])
async def test_success(settings: Settings, data_dir: Path, reasoner: Reasoner) -> None:
    """Check that a reasoned ontology replaces the upload as the one served, with inferred parents."""
    context = await reason(settings, data_dir, "ok.ofn", reasoner)
    assert context.reasoned == context.ontology == context.upload.with_name(REASONED)
    assert context.upload.with_name(browl.robot.PARTIAL).exists() is False
    text = context.reasoned.read_text()
    assert "s2/Dog" in text and "s2/Pet" in text  # Turtle; Dog ⊑ Pet is checked with the shortcuts below


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_materialize_and_shortcuts(settings: Settings, data_dir: Path, tmp_path: Path) -> None:
    """Check reasoning's result: materialized restrictions added, shortcuts removed, all else and equivalences kept."""
    settings = settings.model_copy(update={"materialize_properties": [f"{NS}part_of"]})
    context = await reason(settings, data_dir, "shortcuts.ofn")
    ofn = tmp_path / "out.ofn"
    await run_robot(["convert", "--input", str(context.reasoned), "--output", str(ofn)], settings)
    axioms = {
        line.replace(f"<{NS}", "").replace(">", "")
        for line in ofn.read_text().splitlines()
        if line.startswith(("SubClassOf", "EquivalentClasses")) and "owl:Thing" not in line
    }
    some = "SubClassOf({} ObjectSomeValuesFrom({} {}))".format
    assert axioms == {
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
    assert sorted(p.name for p in (data_dir / "uploads" / "1").iterdir()) == [f"ontology{Path(fixture).suffix}"]


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_time_limit(data_dir: Path) -> None:
    """Check that a run past REASONER_TIMEOUT_S is killed, and the job records the time limit."""
    settings = make_settings(ROBOT_JAR=str(find_robot_jar()), REASONER_TIMEOUT_S=1)
    context = JobContext(1, settings, Reasoner.HERMIT, pigeonhole(data_dir / "uploads" / "1" / "ontology.ofn", 9))
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
