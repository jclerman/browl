"""Tests for the imports stage: listing imports, fetching them only from public addresses, and the catalog (A-4)."""

import ipaddress
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import browl.imports
from browl.imports import (
    OWL_IMPORTS,
    FetchError,
    _ImportLines,
    catalog_names,
    empty_ontology,
    fetch,
    fetch_client,
    imports_stage,
    is_public,
    list_rdf_imports,
)
from browl.jobs import STATE_DB, ImportResult, JobContext, JobState, JobStore, Reasoner, Stage, StageError
from browl.robot import reasoning_stage
from browl.settings import Settings
from browl.upload import Syntax
from tests.conftest import find_robot_jar
from tests.helpers import Site, make_app, make_settings, turtle

pytestmark = pytest.mark.anyio

EX = "http://example.org/"


def allow_all(address: object) -> bool:
    """Allow every address, for fetching from the tests' local server."""
    return True


@pytest.fixture
def settings(data_dir: Path, jena_home: Path) -> Settings:
    """Return settings with the tests' Jena and robot.jar."""
    return make_settings(JENA_HOME=str(jena_home), ROBOT_JAR=str(find_robot_jar()))


def store_upload(data_dir: Path, content: str, suffix: str = ".ttl") -> Path:
    """Store an upload where the uploading stage would."""
    path = data_dir / "uploads" / "1" / f"ontology{suffix}"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    return path


def catalog_map(path: Path) -> dict[str, str]:
    """Return a catalog's entries: each name and the file it maps to."""
    namespace = "{urn:oasis:names:tc:entity:xmlns:xml:catalog}"
    return {entry.get("name", ""): entry.get("uri", "") for entry in ET.parse(path).getroot().iter(f"{namespace}uri")}


@pytest.mark.parametrize("size", [1, 5, 100, 10_000])
def test_import_lines(size: int) -> None:
    """Check that `owl:imports` triples are found in N-Triples however it's split, and only as the predicate."""
    lines = [
        f"<{EX}o> <{OWL_IMPORTS}> <{EX}a> .",
        f"_:b0 <{OWL_IMPORTS}> <{EX}b> .",
        f"<{EX}x> <{EX}seeAlso> <{OWL_IMPORTS}> .",
        f'<{EX}o> <{OWL_IMPORTS}> "not an IRI" .',
        f"<{EX}o> <{OWL_IMPORTS}> <{EX}a> .",
    ]
    data = ("\n".join(lines) + "\n").encode()
    found: dict[str, None] = {}
    collect = _ImportLines(found)
    for start in range(0, len(data), size):
        collect(data[start : start + size])
    assert list(found) == [f"{EX}a", f"{EX}b"]


