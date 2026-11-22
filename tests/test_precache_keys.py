"""Tests for the derived database's build keys: the import scanner, the keys, and that each covers what its code uses.

The coverage tests are the guarantee that a derived file is current only for the code and settings that would build it
now: they run the header and each hook in `HOOKS` against a stubbed Fuseki, tracing the browl code that runs, the
files under `browl/` opened and the settings read, and check that the component's key covers them all.
"""

import dataclasses
import importlib
import json
import operator
import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType, SimpleNamespace
from typing import Any

import pytest
from pydantic import Secret, SecretBytes, SecretStr

import browl
from browl import derived
from browl.derived import Meta
from browl.fuseki import FusekiClient
from browl.jobs import JobContext
from browl.jobtypes import ImportResult, Reasoner
from browl.loading import dataset_name
from browl.precache import header, keys, usage_table
from browl.precache.core import Hook, PreCacheContext, hook
from browl.precache.keys import Closure, build_keys, closure, code_digest
from browl.precache.stage import HOOKS, build, on_thread
from browl.precache.usage_table import Progress, fill_usage_table
from browl.settings import Settings
from browl.usage_tables import build_file
from tests.helpers import LOADED_AT, fuseki, make_settings

pytestmark = pytest.mark.anyio

BROWL_DIR = Path(browl.__file__).resolve().parent
EX = "http://example.org/"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """Return the test settings, with `DATA_DIR` an empty directory."""
    return make_settings()


# The scanner.


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    """Yield the name and directory of a new, empty package on `sys.path`; it's forgotten afterwards."""
    name = f"keytest_{uuid.uuid4().hex}"
    directory = tmp_path / "packages" / name
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(directory.parent))
    yield name, directory
    for module in [module for module in sys.modules if module == name or module.startswith(f"{name}.")]:
        del sys.modules[module]


def write(directory: Path, files: dict[str, str]) -> None:
    """Write files, dedented, into a directory, making any directories they're in."""
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    importlib.invalidate_caches()


MODULES = {
    "main.py": '''
        """The module whose closure is found."""

        import json
        import typing
        from typing import TYPE_CHECKING

        import {pkg}.absolute
        from {pkg} import imported
        from {pkg}.sub import VALUE
        from . import relative
        from .functions import function

        import httpx2
        from browl.precache.core import sparql_file

        if TYPE_CHECKING:
            from {pkg} import typed
        else:
            from {pkg} import otherwise
        if typing.TYPE_CHECKING:
            import {pkg}.typed_too

        ANSWER = 42
        """An attribute's docstring."""


        def query() -> str:
            """Return the query."""
            return sparql_file("q.rq") + function() + str(VALUE)  # a comment
        ''',
    "absolute.py": "X = 1\n",
    "imported.py": '"""Imported with from."""\n\nfrom .deep import Y\n',
    "deep.py": "Y = 2\n",
    "relative.py": "Z = 3\n",
    "functions.py": "def function() -> str:\n    return ''\n",
    "sub/__init__.py": "VALUE = 4\n",
    "unused.py": "W = 5\n",
    "typed.py": "T = 6\n",
    "typed_too.py": "T = 7\n",
    "otherwise.py": "O = 8\n",
}


@pytest.fixture
def scanned(package: tuple[str, Path], tmp_path: Path) -> tuple[str, Path, Path]:
    """Yield the package of `MODULES`, its directory, and the directory `q.rq` is in."""
    name, directory = package
    write(directory, {file: text.replace("{pkg}", name) for file, text in MODULES.items()})
    sparql = tmp_path / "sparql"
    write(sparql, {"q.rq": "SELECT * WHERE { ?s ?p ?o }\n"})
    return name, directory, sparql


def test_closure(scanned: tuple[str, Path, Path]) -> None:
    """Check that the closure follows absolute, `from` and relative imports, and finds `sparql_file`'s files.

    A package's `__init__` is in it when a name that isn't a submodule is imported from it. Imports under `if
    TYPE_CHECKING:` are left out, but not its `else`. The standard library is left out, and a third-party module is
    recorded by its distribution's version.
    """
    name, directory, sparql = scanned
    found = closure(f"{name}.main", root=name, sparql_dir=sparql)
    assert sorted(found.modules) == sorted(
        f"{name}.{module}"
        for module in ["main", "absolute", "imported", "deep", "relative", "functions", "sub", "otherwise"]
    )
    assert found.modules[f"{name}.sub"] == directory / "sub" / "__init__.py"
    assert found.files == {"q.rq": sparql / "q.rq"}
    assert found.distributions["httpx2"] == importlib.metadata.version("httpx2")
    assert "json" not in found.distributions


