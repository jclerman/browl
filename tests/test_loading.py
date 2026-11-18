"""Tests for the loading stage: a new TDB2 store per load, Fuseki switched to it, and its triples counted (A-4)."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

import browl.fuseki
import browl.imports
import browl.loading
from browl import derived
from browl.derived import read_meta
from browl.fuseki import UNION_FROM, FusekiAdmin, FusekiAdminError, FusekiClient, SparqlError, dataset_clause
from browl.jobs import ImportResult, JobContext, Site, Stage, StageError
from browl.loading import count_store, dataset_name, loading_stage, store_path
from browl.process import Completed
from browl.settings import Settings
from tests.conftest import find_robot_jar
from tests.helpers import StaticServer, make_app, make_settings, turtle

pytestmark = [pytest.mark.anyio, pytest.mark.fuseki]

EX = "http://example.org/"
COUNT = "SELECT (COUNT(*) AS ?n) {} WHERE {{ ?s ?p ?o }}"


@pytest.fixture
def settings(data_dir: Path, fuseki_url: str, fuseki_base: Path, jena_home: Path) -> Settings:
    """Return settings for the tests' Fuseki, its base directory, and Jena's tools."""
    return make_settings(FUSEKI_URL=fuseki_url, FUSEKI_BASE=str(fuseki_base), JENA_HOME=str(jena_home))


@pytest.fixture
async def http(fuseki_url: str) -> AsyncIterator[httpx2.AsyncClient]:
    """Yield an HTTP client for the tests' Fuseki."""
    async with httpx2.AsyncClient(base_url=fuseki_url) as client:
        yield client


def job(settings: Settings, generation: int, ontology: str, imports: dict[str, str] | None = None) -> JobContext:
    """Return a job's context with an uploaded ontology and fetched imports, as the earlier stages leave it."""
    directory = settings.data_dir / "uploads" / str(generation)
    (directory / "imports").mkdir(parents=True)
    upload = directory / "ontology.ttl"
    upload.write_text(ontology)
    context = JobContext(generation, settings, upload=upload)
    for number, (iri, content) in enumerate((imports or {}).items()):
        path = directory / "imports" / f"{number}.ttl"
        path.write_text(content)
        context.imports.append(ImportResult(iri))
        context.import_files[iri] = path
    context.imports.append(ImportResult("file:///x", reason="only http and https imports are fetched"))
    return context


async def count(http: httpx2.AsyncClient, dataset: str, dataset_clause: str = "") -> int:
    """Return how many triples a query with the dataset clause sees."""
    result = await FusekiClient(http).select(dataset, COUNT.format(dataset_clause))
    return int(result.rows[0][0].value)  # type: ignore[union-attr]


async def browl_datasets(http: httpx2.AsyncClient) -> list[str]:
    """Return the registered datasets that loads make."""
    return sorted(name for name in await FusekiAdmin(http).datasets() if name.startswith("gen-"))


async def test_load_and_replace(settings: Settings, http: httpx2.AsyncClient) -> None:
    """Check a first load and its replacement, and the graphs: the ontology's, and each import's.

    The `FROM` clause merges them as a set; the replacement removes the previous dataset and its store.
    """
    ontology = f"<{EX}a> <{EX}p> <{EX}b> .\n<{EX}b> <{EX}p> <{EX}c> .\n"
    imported = f"<{EX}a> <{EX}p> <{EX}b> .\n<{EX}x> <{EX}p> <{EX}y> .\n"  # one triple the ontology also states
    first = job(settings, 101, ontology, {f"{EX}imported": imported})
    await loading_stage(first, http)
    assert first.triples == 2
    assert first.imports == [
        ImportResult(f"{EX}imported", triples=2),
        ImportResult("file:///x", reason="only http and https imports are fetched"),
    ]
    assert await browl_datasets(http) == ["gen-101"]
    assert await count(http, "gen-101") == 2
    assert await count(http, "gen-101", dataset_clause(True)) == 3
    assert dataset_clause(False) == ""
    assert UNION_FROM in dataset_clause(True)

    second = job(settings, 102, f"<{EX}z> <{EX}p> <{EX}z> .\n")
    await loading_stage(second, http)
    assert second.triples == 1
    assert await browl_datasets(http) == ["gen-102"]
    assert not store_path(settings, 101).exists()
    assert await count(http, "gen-102") == 1