def test_import_lines_skip_long_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a line longer than `MAX_IMPORT_LINE_BYTES` is skipped, unheld, and the lines after it still read."""
    monkeypatch.setattr(browl.imports, "MAX_IMPORT_LINE_BYTES", 100)
    long_line = f"<{EX}o> <{OWL_IMPORTS}> <{EX}{'x' * 300}> .\n".encode()
    data = long_line + f"<{EX}o> <{OWL_IMPORTS}> <{EX}after> .\n".encode()
    found: dict[str, None] = {}
    collect = _ImportLines(found)
    for start in range(0, len(data), 64):
        collect(data[start : start + 64])
    assert list(found) == [f"{EX}after"]


async def test_rdf_imports(settings: Settings, tmp_path: Path) -> None:
    """Check that `riot` lists the imports of Turtle and RDF/XML, with relative IRIs resolved."""
    ttl = tmp_path / "o.ttl"
    ttl.write_text(turtle(f"{EX}o", f"{EX}a", "rel.owl"))
    found = await list_rdf_imports(ttl, Syntax.TURTLE, settings)
    assert found == [f"{EX}a", (tmp_path / "rel.owl").as_uri()]
    rdf = tmp_path / "o.rdf"
    rdf.write_text(
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:owl="http://www.w3.org/2002/07/owl#">'
        f'<owl:Ontology rdf:about="{EX}o"><owl:imports rdf:resource="{EX}b"/></owl:Ontology></rdf:RDF>'
    )
    assert await list_rdf_imports(rdf, Syntax.RDF_XML, settings) == [f"{EX}b"]


async def test_rdf_parse_error(settings: Settings, tmp_path: Path) -> None:
    """Check that a document Jena can't parse fails, quoting Jena's error."""
    path = tmp_path / "o.ttl"
    path.write_text("<a> <b> .\n")
    with pytest.raises(StageError, match=r"^parse error: Jena couldn't read o.ttl as Turtle:\n.*"):
        await list_rdf_imports(path, Syntax.TURTLE, settings)


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:8.8.8.8", True),
        ("2002:7f00:1::", False),  # 6to4 for 127.0.0.1
        ("2002:0808:0808::", False),  # 6to4 isn't globally reachable, whatever it embeds
    ],
)
def test_is_public(address: str, public: bool) -> None:
    """Check which addresses imports may be fetched from."""
    assert is_public(ipaddress.ip_address(address)) is public


@pytest.mark.parametrize(
    ("iri", "names"),
    [
        (f"{EX}a", [f"{EX}a"]),
        ("file:///x/y.owl", ["file:///x/y.owl", "file:/x/y.owl", "file://localhost/x/y.owl"]),
        ("file:/x/y.owl", ["file:/x/y.owl", "file:///x/y.owl", "file://localhost/x/y.owl"]),
        ("y.owl", ["y.owl"]),
    ],
)
def test_catalog_names(iri: str, names: list[str]) -> None:
    """Check that a file IRI is listed in every spelling the OWLAPI or Jena may give it."""
    assert catalog_names(iri) == names


def test_empty_ontology() -> None:
    """Check that a dropped import's empty ontology keeps its IRI, unless the IRI can't be written."""
    assert empty_ontology(f"{EX}a") == f"Ontology(<{EX}a>)\n"
    assert empty_ontology(f"{EX}a b") == "Ontology()\n"


async def test_fetch(site: Site, tmp_path: Path, data_dir: Path) -> None:
    """Check that an import is fetched and its syntax found, from its content or else its content type."""
    settings = make_settings()
    async with fetch_client(allow_all) as client:
        url = site.page("/a", "<?xml version='1.0'?><rdf:RDF/>", "application/rdf+xml")
        assert await fetch(client, url, tmp_path / "a", settings) == Syntax.RDF_XML
        assert (tmp_path / "a").read_text().startswith("<?xml")
        url = site.page("/b", "ex:a ex:b ex:c .", "text/turtle; charset=utf-8")
        assert await fetch(client, url, tmp_path / "b", settings) == Syntax.TURTLE
        site.pages["/r"] = (302, {"Location": "/a"}, b"")
        assert await fetch(client, site.url + "/r", tmp_path / "r", settings) == Syntax.RDF_XML


@pytest.mark.parametrize(
    ("iri", "page", "match"),
    [
        ("file:///etc/passwd", None, "only http and https imports are fetched"),
        ("ftp://example.org/a", None, "only http and https imports are fetched"),
        ("https://exaémple..com/", None, "not a valid URL: Invalid IDNA hostname"),
        ("http://[::1/", None, "not a valid URL"),
        ("/missing", None, "HTTP 404 from"),
        ("/ofx", ("<Ontology ontologyIRI='x'/>", "application/owl+xml"), r"not in a syntax.*: OWL/XML"),
        ("/zip", (b"\x1f\x8b\x08 compressed", "application/gzip"), r"not in a syntax.*: compressed"),
        ("/plain", ("format-version: 1.2", "text/plain"), r"not in a syntax.*: unknown"),
        ("/big", ("x" * 100, "text/turtle"), "larger than 50 bytes"),
    ],
)
async def test_fetch_refused(
    site: Site, tmp_path: Path, data_dir: Path, iri: str, page: tuple[str | bytes, str] | None, match: str
) -> None:
    """Check that an import that isn't allowed, can't be fetched, or isn't readable fails, leaving no file."""
    if page is not None:
        site.page(iri, *page)
    if iri.startswith("/"):
        iri = site.url + iri
    async with fetch_client(allow_all) as client:
        with pytest.raises(FetchError, match=match):
            await fetch(client, iri, tmp_path / "f", make_settings(MAX_UPLOAD_BYTES=50))
    assert not (tmp_path / "f").exists()


async def test_private_address_refused(site: Site, tmp_path: Path, data_dir: Path) -> None:
    """Check that by default nothing is fetched from a loopback address: the check runs before connecting."""
    url = site.page("/a", "ex:a ex:b ex:c .")
    async with fetch_client() as client:
        with pytest.raises(FetchError, match=r"127\.0\.0\.1 has no public address"):
            await fetch(client, url, tmp_path / "a", make_settings())
    assert site.requested == []


