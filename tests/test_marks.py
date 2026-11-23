"""Tests for the marks `tests/conftest.py` gives the tests that use the `jvm`, `fuseki_url` or `browser` fixture."""

import pytest

# The conftest of the inner runs: the hook under test, and stand-ins for the fixtures, which need nothing.
CONFTEST = """
import pytest

from tests.conftest import pytest_collection_modifyitems


@pytest.fixture
def jvm():
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


def test_direct(jvm):
    pass


def test_indirect(owl):
    pass


@pytest.mark.usefixtures("fuseki_url", "browser")
def test_others():
    pass


def test_plain():
    pass
"""

MARKS = ("jvm", "fuseki", "browser")

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


def test_marks_derived(inner: pytest.Pytester) -> None:
    """Check that each test gets the marks of the fixtures it uses, directly or through another fixture."""
    inner.makepyfile(TESTS)
    items, _ = inner.inline_genitems()
    marks = {item.name: sorted(mark.name for mark in item.iter_markers() if mark.name in MARKS) for item in items}
    assert marks == {
        "test_direct": ["jvm"],
        "test_indirect": ["jvm"],
        "test_others": ["browser", "fuseki"],
        "test_plain": [],
    }


@pytest.mark.parametrize(
    ("expression", "selected"),
    [
        ("jvm", ["test_direct", "test_indirect"]),
        ("not jvm", ["test_others", "test_plain"]),
        ("not jvm and not fuseki", ["test_plain"]),
    ],
)
def test_select_by_derived_marks(inner: pytest.Pytester, expression: str, selected: list[str]) -> None:
    """Check that `-m` selects by the derived marks."""
    inner.makepyfile(TESTS)
    run = inner.inline_run("-m", expression)
    run.assertoutcome(passed=len(selected))
    assert sorted(report.nodeid.split("::")[-1] for report in run.listoutcomes()[0]) == selected
    assert len(run.getcalls("pytest_deselected")[0].items) == 4 - len(selected)


def test_stray_mark_refused(inner: pytest.Pytester) -> None:
    """Check that a mark written by hand on a test that doesn't use its fixture is an error naming the test."""
    inner.makepyfile(
        """
        import pytest

        pytestmark = pytest.mark.fuseki


        @pytest.mark.jvm
        def test_stray():
            pass


        def test_kept(fuseki_url):
            pass
        """
    )
    result = inner.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [
            "ERROR: These tests are marked without using the fixture *",
            "test_stray_mark_refused.py::test_stray (jvm)",
            "test_stray_mark_refused.py::test_stray (fuseki)",
        ]
    )
    result.stderr.no_fnmatch_line("*test_kept*")