async def test_loader_failure(settings: Settings, http: httpx2.AsyncClient) -> None:
    """Check that a failed load registers nothing and leaves no store behind."""
    context = job(settings, 103, "not Turtle\n")
    with pytest.raises(StageError, match=r"^tdb2.tdbloader failed \(exit status \d+\):\n"):
        await loading_stage(context, http)
    assert dataset_name(103) not in await browl_datasets(http)
    assert not store_path(settings, 103).exists()


async def test_symlink_refused(settings: Settings, http: httpx2.AsyncClient, tmp_path: Path) -> None:
    """Check that a file that leads off the jobs volume, as a compromised sandbox could plant, isn't loaded."""
    context = job(settings, 115, f"<{EX}a> <{EX}p> <{EX}b> .\n", {f"{EX}i": ""})
    [path] = context.import_files.values()
    path.unlink()
    (tmp_path / "secret.ttl").write_text(f"<{EX}s> <{EX}p> <{EX}b> .\n")
    path.symlink_to(tmp_path / "secret.ttl")
    with pytest.raises(StageError, match=r"^0.ttl isn't a regular file on the jobs volume$"):
        await loading_stage(context, http)
    assert dataset_name(115) not in await browl_datasets(http)


async def test_count_mismatch(settings: Settings, http: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the load fails, and the new dataset is removed, if Fuseki serves other counts than the store holds."""

    async def wrong(*args: object) -> dict[str | None, int]:
        return {None: 99}

    monkeypatch.setattr(browl.loading, "count_store", wrong)
    context = job(settings, 104, f"<{EX}a> <{EX}p> <{EX}b> .\n")
    with pytest.raises(
        StageError, match=r"^Fuseki serves 1 triples in 1 graphs .* which holds 99 triples in 1 graphs$"
    ):
        await loading_stage(context, http)
    assert dataset_name(104) not in await browl_datasets(http)
    assert not store_path(settings, 104).exists()


@pytest.mark.parametrize(
    ("error", "raised", "match"),
    [
        (asyncio.CancelledError(), asyncio.CancelledError, None),
        (SparqlError("boom"), StageError, r"^counting the new store's triples through Fuseki failed: boom$"),
    ],
)
async def test_count_through_fuseki_fails(
    settings: Settings,
    http: httpx2.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    raised: type[BaseException],
    match: str | None,
) -> None:
    """Check that a load cancelled or failing once Fuseki has opened the new store removes it, dataset and store."""

    async def fail(*args: object) -> dict[str | None, int]:
        raise error

    monkeypatch.setattr(browl.loading, "count_fuseki", fail)
    context = job(settings, 111, f"<{EX}a> <{EX}p> <{EX}b> .\n")
    with pytest.raises(raised, match=match):
        await loading_stage(context, http)
    assert dataset_name(111) not in await browl_datasets(http)
    assert not store_path(settings, 111).exists()


async def test_registration_fails(
    settings: Settings, http: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a registration failing after Fuseki opened the store, as on a timeout, removes it through Fuseki."""
    add = FusekiAdmin.add

    async def add_then_fail(self: FusekiAdmin, name: str) -> None:
        await add(self, name)
        raise FusekiAdminError("Fuseki HTTP request POST /$/datasets timed out after 120 s")

    monkeypatch.setattr(FusekiAdmin, "add", add_then_fail)
    context = job(settings, 112, f"<{EX}a> <{EX}p> <{EX}b> .\n")
    with pytest.raises(StageError, match=r"^Fuseki HTTP request POST /\$/datasets timed out after 120 s$"):
        await loading_stage(context, http)
    assert dataset_name(112) not in await browl_datasets(http)
    assert not store_path(settings, 112).exists()


async def test_old_dataset_kept_on_failure(
    settings: Settings, http: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that failing to remove one old dataset fails nothing, and doesn't keep the others."""
    remove = FusekiAdmin.remove

    async def remove_but_113(self: FusekiAdmin, name: str) -> None:
        if name == dataset_name(113):
            raise FusekiAdminError("boom")
        await remove(self, name)

    await loading_stage(job(settings, 113, f"<{EX}a> <{EX}p> <{EX}b> .\n"), http)
    monkeypatch.setattr(FusekiAdmin, "remove", remove_but_113)
    await loading_stage(job(settings, 114, f"<{EX}a> <{EX}p> <{EX}b> .\n"), http)
    assert await browl_datasets(http) == ["gen-113", "gen-114"]
    context = job(settings, 115, f"<{EX}a> <{EX}p> <{EX}b> .\n")
    await loading_stage(context, http)
    assert context.triples == 1
    assert await browl_datasets(http) == ["gen-113", "gen-115"]
    monkeypatch.undo()
    await FusekiAdmin(http).remove(dataset_name(113))


async def test_stale_store_removed(settings: Settings, http: httpx2.AsyncClient) -> None:
    """Check that a store left by a load that stopped, which no dataset uses, is deleted by the next load."""
    stale = store_path(settings, 100)
    stale.mkdir(parents=True)
    (stale / "junk").write_text("x")
    await loading_stage(job(settings, 105, f"<{EX}a> <{EX}p> <{EX}b> .\n"), http)
    assert not stale.exists()
    assert store_path(settings, 105).is_dir()


async def test_same_name_replaced(settings: Settings, http: httpx2.AsyncClient, tmp_path: Path) -> None:
    """Check that a dataset already registered by the new one's name, as after the job state was reset, is replaced."""
    await loading_stage(job(settings, 107, f"<{EX}a> <{EX}p> <{EX}b> .\n"), http)
    again = settings.model_copy(update={"data_dir": tmp_path / "reset"})
    context = job(again, 107, f"<{EX}a> <{EX}p> <{EX}b> .\n<{EX}c> <{EX}p> <{EX}d> .\n")
    await loading_stage(context, http)
    assert await browl_datasets(http) == ["gen-107"]
    assert await count(http, "gen-107") == 2


async def test_nothing_to_load(settings: Settings, http: httpx2.AsyncClient) -> None:
    """Check that a job with nothing uploaded loads nothing."""
    context = JobContext(106, settings)
    await loading_stage(context, http)
    assert context.triples is None


@pytest.mark.parametrize(
    ("output", "match"),
    [
        (b"", r"^tdb2.tdbquery printed no result$"),
        (b"?g\t?n\n\xff\n", r"^tdb2.tdbquery's result isn't UTF-8: "),
        (b"g\tn\n", r"^tdb2.tdbquery's result is malformed: not a SPARQL TSV header"),
        (b"?g\t?n\n<urn:g>\n", r"^tdb2.tdbquery's result is malformed: expected 2 cells, got 1"),
        (b"?n\n1\n", r"^a triple count's row has 1 cells, not 2$"),
        (b'?g\t?n\n<urn:g>\t"x"\n', r"^a triple count isn't a number: "),
    ],
)
async def test_malformed_count(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: bytes, match: str
) -> None:
    """Check that `tdb2.tdbquery` output that isn't a count's TSV result fails the stage, saying why."""

    async def run_process(*args: object, on_stdout: Callable[[bytes], None], **kwargs: object) -> Completed:
        on_stdout(output)
        return Completed(0)

    monkeypatch.setattr(browl.loading, "run_process", run_process)
    with pytest.raises(StageError, match=match):
        await count_store(make_settings(), tmp_path)


@pytest.mark.parametrize(
    ("completed", "match"),
    [
        (Completed(None, timed_out=True), r"^time limit: tdb2.tdbloader took longer than 14400 s$"),
        (
            Completed(None, out_of_space=True),
            r"^disk space: a volume fell below [\d,]+ bytes free while tdb2.tdbloader",
        ),
        (Completed(1, tail=("boom",)), r"^tdb2.tdbloader failed \(exit status 1\):\nboom$"),
    ],
)
async def test_loader_limits(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed: Completed, match: str
) -> None:
    """Check that a loader stopped at a limit fails the stage saying which, and that it watches both volumes."""
    settings = make_settings(FUSEKI_BASE=str(tmp_path / "fuseki"))
    watched: list[object] = []

    async def run_process(*args: object, watch: list[Path], **kwargs: object) -> Completed:
        watched.extend(watch)
        return completed

    monkeypatch.setattr(browl.loading, "run_process", run_process)
    with pytest.raises(StageError, match=match):
        await browl.loading.load_file(settings, tmp_path, tmp_path / "o.ttl")
    assert watched == [settings.data_dir, settings.fuseki_base]


async def test_admin_errors() -> None:
    """Check that the admin client's failures say what failed."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json={"unexpected": []})
        return httpx2.Response(500, text="boom")

    async with httpx2.AsyncClient(base_url="http://fuseki", transport=httpx2.MockTransport(handler)) as client:
        admin = FusekiAdmin(client)
        with pytest.raises(FusekiAdminError, match="malformed"):
            await admin.datasets()
        with pytest.raises(FusekiAdminError, match=r"POST /\$/datasets answered 500: boom"):
            await admin.add("gen-1")
        with pytest.raises(ValueError, match="not a dataset name"):
            await admin.remove("../x")


async def test_admin_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that admin calls get `ADMIN_TIMEOUT_S` for each phase, not the client's default, and say how they fail."""
    timeouts: list[dict[str, float]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        timeouts.append(request.extensions["timeout"])
        if request.method == "DELETE":
            raise httpx2.ConnectError("refused")
        await asyncio.sleep(1)
        return httpx2.Response(200)

    monkeypatch.setattr(browl.fuseki, "ADMIN_TIMEOUT_S", 0.1)
    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(base_url="http://fuseki", transport=transport, timeout=5.0) as client:
        admin = FusekiAdmin(client)
        with pytest.raises(FusekiAdminError, match=r"^Fuseki HTTP request POST /\$/datasets timed out after 0.1 s$"):
            await admin.add("gen-1")
        with pytest.raises(
            FusekiAdminError, match=r"^Fuseki HTTP request DELETE /\$/datasets/gen-1 failed: ConnectError"
        ):
            await admin.remove("gen-1")
    assert timeouts == [dict.fromkeys(("connect", "read", "write", "pool"), 0.1)] * 2


def test_app_loads_and_restarts(
    settings: Settings, site: StaticServer, monkeypatch: pytest.MonkeyPatch, fuseki_url: str, fuseki_base: Path
) -> None:
    """Check an upload end to end, with nested imports and one that can't be fetched, then a restart.

    The restarted app serves the same store, and reuses the derived database, without loading or building anything.
    """
    monkeypatch.setattr(browl.imports, "is_public", lambda address: True)  # the imports are on the local server
    nested = site.page("/n.ttl", turtle(site.url + "/n.ttl", extra=f"<{EX}n> <{EX}p> <{EX}n> ."))
    direct = site.page("/d.ttl", turtle(site.url + "/d.ttl", nested))
    missing = site.url + "/missing.ttl"
    overrides = {"FUSEKI_URL": fuseki_url, "FUSEKI_BASE": str(fuseki_base), "JENA_HOME": str(settings.jena_home)}
    app = make_app(real_stages=True, **overrides)
    with TestClient(app) as client:
        files = {"ontology": ("o.ttl", turtle(f"{EX}o", direct, missing).encode())}
        assert client.post("/_browl/admin/upload", data={"reasoner": "none"}, files=files).status_code == 200
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
        state = app.state.jobs.state
        assert (state.stage, state.reason) == (Stage.READY, None)
        assert state.triples == 3
        assert [(result.iri, result.dropped, result.triples) for result in state.imports] == [
            (direct, False, 2),
            (missing, True, None),
            (nested, False, 2),
        ]
        generation = state.ready_generation
        assert "couldn't be loaded" in client.get("/_browl/admin/status").text
        with closing(derived.connect(settings.data_dir, generation)) as connection:
            assert read_meta(connection).ontology_iri == f"{EX}o"

        async def datasets() -> list[str]:
            async with httpx2.AsyncClient(base_url=fuseki_url) as http:
                return await browl_datasets(http)

        before = client.portal.call(datasets)
    store = store_path(settings, generation)
    modified = store.stat().st_mtime_ns
    built = derived.derived_path(settings.data_dir, generation).stat().st_mtime_ns

    restarted = make_app(real_stages=True, **overrides)
    with TestClient(restarted) as client:
        assert restarted.state.jobs.state.ready_generation == generation
        assert restarted.state.jobs.state.site == Site.AVAILABLE
        assert client.portal is not None
        assert client.portal.call(datasets) == before == [dataset_name(generation)]
        assert store.stat().st_mtime_ns == modified
        assert derived.derived_path(settings.data_dir, generation).stat().st_mtime_ns == built  # reused, not rebuilt

        async def served() -> int:
            async with httpx2.AsyncClient(base_url=fuseki_url) as http:
                return await count(http, dataset_name(generation), UNION_FROM)

        assert client.portal.call(served) == 3 + 2 + 2


@pytest.mark.parametrize(
    ("name", "content", "triples"),
    [
        pytest.param(
            "o.ofn",
            f"Prefix(:=<{EX}>)\nOntology(<{EX}o>\n"
            "Declaration(Class(:A))\nDeclaration(Class(:B))\nSubClassOf(:A :B)\n)\n",
            4,
            marks=pytest.mark.jvm,
            id="ofn",
        ),
        pytest.param(
            "o.owl",
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#" xmlns:owl="http://www.w3.org/2002/07/owl#">'
            f'<owl:Ontology rdf:about="{EX}o"/><rdf:Description rdf:about="{EX}A">'
            f'<rdfs:subClassOf rdf:resource="{EX}B"/></rdf:Description></rdf:RDF>',
            2,
            id="rdf-xml",
        ),
    ],
)
def test_app_loads_each_syntax(
    request: pytest.FixtureRequest,
    settings: Settings,
    fuseki_url: str,
    fuseki_base: Path,
    name: str,
    content: str,
    triples: int,
) -> None:
    """Check that an OFN upload (converted to Turtle) and an RDF/XML one each load without a reasoner."""
    if request.node.get_closest_marker("jvm"):
        request.getfixturevalue("jvm")  # the OFN lister's JVM needs Java 21 (`JAVA_HOME`), as the fixture finds it
    overrides = {
        "FUSEKI_URL": fuseki_url,
        "FUSEKI_BASE": str(fuseki_base),
        "JENA_HOME": str(settings.jena_home),
        "ROBOT_JAR": str(find_robot_jar()),
    }
    app = make_app(real_stages=True, **overrides)
    with TestClient(app) as client:
        files = {"ontology": (name, content.encode())}
        assert client.post("/_browl/admin/upload", data={"reasoner": "none"}, files=files).status_code == 200
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
    state = app.state.jobs.state
    assert (state.stage, state.reason, state.triples) == (Stage.READY, None, triples)


def test_app_failed_load(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, fuseki_url: str, fuseki_base: Path
) -> None:
    """Check that a load that fails records the stage and reason, and the site then serves "not available"."""

    async def wrong(*args: object) -> dict[str | None, int]:
        return {None: 99}

    monkeypatch.setattr(browl.loading, "count_store", wrong)
    overrides = {"FUSEKI_URL": fuseki_url, "FUSEKI_BASE": str(fuseki_base), "JENA_HOME": str(settings.jena_home)}
    app = make_app(real_stages=True, **overrides)
    with TestClient(app) as client:
        files = {"ontology": ("o.ttl", f"<{EX}a> <{EX}p> <{EX}b> .\n".encode())}
        client.post("/_browl/admin/upload", data={"reasoner": "none"}, files=files)
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
        state = app.state.jobs.state
        assert state.failed_stage == Stage.LOADING
        assert state.reason is not None and state.reason.startswith("Fuseki serves")
        assert client.get("/anything").status_code == 503
