"""Tests for the Usage tab (P-27b): its order, pages, axioms, label and failures, on the test ontology in Fuseki."""

import asyncio
import html
import math
import re
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import closing

import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import browl.fuseki
import browl.lookup
import browl.owlapi
import browl.precache.stage
import browl.usage_tab
import browl.usages
from browl import owl, owlapi
from browl.axioms import convert_axioms
from browl.jobs import JobContext, Stage
from browl.loading import dataset_name
from browl.owlapi import ntriples_ontology
from browl.precache.stage import pre_cache
from browl.rdf import IRI, Literal
from browl.settings import Settings
from browl.usage_tab import (
    UsageGroup,
    UsageTab,
    read_usage_page,
    sorted_usages,
    usage_label,
)
from browl.usages import Usage, UsageSource, UsageStore, read_usages
from tests.helpers import connect, fuseki_dataset, loaded, make_app, make_settings, ontology_ntriples
from tests.test_usages import usage_triples

pytestmark = [pytest.mark.anyio]

NS = "http://example.org/axioms#"
PANEL = {"path": "/axioms", "tab": "usage", "panel": "1"}
"""The query of a Usage tab panel alone on the test ontology's pages, but the fragment."""


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the module's async tests and fixtures on asyncio, as the module-scoped fixtures need one backend."""
    return "asyncio"


@pytest.fixture(scope="module")
def ntriples(jvm: None) -> bytes:
    """Return the test ontology as N-Triples."""
    return asyncio.run(owlapi.run_java(ontology_ntriples))


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """Yield settings with a data directory of the module's own, as `DATA_DIR` too."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATA_DIR", str(tmp_path_factory.mktemp("data")))
        yield make_settings()


@pytest.fixture(scope="module")
async def store(fuseki_url: str, ntriples: bytes, settings: Settings) -> AsyncIterator[UsageStore]:
    """Yield a store of the test ontology."""
    async with loaded(fuseki_url, settings, {None: ntriples}, "") as found:
        yield found


def entity_iris(settings: Settings, store: UsageStore) -> list[str]:
    """Return the IRIs of the store's entities."""
    with closing(connect(settings, store.generation)) as connection:
        return sorted({iri for (iri,) in connection.execute("SELECT iri FROM entity")})


def lone_axiom(ntriples: bytes) -> str:
    """Return the text of the one axiom a usage's triples parse as, but declarations, without annotations."""
    with ntriples_ontology(ntriples) as ontology:
        found = [
            axiom.getAxiomWithoutAnnotations()
            for axiom in ontology.getAxioms()
            if str(axiom.getAxiomType().getName()) != "Declaration"
        ]
    [node] = convert_axioms(found)
    return node.manchester


