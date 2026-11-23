"""Tests for the marks `tests/conftest.py` gives the tests that use the `java`, `fuseki_url` or `browser` fixture."""

import jpype
import pytest

# The conftest of the inner runs: the hook under test, and stand-ins for the fixtures, which need nothing.
CONFTEST = """
import pytest

from tests.conftest import pytest_collection_modifyitems, pytest_runtest_setup


@pytest.fixture
def java():
    pass


@pytest.fixture
def jvm(java):
    pass


@pytest.fixture
def fuseki_url():
    return "http://fuseki"


@pytest.fixture
def browser():
    pass


@pytest.fixture
def owl(jvm):
    pass
"""

TESTS = """
import pytest


def test_direct(java):
    pass


def test_indirect(owl):
    pass


@pytest.mark.usefixtures("fuseki_url", "browser")
def test_others():
    pass


def test_plain():
    pass
"""

MARKS = ("java", "fuseki", "browser")

# The inner runs' options: the marks registered, for `--strict-markers`, and without pytest-playwright, whose own
# fixtures would clash with the outer run's.
INI = "[pytest]\naddopts = --strict-markers -p no:randomly -p no:playwright\nmarkers =\n" + "".join(
    f"    {mark}\n" for mark in MARKS
)


@pytest.fixture
def inner(pytester: pytest.Pytester) -> pytest.Pytester:
    """Return a `pytester` with the conftest and markers for the inner runs."""
    pytester.makeconftest(CONFTEST)
    pytester.makeini(INI)
    return pytester


@pytest.fixture
def real(pytester: pytest.Pytester) -> pytest.Pytester:
    """Return a `pytester` whose inner runs use the real `java` and `jvm` fixtures."""
    pytester.makeconftest("from tests.conftest import java, jvm, pytest_collection_modifyitems\n")
    pytester.makeini(INI)
    pytester.makepyfile("def test_x(jvm):\n    pass\n")
    return pytester


def test_marks_derived(inner: pytest.Pytester) -> None:
    """Check that each test gets the marks of the fixtures it uses, directly or through another fixture."""
    inner.makepyfile(TESTS)
    items, _ = inner.inline_genitems()
    marks = {item.name: sorted(mark.name for mark in item.iter_markers() if mark.name in MARKS) for item in items}
    assert marks == {
        "test_direct": ["java"],
        "test_indirect": ["java"],
        "test_others": ["browser", "fuseki"],
        "test_plain": [],
    }


@pytest.mark.parametrize(
    ("expression", "selected"),
    [
        ("java", ["test_direct", "test_indirect"]),
        ("not java", ["test_others", "test_plain"]),
        ("not java and not fuseki", ["test_plain"]),
    ],
)
def test_select_by_derived_marks(inner: pytest.Pytester, expression: str, selected: list[str]) -> None:
    """Check that `-m` selects by the derived marks."""
    inner.makepyfile(TESTS)
    run = inner.inline_run("-m", expression)
    run.assertoutcome(passed=len(selected))
    assert sorted(report.nodeid.split("::")[-1] for report in run.listoutcomes()[0]) == selected
    assert len(run.getcalls("pytest_deselected")[0].items) == 4 - len(selected)


@pytest.mark.parametrize("options", [[], ["--numprocesses", "2"]], ids=["serial", "xdist"])
def test_stray_mark_fails(inner: pytest.Pytester, options: list[str]) -> None:
    """Check that a test with a mark written by hand, without its fixture, fails with why, in xdist runs too."""
    inner.makepyfile(
        """
        import pytest

        pytestmark = pytest.mark.fuseki


        @pytest.mark.java
        def test_stray():
            pass


        def test_kept(fuseki_url):
            pass
        """
    )
    result = inner.runpytest(*options)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at setup of test_stray*",
            "marked `java` without the `java` fixture, `fuseki` without the `fuseki_url` fixture; use the fixture*",
            "ERROR test_stray_mark_fails.py::test_stray*",
        ]
    )


def test_jvm_gives_java_mark(real: pytest.Pytester) -> None:
    """Check that the real `jvm` fixture gives the `java` mark, through `java`; collecting doesn't start the JVM."""
    items, _ = real.inline_genitems()
    assert [mark.name for mark in items[0].iter_markers()] == ["java"]


@pytest.mark.parametrize(
    ("ci", "line"),
    [
        (None, "SKIPPED * no robot.jar at /nonexistent/robot.jar*"),
        ("1", "*Failed: CI must run every test: no robot.jar at /nonexistent/robot.jar*"),
    ],
    ids=["local", "ci"],
)
def test_java_missing(real: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, ci: str | None, line: str) -> None:
    """Check that `java` skips a test when `robot.jar` is missing, but fails it in CI."""
    # The inner run shares this process, whose JVM may be running; the patches are undone after the test.
    monkeypatch.setattr(jpype, "isJVMStarted", lambda: False)
    monkeypatch.setenv("ROBOT_JAR", "/nonexistent/robot.jar")
    monkeypatch.setenv("JAVA_HOME", "/nonexistent")
    if ci:
        monkeypatch.setenv("CI", ci)
    else:
        monkeypatch.delenv("CI", raising=False)
    result = real.runpytest("-rs")
    result.assert_outcomes(**({"errors": 1} if ci else {"skipped": 1}))
    result.stdout.fnmatch_lines([line])