def digest(scanned: tuple[str, Path, Path]) -> str:
    """Return the code digest of the scanned package's `main` module."""
    name, _, sparql = scanned
    return code_digest(closure(f"{name}.main", root=name, sparql_dir=sparql))


@pytest.mark.parametrize(
    ("file", "old", "new", "changes"),
    [
        ("main.py", '"""Return the query."""', '"""Return the query, edited."""', False),
        ("main.py", '"""An attribute\'s docstring."""', "", False),
        ("main.py", "# a comment", "# another comment", False),
        ("main.py", "ANSWER = 42", "ANSWER  =  (42)", False),
        ("imported.py", '"""Imported with from."""', '"""Another docstring."""', False),
        ("unused.py", "W = 5", "W = 6", False),
        ("main.py", "ANSWER = 42", "ANSWER = 43", True),
        ("deep.py", "Y = 2", "Y = 3", True),
        ("sub/__init__.py", "VALUE = 4", "VALUE = 5", True),
    ],
    ids=[
        "docstring",
        "attribute docstring",
        "comment",
        "formatting",
        "imported docstring",
        "unused module",
        "code",
        "imported code",
        "package code",
    ],
)
def test_digest_changes(scanned: tuple[str, Path, Path], file: str, old: str, new: str, changes: bool) -> None:
    """Check that the digest changes with the code of a module in the closure, but not its docstrings or comments."""
    before = digest(scanned)
    path = scanned[1] / file
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    assert (digest(scanned) != before) == changes


def test_digest_changes_with_file(scanned: tuple[str, Path, Path]) -> None:
    """Check that the digest changes with a file loaded through `sparql_file`."""
    before = digest(scanned)
    (scanned[2] / "q.rq").write_text("SELECT ?s WHERE { ?s ?p ?o }\n")
    assert digest(scanned) != before


@pytest.mark.parametrize(
    "change",
    [
        lambda found: dataclasses.replace(found, distributions={**found.distributions, "httpx2": "0.0.1"}),
        lambda found: dataclasses.replace(found, distributions={**found.distributions, "other": None}),
    ],
    ids=["distribution version", "another distribution"],
)
def test_digest_changes_with_distributions(
    scanned: tuple[str, Path, Path], change: Callable[[Closure], Closure]
) -> None:
    """Check that the digest changes with the versions of the distributions the closure imports."""
    name, _, sparql = scanned
    found = closure(f"{name}.main", root=name, sparql_dir=sparql)
    assert code_digest(change(found)) != code_digest(found)