@pytest.mark.fuseki
@pytest.mark.jvm
@pytest.mark.parametrize("size", [1, 3, 50])
async def test_every_usage_once(
    store: UsageStore, settings: Settings, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    """Check that every usage of every entity is on exactly one page, as the axiom it is alone, under its subject.

    Each page but the last holds exactly `PAGE_USAGES` axioms, one per usage, a property's too.
    """
    monkeypatch.setattr(browl.usage_tab, "PAGE_USAGES", size)
    for iri in entity_iris(settings, store):
        usages = await read_usages(store, iri)
        ordered = await store.derived.read(store.generation, lambda c, found=usages: sorted_usages(c, found))
        expected: Counter[tuple[str | None, str]] = Counter()
        for usage in usages:
            text = await owlapi.run_java(lone_axiom, await usage_triples(store, iri, usage))
            expected[usage.subject, text] += 1
        found: Counter[tuple[str | None, str]] = Counter()
        pages = max(1, math.ceil(len(usages) / size))
        for page in range(1, pages + 1):
            chunk = ordered[(page - 1) * size : page * size]
            groups = await read_usage_page(store, iri, ordered, page)
            assert [group.subject for group in groups] == list(dict.fromkeys(usage.subject for usage in chunk))
            for group in groups:
                on_page = sum(usage.subject == group.subject for usage in chunk)
                assert len(group.axioms) == on_page, (iri, page, group)
                found.update((group.subject, axiom.without_annotations().manchester) for axiom in group.axioms)
        assert found == expected, iri


@pytest.fixture(scope="module")
def client(fuseki_url: str, ntriples: bytes, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """Yield a test client for an app serving the test ontology from Fuseki, with its derived database pre-cached."""
    generations: list[int] = []  # the app's, whose dataset is the test's; other stores keep theirs

    def name_of(generation: int) -> str:
        return name if generation in generations else dataset_name(generation)

    with pytest.MonkeyPatch.context() as patch, fuseki_dataset(fuseki_url, {None: ntriples}) as name:
        patch.setenv("DATA_DIR", str(tmp_path_factory.mktemp("app")))
        for module in (browl.precache.stage, browl.usages):
            patch.setattr(module, "dataset_name", name_of)
        app = make_app(FUSEKI_URL=fuseki_url)

        async def stage(context: JobContext) -> None:
            generations.append(context.generation)
            async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
                await pre_cache(context, http)

        with TestClient(app) as test_client:
            app.state.jobs.register(Stage.PRE_CACHING, stage)
            test_client.portal.call(app.state.jobs.start)
            test_client.portal.call(app.state.jobs.wait)
            yield test_client


def panel(client: TestClient, name: str, **params: str) -> httpx2.Response:
    """Return the Usage tab panel of the test ontology's entity with this local name."""
    return client.get("/_browl/hash", params={**PANEL, "fragment": name, **params})


def test_order() -> None:
    """Check the tab's order: by subject's display name, case-folded, then IRI; no subject; subjects not in `entity`.

    A punned IRI sorts by its first entity's name, and one without a display name by its IRI. A subject's usages keep
    their order whatever order they're read in.
    """
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE entity (id INTEGER PRIMARY KEY, iri TEXT, display_name TEXT)")
        rows = [
            (1, f"{NS}b", "beta"),
            (2, f"{NS}a", "Beta"),  # ties with b's casefolded name, so the IRI decides
            (3, f"{NS}z", "alpha"),
            (4, f"{NS}pun", "Gamma"),
            (5, f"{NS}pun", "aardvark"),  # the pun's second entity: not its sort name
            (6, f"{NS}m", None),  # sorts by its IRI, after the names
        ]
        connection.executemany("INSERT INTO entity VALUES (?, ?, ?)", rows)
        names = ["z", "a", "b", "pun", "m"]
        usages = [Usage(UsageSource.OBJECT, IRI(f"{NS}{name}"), predicate=IRI(f"{NS}p")) for name in names]
        usages.append(Usage(UsageSource.PREDICATE, IRI(f"{NS}a"), object=Literal("1")))
        usages.append(Usage(UsageSource.PREDICATE, IRI(f"{NS}a"), object=IRI(f"{NS}v")))
        usages.append(Usage(UsageSource.OBJECT, IRI("http://other.org/x"), predicate=IRI(f"{NS}p")))
        usages.append(Usage(UsageSource.OBJECT, IRI("http://another.org/x"), predicate=IRI(f"{NS}p")))
        usages.append(Usage(UsageSource.OBJECT, None))
        ordered = sorted_usages(connection, usages)
        assert sorted_usages(connection, reversed(usages)) == ordered
    subjects = [usage.subject and usage.subject.removeprefix(NS) for usage in ordered]
    assert subjects == ["z", "a", "a", "a", "b", "pun", "m", None, "http://another.org/x", "http://other.org/x"]
    assert [usage.source for usage in ordered[1:4]] == ["object", "predicate", "predicate"]


def subjects_shown(page: str) -> list[str]:
    """Return the text of a Usage tab panel's subject headings, in order, without their markup."""
    found = re.findall(r"<dt>(.*?)</dt>", page, re.DOTALL)
    return [html.unescape(re.sub(r"<[^>]+>", "", heading)).strip() for heading in found]


def lines_shown(page: str) -> list[str]:
    """Return the text of a Usage tab panel's axiom lines, in order, without their annotations or markup."""
    lines = re.findall(r'<dd dir="ltr">(.*?)(?:<ul class="axiom-annotations">|</dd>)', page, re.DOTALL)
    return [html.unescape(re.sub(r"<[^>]+>", "", line)).replace(NS, ":") for line in lines]


def annotations_shown(page: str) -> list[str]:
    """Return a panel's annotation lists' items, nested ones by depth (`>`), without markup, in order."""
    found = []
    depth = 0
    for token in re.findall(r'<ul class="axiom-annotations">|</ul>|<li>.*?(?=<ul|</li>)', page, re.DOTALL):
        if token.startswith("<ul"):
            depth += 1
        elif token == "</ul>":
            depth -= 1
        else:
            text = html.unescape(re.sub(r"<[^>]+>", "", token)).replace(NS, ":").strip()
            found.append(">" * (depth - 1) + text)
    return found


@pytest.mark.fuseki
@pytest.mark.jvm
def test_subjects(client: TestClient) -> None:
    """Check a class's and a property's subjects: by name, then those with no single subject, then non-entities."""
    dog = panel(client, "Dog").text
    assert subjects_shown(dog) == [f"{NS}Cat", f"{NS}Pet", "No single subject", f"{NS}rex"]
    assert lines_shown(dog) == [
        ":Cat :remark :Dog",  # an annotation assertion, by kind and text before Cat's other axiom
        ":Cat DisjointWith :Dog",
        ":Pet SubClassOf :Cat or :Dog",
        "DLSafeRule(Body(ClassAtom(<:Dog> Variable(<:x>))) Head(ClassAtom(<:Animal> Variable(<:x>))))",
        "<:rex> Type :Dog",
    ]
    assert '<p class="usage-count">5 usages</p>' in dog
    age = panel(client, "age").text
    assert subjects_shown(age) == [f"{NS}Person", "No single subject", f"{NS}alice"]
    assert lines_shown(age)[2:] == ["not (<:bob> :age 7)", "<:alice> :age 42"]
    # By display name, case-folded: Heart's and Tricky's names start with "<b>" and "a <script>".
    assert subjects_shown(panel(client, "Animal").text)[:3] == [
        '<b>Heart</b> & "soul"',
        "a <script>alert(1)</script> 'quote'",
        f"{NS}Cat",
    ]
    has_part = panel(client, "hasPart").text
    assert subjects_shown(has_part) == ["No single subject"]
    assert len(lines_shown(has_part)) == 2  # its two general class axioms


@pytest.mark.fuseki
@pytest.mark.jvm
def test_annotations(client: TestClient) -> None:
    """Check that an axiom shows its annotations only when the usage is found through them alone, nested by depth."""
    assert annotations_shown(panel(client, "Fungus").text) == [":remark :Fungus"]  # on Tricky SubClassOf Animal
    plant = panel(client, "Plant").text
    assert annotations_shown(plant) == [':note "nested"', ">:remark :Plant"]
    assert lines_shown(plant)[1] == ":Liver SubClassOf :Organ"
    # On Cat's and Liver's axioms, the general class axiom, and the n-ary DisjointClasses.
    assert annotations_shown(panel(client, "note").text) == [
        ':note "annotated"',
        ':note "nested"',
        ">:remark :Plant",
        ':note "n-ary"',
        ":remark :Mineral",
        ':note "general"',
    ]
    animal = panel(client, "Animal").text  # Cat SubClassOf Animal is annotated, but it's a usage by its own terms
    assert ":Cat SubClassOf :Animal" in lines_shown(animal)
    assert "axiom-annotations" not in animal


@pytest.mark.fuseki
@pytest.mark.jvm
def test_label(client: TestClient) -> None:
    """Check the Usage tab's label: fetched after an Overview renders, inline when the Usage tab is shown."""
    count = f"/_browl/usage-count?iri={NS.replace(':', '%3A').replace('/', '%2F').replace('#', '%23')}Dog"
    overview = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Dog"}).text
    assert f'<span\n      hx-get="{count}" hx-trigger="load" hx-target="this"' in overview
    response = client.get(count)
    assert (response.status_code, response.text, response.headers["X-Robots-Tag"]) == (200, "Usage (5)", "noindex")
    usage = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Dog", "tab": "usage"}).text
    assert "usage-count?" not in usage
    assert 'hx-sync="closest [role=tablist]:replace">Usage (5)</a>' in usage
    assert client.get("/_browl/usage-count", params={"iri": f"{NS}nothing"}).status_code == 404
    assert client.get("/_browl/usage-count").status_code == 404


@pytest.mark.fuseki
@pytest.mark.jvm
def test_pages(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the paging: a subject split across pages marked "(continued)", links to the pages around, and a form."""
    monkeypatch.setattr(browl.usage_tab, "PAGE_USAGES", 1)
    first, second, last = (panel(client, "Dog", **({"page": page} if page else {})).text for page in ("", "2", "5"))
    assert subjects_shown(first) == [f"{NS}Cat"]
    assert subjects_shown(second) == [f"{NS}Cat (continued)"]
    assert '<small class="continued">(continued)</small>' in second
    assert "page 2 of 5" in second
    assert '<a href="/axioms?tab=usage#Dog" rel="prev">Previous</a>' in second
    assert '<a href="/axioms?tab=usage&amp;page=3#Dog" rel="next">Next</a>' in second
    assert 'rel="prev"' not in first and 'rel="next"' not in last
    assert '<form method="get" action="/axioms#Dog">' in first
    assert '<input type="hidden" name="tab" value="usage">' in first
    assert 'name="page" min="1" max="5" value="1"' in first
    assert subjects_shown(last) == [f"{NS}rex"]
    by_iri = client.get("/_browl/iri", params={"iri": f"{NS}rex", "tab": "usage"})  # not an entity
    assert by_iri.status_code == 404
    params = {"iri": f"{NS}Pet", "tab": "usage", "page": "2"}
    fallback = client.get("/_browl/iri", params=params, follow_redirects=False)
    assert fallback.status_code == 302
    assert fallback.headers["Location"] == "/axioms?tab=usage&page=2#Pet"


@pytest.mark.fuseki
@pytest.mark.jvm
@pytest.mark.parametrize("page", ["0", "6", "-1", "01", "1.5", "two", "", "1e1"])
def test_page_not_found(client: TestClient, monkeypatch: pytest.MonkeyPatch, page: str) -> None:
    """Check that a page that isn't a positive integer without leading zeros, or is past the last, is a 404."""
    monkeypatch.setattr(browl.usage_tab, "PAGE_USAGES", 1)
    assert panel(client, "Dog", page=page).status_code == 404
    assert client.get("/_browl/hash", params={**PANEL, "fragment": "Dog", "page": ["1", "1"]}).status_code == 404


@pytest.mark.fuseki
@pytest.mark.jvm
def test_one_page(client: TestClient) -> None:
    """Check that a tab of one page has no paging, and an explicit first page is the same."""
    page = panel(client, "Dog").text
    assert '<nav class="pager"' not in page
    assert panel(client, "Dog", page="1").text == page
    assert panel(client, "Dog", page="2").status_code == 404


@pytest.mark.fuseki
@pytest.mark.jvm
def test_preparing(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that an entity with more usages than streamed shows its count and "Preparing…", without reading them."""
    monkeypatch.setattr(browl.usage_tab, "MAX_STREAMED_USAGES", 4)

    async def never(*args: object) -> None:
        raise AssertionError("read the usages")

    with monkeypatch.context() as patch:
        patch.setattr(browl.usage_tab, "read_usages", never)
        page = panel(client, "Animal").text
    assert '<p class="note" id="usage-preparing">10 usages. Preparing…</p>' in page
    assert "<dl" not in page
    assert panel(client, "Animal", page="1").text == page

    async def three(*args: object) -> int:
        return 3

    monkeypatch.setattr(browl.usage_tab, "count_usages", three)  # fewer than it lists, which can't happen
    assert '<p class="note" id="usage-preparing">3 usages. Preparing…</p>' in panel(client, "Animal").text
    assert "Preparing the usages of http://example.org/axioms#Animal" in caplog.text


@pytest.mark.fuseki
@pytest.mark.jvm
def test_no_usages(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an entity with no usages says so."""

    async def none(*args: object) -> int:
        return 0

    monkeypatch.setattr(browl.usage_tab, "count_usages", none)
    page = panel(client, "Dog").text
    assert '<p class="note">None.</p>' in page
    assert "<dl" not in page


MESSAGE = "The usages can't be shown just now."


@pytest.mark.fuseki
@pytest.mark.jvm
@pytest.mark.parametrize("failing", ["count_usages", "read_usages", "fetch_usage_fragments", "usage_axioms"])
def test_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failing: str
) -> None:
    """Check that a failed count, read, fetch or parse fails only the tab's content, with a message, and is logged."""

    def fail(*args: object) -> None:
        error = browl.owlapi.FragmentError if failing == "usage_axioms" else browl.fuseki.SparqlError
        raise error("no luck")

    async def fail_async(*args: object) -> None:
        fail()

    monkeypatch.setattr(browl.usage_tab, failing, fail if failing == "usage_axioms" else fail_async)
    response = panel(client, "Dog")
    assert response.status_code == 200
    assert MESSAGE in response.text
    assert ("5 usages" in response.text) == (failing != "count_usages")
    [record] = [record for record in caplog.records if record.name == "browl.usage_tab"]
    assert record.levelname == "ERROR"
    assert record.exc_info is not None
    assert "no luck" in str(record.exc_info[1])
    if failing == "count_usages":
        count = client.get("/_browl/usage-count", params={"iri": f"{NS}Dog"})
        assert (count.status_code, count.text) == (200, "Usage")


@pytest.mark.fuseki
@pytest.mark.jvm
def test_too_many_iris(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a page whose axioms mention too many IRIs says so, rather than fetch, parse and link them all."""
    monkeypatch.setattr(browl.usage_tab, "MAX_FRAGMENT_IRIS", 2)
    page = panel(client, "Dog").text
    assert "This page's usages mention too many entities to show here." in page
    assert "mentions" in caplog.text and "more than 2" in caplog.text


def test_tab_json() -> None:
    """Check that a page of the tab round-trips through JSON, its axioms with their fields and annotations."""
    note = owl.Entity(kind=owl.EntityKind.ANNOTATION_PROPERTY, iri=f"{NS}note")
    cat, animal = (owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{NS}{name}") for name in ("Cat", "Animal"))
    value = owl.Literal(lexical="x", datatype="http://www.w3.org/2001/XMLSchema#string")
    annotated = owl.SubClassOf(sub=cat, super=animal, annotations=(owl.Annotation(property=note, value=value),))
    assertion = owl.AnnotationAssertion(subject=owl.IRIReference(iri=cat.iri), property=note, value=value)
    tab = UsageTab(
        count=2,
        page=1,
        pages=1,
        groups=[UsageGroup(subject=IRI(cat.iri), continued=True, axioms=[annotated, assertion])],
    )
    assert UsageTab.model_validate_json(tab.model_dump_json()) == tab
    assert usage_label(None) == "Usage"
    assert usage_label(12345) == "Usage (12,345)"


def test_pager_fallback_url() -> None:
    """Check that the paging of a page at a URL with a query, a fallback URL, keeps that query in its form's fields."""
    request = Request({"type": "http", "root_path": "/root", "headers": []})
    at = browl.lookup._TabUrls("/_browl/iri?iri=urn%3Aexample%3Ax")
    tab = UsageTab(count=120, page=2, pages=3, groups=[])
    pager = browl.lookup._pager(request, at, tab)
    assert pager is not None
    assert pager.previous == "/root/_browl/iri?iri=urn%3Aexample%3Ax&tab=usage"
    assert pager.next == "/root/_browl/iri?iri=urn%3Aexample%3Ax&tab=usage&page=3"
    assert (pager.action, pager.fields) == ("/root/_browl/iri", [("iri", "urn:example:x"), ("tab", "usage")])
    assert browl.lookup._pager(request, at, UsageTab(count=50, page=1, pages=1, groups=[])) is None
    assert browl.lookup._pager(request, at, UsageTab(count=None, page=1, pages=None, groups=None)) is None


OWL_NS = "http://www.w3.org/2002/07/owl#"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
SUBCLASS = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"
CLASSES = "".join(f"<{NS}{name}> {RDF_TYPE} <{OWL_NS}Class> .\n" for name in "ABCDE")
"""Declarations of classes A to E."""


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
def test_shared_and_unclaimed(caplog: pytest.LogCaptureFixture) -> None:
    """Check that an axiom about two subjects goes to the first that lacks one, and one about none goes with no subject.

    That one is logged, and a fragment the OWLAPI can't parse fails.
    """
    triples = (
        f"{CLASSES}<{NS}A> <{OWL_NS}disjointWith> <{NS}B> .\n<{NS}B> {SUBCLASS} <{NS}E> .\n"
        f"<{NS}C> {SUBCLASS} <{NS}E> .\n"  # about C, which isn't a subject here
    ).encode()
    general = f"{CLASSES}<{NS}D> {SUBCLASS} <{NS}E> .\n".encode()
    subjects = [(f"{NS}A", 1), (f"{NS}B", 1)]
    by_subject, rest = asyncio.run(owlapi.run_java(browl.usage_tab.usage_axioms, triples, general, f"{NS}E", subjects))
    texts = {subject: [axiom.manchester.replace(NS, "") for axiom in axioms] for subject, axioms in by_subject.items()}
    assert texts == {f"{NS}A": ["<A> DisjointWith <B>"], f"{NS}B": ["<B> SubClassOf <E>"]}
    assert [axiom.manchester.replace(NS, "") for axiom in rest] == ["<C> SubClassOf <E>", "<D> SubClassOf <E>"]
    assert "Showing 1 axioms of the usages of http://example.org/axioms#E with no subject" in caplog.text
    with pytest.raises(browl.owlapi.FragmentError, match="couldn't parse"):
        asyncio.run(owlapi.run_java(browl.usage_tab.usage_axioms, b"not N-Triples", b"", f"{NS}E", subjects))


@pytest.mark.fuseki
@pytest.mark.jvm
async def test_blank_object(fuseki_url: str, settings: Settings) -> None:
    """Check a property's usage whose object is blank, an anonymous individual: its triple is fetched by its label."""
    remark = f"<{NS}remark>"
    triples = f"{CLASSES}{remark} {RDF_TYPE} <{OWL_NS}AnnotationProperty> .\n<{NS}A> {remark} _:x .\n"
    async with loaded(fuseki_url, settings, {None: triples.encode()}, "") as blank:
        [usage] = await read_usages(blank, f"{NS}remark")
        [group] = await read_usage_page(blank, f"{NS}remark", [usage], 1)
    assert group.subject == f"{NS}A"
    assert [axiom.manchester for axiom in group.axioms] == [f"<{NS}A> <{NS}remark> _:b1"]


@pytest.mark.fuseki
@pytest.mark.jvm
async def test_imports(fuseki_url: str, ntriples: bytes, settings: Settings, store: UsageStore) -> None:
    """Check that with some triples in a named graph, as an import's, read with the imports' clause, pages are the same.

    Blank nodes stay in the default graph, since each load names its own.
    """
    subjects = (f"<{NS}Dog> ", f"<{NS}Cat> ", f"<{NS}Liver> ", f"<{NS}Tricky> ", f"<{NS}remark> ")
    lines = ntriples.decode().splitlines(keepends=True)
    moved = [line.startswith(subjects) and " _:" not in line for line in lines]
    named = "".join(line for line, move in zip(lines, moved, strict=True) if move).encode()
    rest = "".join(line for line, move in zip(lines, moved, strict=True) if not move).encode()
    graphs = {None: rest, "http://example.org/imported": named}
    async with loaded(fuseki_url, settings, graphs, browl.fuseki.UNION_FROM) as split:
        for name in ("Animal", "Fungus", "Plant", "remark", "Dog", "note"):
            iri = f"{NS}{name}"
            pages = []
            for each in (store, split):
                usages = await read_usages(each, iri)
                ordered = await each.derived.read(each.generation, lambda c, found=usages: sorted_usages(c, found))
                pages.append(await read_usage_page(each, iri, ordered, 1))
            assert pages[0] == pages[1], name
            assert pages[0]