async def test_redirect_checked(site: Site, tmp_path: Path, data_dir: Path) -> None:
    """Check that each redirect's address is checked too, before connecting to it."""
    checked: list[str] = []

    def first_only(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        checked.append(str(address))
        return len(checked) == 1

    port = site.url.rpartition(":")[2]
    site.pages["/r"] = (302, {"Location": f"http://localhost:{port}/a"}, b"")
    site.page("/a", "ex:a ex:b ex:c .")
    async with fetch_client(first_only) as client:
        with pytest.raises(FetchError, match="localhost has no public address"):
            await fetch(client, site.url + "/r", tmp_path / "a", make_settings())
    assert site.requested == ["/r"]


async def test_stage(site: Site, settings: Settings, data_dir: Path) -> None:
    """Check that imports are fetched recursively, the unfetchable dropped, and every one is in the catalog."""
    a, b = site.url + "/a.ttl", site.url + "/b.rdf"
    site.page("/a.ttl", turtle(a, b, "file:///etc/passwd", a))
    site.page(
        "/b.rdf",
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:owl="http://www.w3.org/2002/07/owl#">'
        f'<owl:Ontology rdf:about="{b}"><owl:imports rdf:resource="{a}"/></owl:Ontology></rdf:RDF>',
        "application/rdf+xml",
    )
    missing = site.url + "/missing.ttl"
    upload = store_upload(data_dir, turtle(f"{EX}o", a, missing))
    context = JobContext(1, settings, upload=upload)
    await imports_stage(context, allow_all)
    assert context.imports == [
        ImportResult(a),
        ImportResult(missing, reason=f"HTTP 404 from {missing}"),
        ImportResult(b, a),
        ImportResult("file:///etc/passwd", a, "only http and https imports are fetched"),
    ]
    assert sorted(site.requested) == ["/a.ttl", "/b.rdf", "/missing.ttl"]
    imports_dir = upload.parent / "imports"
    assert context.import_files == {a: imports_dir / "1.ttl", b: imports_dir / "3.rdf"}
    assert context.catalog == upload.parent / "catalog.xml"
    entries = catalog_map(context.catalog)
    assert entries[a] == (imports_dir / "1.ttl").resolve().as_uri()
    assert entries[b] == (imports_dir / "3.rdf").resolve().as_uri()
    for dropped in (missing, "file:///etc/passwd", "file:/etc/passwd", "file://localhost/etc/passwd"):
        assert Path(entries[dropped].removeprefix("file://")).read_text().startswith("Ontology(")


async def test_stage_parse_error(settings: Settings, data_dir: Path) -> None:
    """Check that an upload that can't be parsed fails the stage."""
    context = JobContext(1, settings, upload=store_upload(data_dir, "<a> <b> .\n"))
    with pytest.raises(StageError, match=r"^parse error"):
        await imports_stage(context, allow_all)


async def test_stage_unreadable_import(site: Site, settings: Settings, data_dir: Path) -> None:
    """Check that an import that can't be parsed is dropped, not loaded."""
    bad = site.page("/bad.ttl", "<a> <b> .\n")
    context = JobContext(1, settings, upload=store_upload(data_dir, turtle(f"{EX}o", bad)))
    await imports_stage(context, allow_all)
    [result] = context.imports
    assert result.dropped and result.reason is not None and result.reason.startswith("parse error")
    assert context.import_files == {}
    assert sorted(path.name for path in (data_dir / "uploads" / "1" / "imports").iterdir()) == ["empty-0.ofn"]


async def test_stage_import_limit(
    site: Site, settings: Settings, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that imports past `MAX_IMPORTS` attempts, failed ones too, are dropped without being fetched."""
    monkeypatch.setattr(browl.imports, "MAX_IMPORTS", 2)
    first, second = site.url + "/missing.ttl", site.page("/2.ttl", turtle("urn:2"))
    third = site.page("/3.ttl", turtle("urn:3"))
    context = JobContext(1, settings, upload=store_upload(data_dir, turtle(f"{EX}o", first, second, third)))
    await imports_stage(context, allow_all)
    reasons = [result.reason for result in context.imports]
    assert reasons[1:] == [None, "more than 2 imports"]
    assert reasons[0] is not None and reasons[0].startswith("HTTP 404")
    assert site.requested == ["/missing.ttl", "/2.ttl"]


async def test_stage_listed_limit(
    site: Site, settings: Settings, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a load whose documents list more than `MAX_LISTED` imports fails."""
    monkeypatch.setattr(browl.imports, "MAX_LISTED", 2)
    context = JobContext(1, settings, upload=store_upload(data_dir, turtle(f"{EX}o", "urn:a", "urn:b", "urn:c")))
    with pytest.raises(StageError, match=r"^the ontology and its imports list more than 2 imports$"):
        await imports_stage(context, allow_all)
    assert site.requested == []


async def test_stage_byte_budget(site: Site, data_dir: Path, jena_home: Path) -> None:
    """Check that the imports together may be no larger than `MAX_UPLOAD_BYTES`."""
    first = site.page("/1.ttl", turtle("urn:1"))
    second = site.page("/2.ttl", turtle("urn:2"))
    size = len(turtle("urn:1").encode())
    settings = make_settings(JENA_HOME=str(jena_home), MAX_UPLOAD_BYTES=str(size + 20))
    context = JobContext(1, settings, upload=store_upload(data_dir, turtle(f"{EX}o", first, second)))
    await imports_stage(context, allow_all)
    assert [result.reason for result in context.imports] == [
        None,
        "larger than the 20 bytes left of MAX_UPLOAD_BYTES for all imports together",
    ]


async def test_stage_deadline(site: Site, settings: Settings, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that imports not fetched within `FETCH_ALL_TIMEOUT_S` are dropped."""
    monkeypatch.setattr(browl.imports, "FETCH_ALL_TIMEOUT_S", 0.0)
    context = JobContext(1, settings, upload=store_upload(data_dir, turtle(f"{EX}o", site.url + "/1.ttl")))
    await imports_stage(context, allow_all)
    assert [result.reason for result in context.imports] == ["fetching all imports took longer than 0 s"]
    assert site.requested == []


async def test_stage_no_upload(settings: Settings) -> None:
    """Check that a job with nothing uploaded has no imports."""
    context = JobContext(1, settings)
    await imports_stage(context)
    assert context.catalog is None


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_ofn_import_converted(site: Site, settings: Settings, data_dir: Path) -> None:
    """Check that an import in OWL Functional Syntax is converted to Turtle, for the loader, and its imports listed."""
    nested = site.page("/n.ttl", turtle(site.url + "/n.ttl"))
    ofn = site.page("/o.ofn", f"Ontology(<{site.url}/o.ofn>\nImport(<{nested}>)\nDeclaration(Class(<{EX}A>))\n)\n")
    context = JobContext(1, settings, upload=store_upload(data_dir, turtle(f"{EX}o", ofn)))
    await imports_stage(context, allow_all)
    assert [result.iri for result in context.imports] == [ofn, nested]
    assert site.requested == ["/o.ofn", "/n.ttl"]
    converted = context.import_files[ofn]
    assert converted.suffix == ".ttl"
    assert f"<{EX}A>" in converted.read_text()


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
@pytest.mark.parametrize("reasoner", [Reasoner.ELK, Reasoner.NONE])
async def test_ofn_upload_resolves_no_import(
    http_server: tuple[str, list[str]], settings: Settings, data_dir: Path, tmp_path: Path, reasoner: Reasoner
) -> None:
    """Check that neither the OFN conversion nor ROBOT, given the catalog, fetches an import or reads a local one.

    The local file imports a page of the local server, which would be requested if it were read. The relative import
    can't be fetched, and is left out of the converted Turtle, so ROBOT doesn't resolve it against the Turtle's base.
    """
    url, requested = http_server
    secret = tmp_path / "secret.ttl"
    secret.write_text(turtle(f"{EX}secret", f"{url}/from-secret"))
    imports = "".join(f"Import(<{iri}>)\n" for iri in (f"{url}/a.owl", secret.as_uri(), "relative.owl"))
    upload = store_upload(data_dir, f"Ontology(<{EX}o>\n{imports}Declaration(Class(<{EX}A>))\n)\n", ".ofn")
    context = JobContext(1, settings, reasoner, upload)
    await imports_stage(context)  # the default check refuses the local server
    assert sorted(result.iri for result in context.imports) == sorted([f"{url}/a.owl", secret.as_uri(), "relative.owl"])
    assert all(result.dropped for result in context.imports)
    assert context.converted == upload.with_name(browl.imports.CONVERTED)
    assert "relative.owl" not in context.converted.read_text()
    await reasoning_stage(context)
    assert (context.reasoned is not None) == (reasoner is not Reasoner.NONE)
    assert context.ontology == (context.reasoned or context.converted)
    assert requested == []


def test_status_shows_dropped_imports(data_dir: Path) -> None:
    """Check that the status lists the imports, the dropped ones marked as failures, and the triples loaded."""
    state = JobState(
        generation=1,
        stage=Stage.READY,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        ready_generation=1,
        imports=(ImportResult(f"{EX}a", triples=5), ImportResult("file:///x", f"{EX}a", "only http and https")),
        triples=1234,
    )
    JobStore(data_dir / STATE_DB).write(state)
    with TestClient(make_app()) as client:
        page = client.get("/_browl/admin/status").text
    assert "1,234 triples loaded, not counting imports." in page
    assert (
        "1 of the ontology's 2 imports\n    couldn't be loaded</strong>, so the site leaves out what it declares."
        in page
    )
    assert f'<li class="failure"><code>file:///x</code> (imported by <code>{EX}a</code>):' in page
    assert "only http and https" in page
    assert "5 triples" in page