def test_digest_changes_with_python(scanned: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the digest changes with Python's minor version, but not its micro version."""
    name, _, sparql = scanned
    found = closure(f"{name}.main", root=name, sparql_dir=sparql)
    before = code_digest(found)
    major, minor, micro = sys.version_info[:3]
    monkeypatch.setattr(
        keys, "sys", SimpleNamespace(version_info=SimpleNamespace(major=major, minor=minor, micro=micro + 1))
    )
    assert code_digest(found) == before
    monkeypatch.setattr(
        keys, "sys", SimpleNamespace(version_info=SimpleNamespace(major=major, minor=minor + 1, micro=0))
    )
    assert code_digest(found) != before


@pytest.mark.parametrize("call", ["sparql_file(NAME)", "sparql_file('a.rq', 'b.rq')", "core.sparql_file(name='a.rq')"])
def test_closure_needs_literal(package: tuple[str, Path], call: str) -> None:
    """Check that a `sparql_file` call with anything but one string literal fails, as its file can't be found."""
    name, directory = package
    write(
        directory,
        {"main.py": f"from browl.precache import core\nfrom browl.precache.core import sparql_file\n{call}\n"},
    )
    with pytest.raises(ValueError, match=r"main\.py:3: pass sparql_file one string literal"):
        closure(f"{name}.main", root=name)


def test_closure_needs_source() -> None:
    """Check that a module without Python source can't be scanned."""
    with pytest.raises(ValueError, match="the module sys has no Python source"):
        closure("sys")


# The keys.


def test_build_keys(settings: Settings) -> None:
    """Check that a file's keys are the schema's, the header's and each hook's."""
    built = build_keys(settings, HOOKS)
    assert list(built) == ["schema", "meta", *(hook.name for hook in HOOKS)]
    assert build_keys(make_settings(), HOOKS) == built  # computed the same each time


@pytest.mark.parametrize(
    ("name", "change"),
    [
        ("SCHEMA", lambda schema: schema + "CREATE TABLE extra (x INTEGER);\n"),
        ("INDEXES", lambda indexes: {**indexes, "entity": (*indexes["entity"], "CREATE INDEX extra ON entity (type)")}),
    ],
)
def test_schema_key_follows_schema(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, name: str, change: Callable[[Any], Any]
) -> None:
    """Check that a change to `browl.derived.SCHEMA` or `INDEXES` changes the schema's key, and only it."""
    before = build_keys(settings, HOOKS)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(derived, name, change(getattr(derived, name)))
            keys.schema_key.cache_clear()
            after = build_keys(settings, HOOKS)
    finally:
        keys.schema_key.cache_clear()
    assert {component for component in before if before[component] != after[component]} == {keys.SCHEMA}


# Prints the build keys for the settings given as JSON, run where `browl` is importable but `tests` may not be.
_PRINT_KEYS = """
import json, sys
from browl.precache import HOOKS, build_keys
from browl.settings import Settings
print(json.dumps(build_keys(Settings.model_validate(json.loads(sys.argv[1])), HOOKS)))
"""


def test_build_keys_independent_of_process(data_dir: Path, tmp_path: Path) -> None:
    """Check that the keys depend only on content: the same in processes with other hash seeds and directories."""
    config = {
        "BASE_URL": "http://example.com/sub/",
        "AUTH_DISABLED": True,
        "DATA_DIR": str(data_dir),
        "EXTRA_PREFIXES": {f"p{n}": f"{EX}{n}/" for n in range(10)},
        "NAME_PROPERTIES": ["skos:prefLabel", "rdfs:label", "skos:altLabel"],
        "LANGUAGE_RANKING": ["fr", "en", "de"],
    }
    outputs = []
    for seed in ["1", "2"]:
        directory = tmp_path / f"cwd-{seed}"
        directory.mkdir()
        result = subprocess.run(
            [sys.executable, "-c", _PRINT_KEYS, json.dumps(config)],
            cwd=directory,
            env=os.environ | {"PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(json.loads(result.stdout))
    assert outputs[0] == outputs[1]
    assert outputs[0] == build_keys(Settings.model_validate(config), HOOKS)  # and in this process


@pytest.mark.parametrize(
    ("overrides", "changed"),
    [
        ({"BASE_URL": "http://example.com/"}, {"entities"}),
        ({"RESERVED_PREFIX": "_other"}, {"entities"}),
        ({"NAME_PROPERTIES": ["skos:prefLabel"]}, {"meta", "names"}),
        ({"LANGUAGE_RANKING": ["fr"]}, {"meta", "names"}),
        ({"EXTRA_PREFIXES": {"ex": EX}}, {"meta", "names"}),
        ({"OIDC_CLIENT_SECRET": "rotated", "FUSEKI_URL": "http://other:3030"}, set()),
    ],
    ids=["base url", "reserved prefix", "name properties", "language ranking", "extra prefixes", "undeclared"],
)
def test_keys_follow_settings(settings: Settings, overrides: dict[str, object], changed: set[str]) -> None:
    """Check that a setting changes the key of each component that declares it, and only those."""
    before = build_keys(settings, HOOKS)
    after = build_keys(make_settings(**overrides), HOOKS)
    assert {component for component in before if before[component] != after[component]} == changed


async def nothing(context: PreCacheContext) -> None:
    """Do nothing, as a hook."""


def test_build_keys_rejects_unknown_setting(settings: Settings) -> None:
    """Check that a hook can't declare a setting that doesn't exist."""
    with pytest.raises(ValueError, match=r"not Settings fields: \['no_such_setting'\]"):
        build_keys(settings, [hook("base_url", "no_such_setting")(nothing)])


def test_build_keys_rejects_secret(settings: Settings) -> None:
    """Check that a hook can't declare a secret setting, which a key would miss the changes of, and expose."""
    with pytest.raises(ValueError, match=r"secret Settings fields can't be in a build key: \['oidc_client_secret'\]"):
        build_keys(settings, [hook("base_url", "oidc_client_secret")(nothing)])


@pytest.mark.parametrize(
    ("annotation", "secret"),
    [
        (SecretStr, True),
        (SecretStr | None, True),
        (SecretBytes, True),
        (Secret[int], True),
        (list[SecretStr], True),
        (str, False),
        (str | None, False),
        (dict[str, str], False),
    ],
)
def test_is_secret(annotation: object, secret: bool) -> None:
    """Check that a secret type is found, alone, optional or as an argument."""
    assert keys._is_secret(annotation) == secret


@pytest.mark.parametrize("names", [["nothing", "nothing"], ["meta"]], ids=["two hooks", "hook named meta"])
def test_build_keys_rejects_duplicate_names(settings: Settings, names: list[str]) -> None:
    """Check that components must have different names."""

    def named(name: str) -> Hook:
        function: Any = lambda context: nothing(context)  # noqa: E731
        function.__name__ = name
        return hook()(function)

    with pytest.raises(ValueError, match="two build key components are named"):
        build_keys(settings, [named(name) for name in names])


def test_hook_name_and_module() -> None:
    """Check that a hook is named after its function, and its key covers its function's module."""
    made = hook("base_url")(nothing)
    assert (made.name, made.module, made.settings) == ("nothing", __name__, frozenset({"base_url"}))


# What each key covers, traced.


class RecordingSettings:
    """Settings that record which of their fields are read, and any other use, which could read them all.

    Anything but a field, such as `model_dump`, `__dict__` (it has slots, so `__dict__` goes through `__getattr__`) or
    iteration, could read every field unrecorded, so it's recorded in `bulk` instead.

    Attributes:
        read: The names of the fields read.
        bulk: The other attributes read, and `__iter__` if it was iterated.
    """

    __slots__ = ("_settings", "bulk", "read")

    def __init__(self, settings: Settings) -> None:
        """Wrap the settings."""
        self._settings = settings
        self.read: set[str] = set()
        self.bulk: set[str] = set()

    def __getattr__(self, name: str) -> object:
        """Return a setting, recording that it was read, or any other attribute, recording it in `bulk`."""
        (self.read if name in Settings.model_fields else self.bulk).add(name)
        return getattr(self._settings, name)

    def __iter__(self) -> Iterator[tuple[str, object]]:
        """Iterate over the fields and their values, recording it in `bulk`."""
        self.bulk.add("__iter__")
        return iter(self._settings)

    def clear(self) -> None:
        """Forget what was read."""
        self.read.clear()
        self.bulk.clear()


@dataclass
class Trace:
    """What ran while tracing.

    Attributes:
        code: The source files of the Python functions that started.
        opened: The paths opened, as given.
    """

    code: set[str] = field(default_factory=set)
    opened: set[str] = field(default_factory=set)


_tracing: Trace | None = None  # the audit hook's target; None when not tracing


def _audit(event: str, args: tuple[Any, ...]) -> None:
    trace = _tracing
    if trace is not None and event == "open" and isinstance(args[0], str | bytes | os.PathLike):
        trace.opened.add(os.fsdecode(args[0]))


sys.addaudithook(_audit)  # it can't be removed, so it's inert unless tracing


@contextmanager
def tracing() -> Iterator[Trace]:
    """Trace the functions that start (`sys.monitoring`, every thread) and the files opened (audit events)."""
    global _tracing
    monitoring = sys.monitoring
    tool = next(tool for tool in range(6) if monitoring.get_tool(tool) is None)
    monitoring.use_tool_id(tool, "browl build key coverage")
    trace = Trace()

    def started(code: CodeType, offset: int) -> None:
        trace.code.add(code.co_filename)

    monitoring.register_callback(tool, monitoring.events.PY_START, started)
    monitoring.set_events(tool, monitoring.events.PY_START)
    _tracing = trace
    try:
        yield trace
    finally:
        _tracing = None
        monitoring.set_events(tool, monitoring.events.NO_EVENTS)
        monitoring.register_callback(tool, monitoring.events.PY_START, None)
        monitoring.free_tool_id(tool)


def uncovered(
    component: str, module: str, declared: frozenset[str], trace: Trace, recording: RecordingSettings
) -> list[str]:
    """Return what a component used that its key doesn't cover, each with what the developer should do."""
    covered = closure(module)
    sources = {path.resolve() for path in covered.modules.values()}
    files = {path.resolve() for path in covered.files.values()}
    problems = []
    for source in sorted({Path(name).resolve() for name in trace.code} - sources):
        if source.is_relative_to(BROWL_DIR):
            problems.append(
                f"{component} ran code in {source}, which its key doesn't cover: import that module, by name, from"
                f" {module} or a browl module it imports"
            )
    for opened in sorted({Path(name).resolve() for name in trace.opened} - files):
        if opened.is_relative_to(BROWL_DIR) and opened.suffix not in {".py", ".pyc"}:
            problems.append(
                f"{component} opened {opened}, which its key doesn't cover: load it through"
                " browl.precache.core.sparql_file, with a string literal"
            )
    where = "in @hook(...)" if component != keys.META else "in browl.precache.header.SETTINGS"
    for name in sorted(recording.read - declared):
        problems.append(f"{component} read the setting {name!r}: declare it {where}, so its key covers its value")
    if recording.bulk:
        problems.append(
            f"{component} used the settings other than by reading a field ({', '.join(sorted(recording.bulk))}),"
            f" which its key can't cover: read each setting by name, and declare it {where}"
        )
    return problems


def traced_job(settings: Settings) -> tuple[JobContext, RecordingSettings]:
    """Return a job whose settings record what's read, and those settings."""
    recording = RecordingSettings(settings)
    job: Any = JobContext(
        1,
        recording,
        Reasoner.ELK,
        imports=[ImportResult(f"{EX}i", triples=1)],
        started_at=LOADED_AT,  # type: ignore[arg-type]
    )
    return job, recording


async def test_meta_key_covers_header(settings: Settings) -> None:
    """Check that the `meta` key covers the code the header runs, the files it opens and the settings it reads."""
    job, recording = traced_job(settings)
    dataset = dataset_name(1)
    async with fuseki() as http:
        with tracing() as trace:
            meta = await header.header_meta(job, FusekiClient(http), dataset)
    assert meta.title == "Onto"
    assert str(Path(header.__file__).resolve()) in {str(Path(name).resolve()) for name in trace.code}  # it traced
    assert uncovered(keys.META, header.__name__, header.SETTINGS, trace, recording) == []


@pytest.mark.parametrize("index", range(len(HOOKS)), ids=[hook.name for hook in HOOKS])
async def test_hook_key_covers_hook(settings: Settings, index: int) -> None:
    """Check that a hook's key covers the code it runs, the files it opens and the settings it reads.

    The hooks before it run first, untraced, as it may read their tables.
    """
    job, recording = traced_job(settings)
    meta = Meta(1, Reasoner.ELK, LOADED_AT, ontology_iri=f"{EX}onto")
    traced = HOOKS[index]
    async with fuseki() as http, build(settings.data_dir, meta, build_keys(settings, HOOKS)) as db:
        context = PreCacheContext(job, FusekiClient(http), dataset_name(1), db)
        for earlier in HOOKS[:index]:
            await earlier(context)
        recording.clear()
        with tracing() as trace:
            await traced(context)
    module = sys.modules[traced.module]
    assert module.__file__ is not None
    assert str(Path(module.__file__).resolve()) in {str(Path(name).resolve()) for name in trace.code}  # it traced
    assert uncovered(traced.name, traced.module, traced.settings, trace, recording) == []


async def test_usage_table_key_covers_build(settings: Settings) -> None:
    """Check that the usage table's key covers the code its build runs and the files it opens; it reads no settings."""
    job, recording = traced_job(settings)
    meta = Meta(1, Reasoner.ELK, LOADED_AT, ontology_iri=f"{EX}onto")
    async with fuseki() as http:
        async with build(settings.data_dir, meta, build_keys(settings, HOOKS)) as db:
            context = PreCacheContext(job, FusekiClient(http), dataset_name(1), db)
            for hook in HOOKS:
                await hook(context)
        recording.clear()
        progress, sparql, dataset = Progress(), FusekiClient(http), dataset_name(1)
        async with on_thread(build_file(settings.data_dir, 1, "key"), "usage-table") as db:
            with tracing() as trace:
                await fill_usage_table(sparql, dataset, "", db, progress)
    assert progress.triples == 3
    assert str(Path(usage_table.__file__).resolve()) in {str(Path(name).resolve()) for name in trace.code}
    assert uncovered("usage table", usage_table.__name__, frozenset(), trace, recording) == []
    assert {"usage-scan.rq", "usage-scan-count.rq"} <= closure(usage_table.__name__).files.keys()


def test_usage_table_key() -> None:
    """Check that the usage table's key follows the derived database's keys, and is the same each time."""
    assert keys.usage_table_key({"a": "1"}) == keys.usage_table_key({"a": "1"})
    assert keys.usage_table_key({"a": "1"}) != keys.usage_table_key({"a": "2"})


def test_entities_reads_its_query(settings: Settings) -> None:
    """Check that the entity list is a file the entities hook's key covers."""
    [entities] = [hook for hook in HOOKS if hook.name == "entities"]
    assert "entity-list.rq" in closure(entities.module).files


async def test_uncovered_reports(package: tuple[str, Path], settings: Settings) -> None:
    """Check that tracing catches a hook that runs browl code, opens a file or reads a setting its key doesn't cover."""
    name, directory = package
    write(
        directory,
        {
            "sneaky.py": """
                import importlib
                from pathlib import Path


                async def sneaky(context):
                    importlib.import_module("browl.names").best_values([], ["en"])
                    directory = Path(importlib.import_module("browl").__file__).parent
                    (directory / "sparql" / "count-triples.rq").read_text()
                    return context.job.settings.reserved_prefix
                """
        },
    )
    function: Callable[..., Any] = importlib.import_module(f"{name}.sneaky").sneaky
    job, recording = traced_job(settings)
    with tracing() as trace:
        await function(SimpleContext(job))
    problems = uncovered("sneaky", f"{name}.sneaky", frozenset(), trace, recording)
    assert len(problems) == 3
    assert "ran code in" in problems[0] and "names.py" in problems[0] and "import that module" in problems[0]
    assert "count-triples.rq" in problems[1] and "load it through browl.precache.core.sparql_file" in problems[1]
    assert (
        problems[2]
        == "sneaky read the setting 'reserved_prefix': declare it in @hook(...), so its key covers its value"
    )


@dataclass
class SimpleContext:
    """A stand-in for `PreCacheContext` with only a job.

    Attributes:
        job: The job.
    """

    job: JobContext


@pytest.mark.parametrize(
    ("use", "bulk"),
    [
        (lambda settings: settings.model_dump(), "model_dump"),
        (lambda settings: settings.model_dump_json(), "model_dump_json"),
        (lambda settings: settings.__dict__, "__dict__"),
        (vars, "__dict__"),
        (dict, "__iter__, keys"),
        (lambda settings: [value for _, value in settings], "__iter__"),
        (operator.methodcaller("model_copy"), "model_copy"),
    ],
    ids=["model_dump", "model_dump_json", "__dict__", "vars", "dict", "iteration", "methodcaller"],
)
def test_uncovered_reports_bulk_reads(settings: Settings, use: Callable[[Any], object], bulk: str) -> None:
    """Check that using the settings other than by reading a field, which could read them all, is caught."""
    job, recording = traced_job(settings)
    use(job.settings)
    [problem] = uncovered("bulk", header.__name__, frozenset(), Trace(), recording)
    assert problem == (
        f"bulk used the settings other than by reading a field ({bulk}), which its key can't cover: read each setting"
        " by name, and declare it in @hook(...)"
    )
