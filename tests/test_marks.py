"""Tests for the marks `tests/conftest.py` gives the tests that use the `java`, `fuseki_url` or `browser` fixture."""

import re

import jpype
import pytest

from tests.conftest import FIXTURE_MARKS

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


def test_fuseki(fuseki_url):
    pass


def test_browser(browser):
    pass


def test_plain():
    '''A test, and a doctest, that use no fixture.

    >>> 1
    1
    '''


class TestClass:
    def test_method(self, java, fuseki_url):
        pass


@pytest.mark.parametrize("number", [1, 2])
def test_parametrized(number, browser):
    pass
"""

TEST_COUNT = 8
"""How many tests `TESTS` has, not counting the doctest."""

# The inner runs' options: the marks registered, for `--strict-markers`, and without pytest-playwright, whose own
# fixtures would clash with the outer run's.
INI = "[pytest]\naddopts = --strict-markers -p no:randomly -p no:playwright\nmarkers =\n" + "".join(
    f"    {mark}\n" for mark in FIXTURE_MARKS.values()
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


def name(nodeid: str) -> str:
    """Return a test's ID without its file's."""
    return nodeid.split("::", 1)[1]


def test_marks_derived(inner: pytest.Pytester) -> None:
    """Check that each test, doctest, method and parametrized test gets the marks of the fixtures it uses."""
    inner.makepyfile(test_inner=TESTS)
    items, _ = inner.inline_genitems("--doctest-modules")
    marks = {name(item.nodeid): sorted(mark.name for mark in item.iter_markers()) for item in items}
    assert marks == {
        "test_direct": ["java"],
        "test_indirect": ["java"],
        "test_fuseki": ["fuseki"],
        "test_browser": ["browser"],
        "test_plain": [],
        "test_inner.test_plain": [],
        "TestClass::test_method": ["fuseki", "java"],
        "test_parametrized[1]": ["browser", "parametrize"],
        "test_parametrized[2]": ["browser", "parametrize"],
    }


@pytest.mark.parametrize(
    ("expression", "selected"),
    [
        ("java", ["TestClass::test_method", "test_direct", "test_indirect"]),
        ("fuseki", ["TestClass::test_method", "test_fuseki"]),
        ("browser", ["test_browser", "test_parametrized[1]", "test_parametrized[2]"]),
        ("not java", ["test_browser", "test_fuseki", "test_parametrized[1]", "test_parametrized[2]", "test_plain"]),
        ("not java and not fuseki", ["test_browser", "test_parametrized[1]", "test_parametrized[2]", "test_plain"]),
    ],
)
def test_select_by_derived_marks(inner: pytest.Pytester, expression: str, selected: list[str]) -> None:
    """Check that `-m` selects by the derived marks."""
    inner.makepyfile(test_inner=TESTS)
    run = inner.inline_run("-m", expression)
    run.assertoutcome(passed=len(selected))
    assert sorted(name(report.nodeid) for report in run.listoutcomes()[0]) == selected
    assert len(run.getcalls("pytest_deselected")[0].items) == TEST_COUNT - len(selected)


STRAY_TESTS = """
import pytest

pytestmark = pytest.mark.fuseki


@pytest.mark.java
def test_stray():
    pass


@pytest.mark.browser
class TestStray:
    def test_method(self, fuseki_url):
        pass


@pytest.mark.parametrize("number", [pytest.param(1, marks=pytest.mark.java), 2])
def test_parametrized(number, fuseki_url):
    pass


def test_kept(fuseki_url):
    pass
"""


@pytest.mark.parametrize("options", [[], ["--numprocesses", "2"]], ids=["serial", "xdist"])
def test_stray_marks_fail(inner: pytest.Pytester, options: list[str]) -> None:
    """Check that each test with a mark written by hand, without its fixture, fails with why, in xdist runs too.

    The marks are on a test, its module, a class and a parameter.
    """
    inner.makepyfile(test_inner=STRAY_TESTS)
    result = inner.runpytest(*options)
    result.assert_outcomes(passed=2, errors=3)
    for test, reason in {
        "test_stray": "`java` without the `java` fixture, `fuseki` without the `fuseki_url` fixture",
        "TestStray.test_method": "`browser` without the `browser` fixture",
        "test_parametrized[1]": "`java` without the `java` fixture",
    }.items():
        # Under xdist, the worker's name comes between the two lines.
        result.stdout.re_match_lines(
            [f".* ERROR at setup of {re.escape(test)} ", f"marked {re.escape(reason)}; use the fixture"]
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
