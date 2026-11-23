"""Tests for the Usage tab (P-27b): its order, pages, axioms, label and failures, on the test ontology in Fuseki."""

import asyncio
import html
import math
import re
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import closing, contextmanager
from typing import Any

import httpx2
import jpype
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import browl.axioms
import browl.fuseki
import browl.lookup
import browl.owlapi
import browl.precache.stage
import browl.usage_tab
import browl.usage_tables
import browl.usages
from browl import owl, owlapi
from browl.axioms import axioms_about, convert_axioms, entity_axioms, fetch_fragment, is_declaration
from browl.frames import frame_groups
from browl.jobs import JobContext, Stage
from browl.links import EntityLink
from browl.loading import dataset_name
from browl.owlapi import ntriples_ontology
from browl.pages import templates
from browl.precache.stage import pre_cache
from browl.rdf import IRI
from browl.settings import Settings
from browl.usage_tab import (
    UsageGroup,
    UsageTab,
    fetch_usage_fragments,
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


def lone_axiom(ntriples: bytes) -> owl.AnyAxiom:
    """Return the one axiom a usage's triples parse as, but unannotated declarations, without annotations."""
    with ntriples_ontology(ntriples) as ontology:
        found = [
            axiom.getAxiomWithoutAnnotations()
            for axiom in ontology.getAxioms()
            if axiom.isAnnotated() or not is_declaration(axiom)
        ]
    [node] = convert_axioms(found)
    return node


@pytest.mark.fuseki
@pytest.mark.jvm
@pytest.mark.parametrize("size", [1, 3, 50])
async def test_every_usage_once(
    store: UsageStore, settings: Settings, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    """Check that every usage of every entity is on exactly one page, as the axiom it is alone, under its subject.

    Each page but the last holds exactly `PAGE_USAGES` axioms, one per usage, a property's too. A symmetric axiom
    between two is written with its subject first.
    """
    monkeypatch.setattr(browl.usage_tab, "PAGE_USAGES", size)
    for iri in entity_iris(settings, store):
        usages = await read_usages(store, iri)
        ordered = await store.derived.read(store.generation, lambda c, found=usages: sorted_usages(c, found))
        expected: Counter[tuple[str | None, str]] = Counter()
        for usage in usages:
            axiom = await owlapi.run_java(lone_axiom, await usage_triples(store, iri, usage))
            subject = usage.subject
            expected[subject, (axiom if subject is None else axiom.with_first(subject)).manchester] += 1
        found: Counter[tuple[str | None, str]] = Counter()
        pages = max(1, math.ceil(len(usages) / size))
        for page in range(1, pages + 1):
            chunk = ordered[(page - 1) * size : page * size]
            continued = page > 1 and bool(chunk) and ordered[(page - 1) * size - 1].subject == chunk[0].subject
            groups = await read_usage_page(store, iri, chunk, continued)
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
        for module in (browl.precache.stage, browl.usages, browl.usage_tables):
            patch.setattr(module, "dataset_name", name_of)
        app = make_app(FUSEKI_URL=fuseki_url)  # its tests build the usage table when they need it (`usage_table`)

        async def stage(context: JobContext) -> None:
            generations.append(context.generation)
            async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
                await pre_cache(context, http)

        with TestClient(app) as test_client:
            app.state.jobs.register(Stage.PRE_CACHING, stage)
            test_client.portal.call(app.state.jobs.start)
            test_client.portal.call(app.state.jobs.wait)
            yield test_client


@contextmanager
def usage_table(client: TestClient) -> Iterator[None]:
    """Build the usage table of the client's app's generation, so its pages read it; then delete it."""
    app: Any = client.app
    tables, generation = app.state.usage_tables, app.state.jobs.state.ready_generation
    assert client.portal is not None
    client.portal.call(tables.start, generation, "")
    client.portal.call(tables.wait)
    assert not tables.failed(generation)
    try:
        yield
    finally:
        client.portal.call(tables.close)
        browl.usage_tables.table_path(app.state.settings.data_dir, generation).unlink()


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
        usages.append(Usage(UsageSource.PREDICATE, IRI(f"{NS}a"), index=1))
        usages.append(Usage(UsageSource.PREDICATE, IRI(f"{NS}a"), index=0))
        usages.append(Usage(UsageSource.OBJECT, IRI("http://other.org/x"), predicate=IRI(f"{NS}p")))
        usages.append(Usage(UsageSource.OBJECT, IRI("http://another.org/x"), predicate=IRI(f"{NS}p")))
        usages.append(Usage(UsageSource.OBJECT, None))
        ordered = sorted_usages(connection, usages)
        assert sorted_usages(connection, reversed(usages)) == ordered
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(browl.usage_tab, "LOOKUP_BATCH", 2)  # the names read in several queries
            assert sorted_usages(connection, usages) == ordered
    subjects = [usage.subject and usage.subject.removeprefix(NS) for usage in ordered]
    assert subjects == ["z", "a", "a", "a", "b", "pun", "m", None, "http://another.org/x", "http://other.org/x"]
    assert [usage.source for usage in ordered[1:4]] == ["object", "predicate", "predicate"]


def subjects_shown(page: str) -> list[str]:
    """Return the text of a Usage tab panel's subject headings, in order, without their markup."""
    found = re.findall(r"<dt>(.*?)</dt>", page, re.DOTALL)
    return [html.unescape(re.sub(r"<[^>]+>", "", heading)).strip() for heading in found]


LINE = re.compile(
    r'<dd dir="ltr" class="usage-line( usage-whole)?">(.*?)(?:<ul class="axiom-annotations">|</dd>)', re.DOTALL
)
"""A Usage tab line: whether it spans the keyword's and the rest's columns, and its markup, without annotations."""
SPLIT = re.compile(
    r'<span class="visually-hidden">(.*?)</span>\s*<span class="usage-keyword">(.*?)</span>\s*'
    r'<span class="usage-axiom">(.*)</span>\s*$',
    re.DOTALL,
)
"""A line in columns: the subject, for screen readers alone; the keyword; the rest."""


def plain(markup: str) -> str:
    """Return markup's text, as a screen reader reads it: without tags, spaces collapsed, the namespace `:`."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", markup)).split()).replace(NS, ":")


def lines_shown(page: str) -> list[str]:
    """Return the text of a Usage tab panel's axiom lines, in order, without their annotations or markup."""
    return [plain(line) for _, line in LINE.findall(page)]


def columns_shown(page: str) -> list[tuple[str, str] | str]:
    """Return a Usage tab panel's lines: each in columns as its keyword and the rest, or whole, as text."""
    found: list[tuple[str, str] | str] = []
    for whole, line in LINE.findall(page):
        split = SPLIT.match(line)
        assert (split is None) == bool(whole), line
        found.append(plain(line) if split is None else (plain(split[2]), plain(split[3])))
    return found


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
    """Check a class's and a property's subjects: by name, then those with no single subject, then non-entities.

    Each line reads, for a screen reader, as its whole axiom, its subject in text only it reads where the line is in
    columns.
    """
    dog = panel(client, "Dog").text
    assert subjects_shown(dog) == [f"{NS}Cat", f"{NS}Pet", "No single subject", f"{NS}rex"]
    assert lines_shown(dog) == [
        ":Cat :remark :Dog",  # an annotation assertion, by kind and text before Cat's other axiom
        ":Cat DisjointWith :Dog",
        ":Pet SubClassOf :Cat or :Dog",
        "DLSafeRule(Body(ClassAtom(<:Dog> Variable(<:x>))) Head(ClassAtom(<:Animal> Variable(<:x>))))",
        "<:rex> Type :Dog",
    ]
    assert columns_shown(dog) == [
        (":remark", ":Dog"),
        ("DisjointWith", ":Dog"),
        ("SubClassOf", ":Cat or :Dog"),
        "DLSafeRule(Body(ClassAtom(<:Dog> Variable(<:x>))) Head(ClassAtom(<:Animal> Variable(<:x>))))",
        ("Type", ":Dog"),
    ]
    assert "5 usages" not in plain(dog) and "usage-count" not in dog  # the count is in the tab's label only
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
    assert 'hx-sync="#tab-panel:replace">Usage (5)</a>' in usage
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
    assert 'value="2"\n        id="usage-page" required> of 5</label>' in second
    assert second.index('<nav class="pager"') < second.index('<dl class="frame usage-frame">')
    # Previous, the form and Next fetch panels from the hash route; its URLs are `test_pager_hash_route`'s.
    nav = second[second.index('<nav class="pager"') : second.index("</nav>")]
    assert 'data-panel="/_browl/hash?' in nav
    assert nav.count('hx-get="/_browl/hash?path=%2Faxioms&amp;fragment=Dog&amp;') == 3
    assert 'rel="prev"' not in first and 'rel="next"' not in last
    assert '<span class="pager-off" aria-disabled="true">Previous</span>' in first
    assert '<span class="pager-off" aria-disabled="true">Next</span>' in last
    assert "pager-off" not in second
    assert '<form method="get" action="/axioms#Dog" hx-get=' in first
    assert '<input type="hidden" name="tab" value="usage">' in first
    assert 'name="page" min="1" max="5" value="1"' in first
    assert subjects_shown(last) == [f"{NS}rex"]
    general = [panel(client, "hasPart", **({"page": page} if page else {})).text for page in ("", "2")]
    assert [subjects_shown(each) for each in general] == [["No single subject"], ["No single subject (continued)"]]
    by_iri = client.get("/_browl/iri", params={"iri": f"{NS}rex", "tab": "usage"})  # not an entity
    assert by_iri.status_code == 404
    params = {"iri": f"{NS}Pet", "tab": "usage", "page": "2"}
    fallback = client.get("/_browl/iri", params=params, follow_redirects=False)
    assert fallback.status_code == 302
    assert fallback.headers["Location"] == "/axioms?tab=usage&page=2#Pet"


@pytest.mark.fuseki
@pytest.mark.jvm
@pytest.mark.parametrize("page", ["0", "6", "-1", "01", "1.5", "two", "", "1e1", "999999999", "1000000000"])
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
    assert preparing(page) == ("10 usages. Preparing…", POLL)
    assert "<dl" not in page
    assert panel(client, "Animal", page="1").text == page

    async def three(*args: object) -> int:
        return 3

    monkeypatch.setattr(browl.usage_tab, "count_usages", three)  # fewer than it lists, which can't happen
    assert preparing(panel(client, "Animal").text) == ("3 usages. Preparing…", POLL)
    assert "Preparing the usages of http://example.org/axioms#Animal" in caplog.text


@pytest.mark.fuseki
@pytest.mark.jvm
def test_table_pages(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that once the usage table is ready, every entity's pages and label are those streamed before.

    An entity with more usages than are streamed is shown, not "Preparing…"; nothing is streamed or counted by Fuseki.
    """
    monkeypatch.setattr(browl.usage_tab, "PAGE_USAGES", 3)
    names = sorted({iri.removeprefix(NS) for iri in tab_iris(client) if iri.startswith(NS)})
    expected = {}
    for name in names:
        pages, page = [], 1
        while (response := panel(client, name, **({"page": str(page)} if page > 1 else {}))).status_code == 200:
            pages.append(response.text)
            page += 1
        count = client.get("/_browl/usage-count", params={"iri": f"{NS}{name}"}).text
        expected[name] = (pages, count)

    async def never(*args: object) -> None:
        raise AssertionError("streamed or counted by Fuseki")

    monkeypatch.setattr(browl.usage_tab, "read_usages", never)
    monkeypatch.setattr(browl.usages, "_corrections", never)
    monkeypatch.setattr(browl.usage_tab, "MAX_STREAMED_USAGES", 0)
    client.app.state.usage_counts = browl.usages.UsageCounts()  # type: ignore[attr-defined]
    with usage_table(client):
        for name, (pages, count) in expected.items():
            for number, page in enumerate(pages, 1):
                assert panel(client, name, **({"page": str(number)} if number > 1 else {})).text == page, (name, number)
            assert panel(client, name, page=str(len(pages) + 1)).status_code == 404
            assert client.get("/_browl/usage-count", params={"iri": f"{NS}{name}"}).text == count


def tab_iris(client: TestClient) -> list[str]:
    """Return the IRIs of the entities the client's app serves."""
    app = client.app
    generation = app.state.jobs.state.ready_generation  # type: ignore[attr-defined]
    rows = client.portal.call(  # type: ignore[union-attr]
        app.state.derived.read,  # type: ignore[attr-defined]
        generation,
        lambda connection: connection.execute("SELECT DISTINCT iri FROM entity WHERE type != 'ontology'").fetchall(),
    )
    return [iri for (iri,) in rows]


POLL = "/_browl/hash?path=%2Faxioms&amp;fragment=Animal&amp;tab=usage&amp;panel=1"
"""Where Animal's tab panel, preparing, polls itself."""


def preparing(page: str) -> tuple[str, str | None] | None:
    """Return a Usage tab panel's "Preparing…" note, and the URL it polls, if any; None if it has none."""
    match = re.search(r'<p class="note" id="usage-preparing"(.*?)>(.*?)</p>', page, re.DOTALL)
    if match is None:
        return None
    attributes, text = match.groups()
    poll = re.fullmatch(r' hx-get="([^"]*)" hx-trigger="every 5s" hx-target="#tab-panel"', attributes)
    return " ".join(text.split()), poll and poll.group(1)


@pytest.mark.fuseki
@pytest.mark.jvm
def test_preparing_polls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that "Preparing…" polls its own page's panel, and says how long the table's build has left if known.

    If the build failed, it says the usages can't be shown, without polling.
    """
    monkeypatch.setattr(browl.usage_tab, "MAX_STREAMED_USAGES", 4)
    monkeypatch.setattr(browl.usage_tab, "PAGE_USAGES", 3)
    tables = client.app.state.usage_tables  # type: ignore[attr-defined]
    poll = POLL.replace("&amp;panel=1", "&amp;page=2&amp;panel=1")
    assert preparing(panel(client, "Animal", page="2").text) == ("10 usages. Preparing…", poll)
    monkeypatch.setattr(tables, "remaining_s", lambda generation: 130.0)
    assert preparing(panel(client, "Animal").text) == ("10 usages. Preparing… Ready in about 3 minutes.", POLL)
    monkeypatch.setattr(tables, "failed", lambda generation: True)
    page = panel(client, "Animal").text
    assert preparing(page) is None
    assert MESSAGE in page


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
    assert panel(client, "Dog", page="2").status_code == 404


MESSAGE = "The usages can't be shown just now."
TOO_LARGE = "This page's usages are too large to show here."


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
    monkeypatch.setattr(browl.axioms, "MAX_FRAGMENT_IRIS", 2)
    page = panel(client, "Dog").text
    assert TOO_LARGE in page
    assert "mentions" in caplog.text and "more than 2" in caplog.text


@pytest.mark.fuseki
@pytest.mark.jvm
def test_too_large(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a page whose fragments, or a property's values, are too large for a query's result says so.

    The triples whose terms are known are written into the fragment, not fetched, so no query's cap holds them.
    """
    monkeypatch.setattr(browl.usage_tab, "CONSTRUCT_MAX_BYTES", 40)
    assert TOO_LARGE in panel(client, "Animal").text  # its usages' triples, written in
    assert "bytes, more than 40" in caplog.text
    assert TOO_LARGE in panel(client, "age").text  # `<:alice> :age 42`: its values, read first
    assert "the page's values are too large: the result is longer than 40 characters" in caplog.text


@pytest.mark.fuseki
@pytest.mark.jvm
def test_unlisted(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that usages counted but not listed, such as anonymous individuals', make no pages past those listed.

    An entity with none listed says so.
    """
    monkeypatch.setattr(browl.usage_tab, "PAGE_USAGES", 1)
    listed = browl.usage_tab.read_usages

    async def fewer(*args: object) -> list[Usage]:
        return (await listed(*args))[:-1]  # type: ignore[arg-type]

    monkeypatch.setattr(browl.usage_tab, "read_usages", fewer)
    assert 'max="4" value="4"' in panel(client, "Dog", page="4").text  # its count is 5
    assert panel(client, "Dog", page="5").status_code == 404

    async def none(*args: object) -> list[Usage]:
        return []

    monkeypatch.setattr(browl.usage_tab, "read_usages", none)
    page = panel(client, "Dog").text
    assert "None of these usages can be listed yet" in page
    assert "<dl" not in page and '<nav class="pager"' not in page
    whole = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Dog", "tab": "usage"}).text
    assert 'hx-sync="#tab-panel:replace">Usage (5)</a>' in whole  # the count, unlisted usages included
    assert panel(client, "Dog", page="2").status_code == 404


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
    page = "/root/_browl/iri?iri=urn%3Aexample%3Ax&tab=usage"
    assert (pager.previous, pager.panel, pager.next) == (
        browl.lookup._PageLink(page, f"{page}&panel=1"),
        f"{page}&page=2&panel=1",
        browl.lookup._PageLink(f"{page}&page=3", f"{page}&page=3&panel=1"),
    )
    assert (pager.action, pager.fields) == ("/root/_browl/iri", [("iri", "urn:example:x"), ("tab", "usage")])
    assert pager.action_panel == "/root/_browl/iri?panel=1"  # the fields give the IRI
    assert browl.lookup._pager(request, at, UsageTab(count=50, page=1, pages=1, groups=[])) is None
    assert browl.lookup._pager(request, at, UsageTab(count=None, page=1, pages=None, groups=None)) is None


def test_pager_hash_route() -> None:
    """Check that the paging of a hash entity's page links its page URLs, but fetches panels from the hash route."""
    request = Request({"type": "http", "root_path": "", "headers": []})
    at = browl.lookup._TabUrls("/onto", "Foo", "/_browl/hash?path=/onto&fragment=Foo")
    pager = browl.lookup._pager(request, at, UsageTab(count=120, page=2, pages=3, groups=[]))
    assert pager is not None
    panel = "/_browl/hash?path=/onto&fragment=Foo&tab=usage"
    assert (pager.previous, pager.panel, pager.next) == (
        browl.lookup._PageLink("/onto?tab=usage#Foo", f"{panel}&panel=1"),
        f"{panel}&page=2&panel=1",
        browl.lookup._PageLink("/onto?tab=usage&page=3#Foo", f"{panel}&page=3&panel=1"),
    )
    assert (pager.action, pager.fields) == ("/onto#Foo", [("tab", "usage")])
    assert pager.action_panel == "/_browl/hash?path=/onto&fragment=Foo&panel=1"


def test_unclaimed_on_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a page's axioms about none of its subjects, which shouldn't happen, follow them, with no subject."""
    subject, entity = (owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{NS}{name}") for name in "AE")
    claimed = owl.SubClassOf(sub=subject, super=entity)
    stray = owl.SubClassOf(sub=owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{NS}C"), super=entity)

    async def fragments(*args: object) -> tuple[bytes, bytes]:
        return b"", b""

    async def parsed(*args: object) -> tuple[dict[str, list[owl.AnyAxiom]], list[owl.AnyAxiom]]:
        return {subject.iri: [claimed]}, [stray]

    monkeypatch.setattr(browl.usage_tab, "fetch_usage_fragments", fragments)
    monkeypatch.setattr(browl.usage_tab, "run_java", parsed)
    usage = Usage(UsageSource.OBJECT, subject.iri, predicate=IRI(f"{NS}p"))
    groups = asyncio.run(read_usage_page(None, entity.iri, [usage], False))  # type: ignore[arg-type]
    assert groups == [UsageGroup(subject=subject.iri, axioms=[claimed]), UsageGroup(subject=None, axioms=[stray])]


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
def test_java_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a Java exception while reading a page's axioms is a `FragmentError`, which fails only the tab."""

    def fail(*args: object) -> None:
        raise jpype.JClass("java.lang.IllegalStateException")("no luck")

    monkeypatch.setattr(browl.usage_tab, "convert_axioms", fail)
    triples = f"{CLASSES}<{NS}A> {SUBCLASS} <{NS}E> .\n".encode()
    with pytest.raises(browl.owlapi.FragmentError, match="no luck"):
        asyncio.run(owlapi.run_java(browl.usage_tab.usage_axioms, triples, b"", f"{NS}E", {f"{NS}A": 1}))


@pytest.mark.fuseki
@pytest.mark.jvm
async def test_fragment_types(store: UsageStore) -> None:
    """Check that a page's fragment has the declaration types of the IRIs it mentions, and no others' or other types.

    Another entity's class assertions or characteristics would add axioms to the page.
    """
    usages = await read_usages(store, f"{NS}Animal")
    subject_triples, _ = await fetch_usage_fragments(store, f"{NS}Animal", usages)
    lines = subject_triples.decode().splitlines()
    part_of = f"<{NS}partOf> {RDF_TYPE}"
    assert f"{part_of} <{OWL_NS}ObjectProperty> ." in lines  # in Heart's and Liver's superclasses
    assert f"{part_of} <{OWL_NS}TransitiveProperty> ." not in lines
    assert not [line for line in lines if line.startswith(f"<{NS}ssn> ")]  # on no page of Animal's


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
    subjects = {f"{NS}A": 1, f"{NS}B": 1}
    by_subject, rest = asyncio.run(owlapi.run_java(browl.usage_tab.usage_axioms, triples, general, f"{NS}E", subjects))
    texts = {subject: [axiom.manchester.replace(NS, "") for axiom in axioms] for subject, axioms in by_subject.items()}
    assert texts == {f"{NS}A": ["<A> DisjointWith <B>"], f"{NS}B": ["<B> SubClassOf <E>"]}
    assert [axiom.manchester.replace(NS, "") for axiom in rest] == ["<C> SubClassOf <E>", "<D> SubClassOf <E>"]
    assert "Showing 1 axioms of the usages of http://example.org/axioms#E with no subject" in caplog.text
    with pytest.raises(browl.owlapi.FragmentError, match="couldn't parse"):
        asyncio.run(owlapi.run_java(browl.usage_tab.usage_axioms, b"not N-Triples", b"", f"{NS}E", subjects))


@pytest.mark.fuseki
@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_blank_object(fuseki_url: str, settings: Settings) -> None:
    """Check a property's usage whose object is blank, an anonymous individual: its triple is fetched by its label."""
    remark = f"<{NS}remark>"
    triples = f"{CLASSES}{remark} {RDF_TYPE} <{OWL_NS}AnnotationProperty> .\n<{NS}A> {remark} _:x .\n"
    async with loaded(fuseki_url, settings, {None: triples.encode()}, "") as blank:
        [usage] = await read_usages(blank, f"{NS}remark")
        [group] = await read_usage_page(blank, f"{NS}remark", [usage], False)
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
                pages.append(await read_usage_page(each, iri, ordered[: browl.usage_tab.PAGE_USAGES], False))
            assert pages[0] == pages[1], name
            assert pages[0]


SPLIT_KINDS = {
    "AnnotationAssertion",
    "AnnotationPropertyDomain",
    "AnnotationPropertyRange",
    "ClassAssertion",
    "DataPropertyAssertion",
    "DataPropertyDomain",
    "DataPropertyRange",
    "DatatypeDefinition",
    "DifferentIndividuals",
    "DisjointClasses",
    "DisjointDataProperties",
    "DisjointObjectProperties",
    "DisjointUnion",
    "EquivalentClasses",
    "EquivalentObjectProperties",
    "HasKey",
    "InverseObjectProperties",
    "ObjectPropertyAssertion",
    "ObjectPropertyDomain",
    "ObjectPropertyRange",
    "SameIndividual",
    "SubAnnotationPropertyOf",
    "SubClassOf",
    "SubDataPropertyOf",
    "SubObjectPropertyOf",
}
"""The kinds of axiom in `axioms.ofn` shown in columns under their subject: those that start with it and a keyword."""
WHOLE_KINDS = {
    "AsymmetricObjectProperty",
    "Declaration",  # annotated, in functional syntax (`browl.owl.OtherAxiom`)
    "DifferentIndividuals",  # with three members, a list, as are the others below
    "DisjointClasses",
    "DisjointDataProperties",
    "DisjointObjectProperties",
    "EquivalentDataProperties",  # always a list
    "FunctionalDataProperty",
    "FunctionalObjectProperty",
    "InverseFunctionalObjectProperty",
    "IrreflexiveObjectProperty",
    "NegativeDataPropertyAssertion",
    "NegativeObjectPropertyAssertion",
    "ReflexiveObjectProperty",
    "SubPropertyChainOf",  # owned by the property it implies, which comes last
    "SymmetricObjectProperty",
    "TransitiveObjectProperty",
}
"""The kinds shown whole under their subject, spanning both columns: those that don't start with it and a keyword."""


def column_kinds(ntriples: bytes) -> tuple[set[tuple[str, bool]], list[tuple[str, str]]]:
    """Return each kind of axiom about each IRI in the ontology, and whether it's in columns under that IRI.

    Also return, for each in columns, its subject, keyword and rest put together, and its whole text.
    """
    kinds: set[tuple[str, bool]] = set()
    texts: list[tuple[str, str]] = []
    with ntriples_ontology(ntriples) as ontology:
        for entity in ontology.getSignature():
            iri = str(entity.getIRI())
            for node in convert_axioms(axioms_about(ontology, iri)):
                node = node.with_first(iri)
                split = node.subject_split(iri)
                kinds.add((node.kind, split is not None))
                if split is not None:
                    parts = (split.subject,), split.keyword, split.rest
                    texts.append((" ".join(owl.manchester_text(part) for part in parts), node.manchester))
    return kinds, texts


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
def test_columns_by_kind(ntriples: bytes) -> None:
    """Check which kinds of axiom are shown in columns under their subject, and that their parts make the whole."""
    kinds, texts = asyncio.run(owlapi.run_java(column_kinds, ntriples))
    assert kinds == {(kind, True) for kind in SPLIT_KINDS} | {(kind, False) for kind in WHOLE_KINDS}
    assert texts
    for joined, whole in texts:
        assert joined == whole
    other = owl.OtherAxiom(axiom_type="DLSafeRule", functional="DLSafeRule(Body() Head())")
    assert other.subject_split(f"{NS}Dog") is None


@pytest.mark.fuseki
@pytest.mark.jvm
def test_columns(client: TestClient) -> None:
    """Check the columns: a line in them reads, for a screen reader, as the whole axiom; others span both.

    Those are a chain, which its implied property owns, an n-ary axiom in the group with no subject, and a general
    class axiom; an `OtherAxiom` is in `test_subjects`.
    """
    has_parent = panel(client, "hasParent").text
    assert columns_shown(has_parent) == [
        ":hasParent o :hasParent SubPropertyOf: :hasAncestor",
        ("InverseOf", ":hasParent"),
        ("SubPropertyOf:", ":hasParent"),
        ":hasParent o :hasBrother SubPropertyOf: :hasUncle",
        ("SubClassOf", ":hasParent max 2 owl:Thing"),
        ("SubClassOf", ":hasParent min 1 :Person"),
    ]
    assert lines_shown(has_parent)[1:3] == [":hasChild InverseOf :hasParent", ":hasMother SubPropertyOf: :hasParent"]
    assert columns_shown(panel(client, "note").text)[2:] == [
        "DisjointClasses: :Animal, :Fungus, :Plant",
        ':hasPart some <b>Heart</b> & "soul" SubClassOf :Vertebrate',
    ]
    assert subjects_shown(panel(client, "note").text)[2:] == ["No single subject"]


OBJECT_PROPERTY = "ObjectProperty"
SYMMETRIC = [
    ("disjointWith", "Class", "DisjointWith"),
    ("equivalentClass", "Class", "EquivalentTo"),
    ("sameAs", "NamedIndividual", "SameAs"),
    ("differentFrom", "NamedIndividual", "DifferentFrom"),
    ("equivalentProperty", OBJECT_PROPERTY, "EquivalentTo"),
    ("propertyDisjointWith", OBJECT_PROPERTY, "DisjointWith"),
    ("inverseOf", OBJECT_PROPERTY, "InverseOf"),
]
"""Symmetric axioms between two: each's predicate, its members' type, and its keyword."""


def symmetric_triples(predicate: str, type_: str) -> bytes:
    """Return a symmetric axiom stated on Z about A, which sorts first, with their declarations."""
    declared = "".join(f"<{NS}{name}> {RDF_TYPE} <{OWL_NS}{type_}> .\n" for name in "AZ")
    return f"{declared}<{NS}Z> <{OWL_NS}{predicate}> <{NS}A> .\n".encode()


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
@pytest.mark.parametrize(("predicate", "type_", "keyword"), SYMMETRIC)
def test_symmetric_subject_first(predicate: str, type_: str, keyword: str) -> None:
    """Check that a symmetric axiom stated on Z, on A's Usage tab, is written with Z, its subject, first."""
    triples = symmetric_triples(predicate, type_)
    found, _ = asyncio.run(owlapi.run_java(browl.usage_tab.usage_axioms, triples, b"", f"{NS}A", {f"{NS}Z": 1}))
    [axiom] = found[f"{NS}Z"]
    assert axiom.manchester == f"<{NS}Z> {keyword} <{NS}A>"
    split = axiom.subject_split(f"{NS}Z")
    assert split is not None
    assert (owl.manchester_text(split.keyword), owl.manchester_text(split.rest)) == (keyword, f"<{NS}A>")


@pytest.mark.fuseki
@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_symmetric_on_both_tabs(fuseki_url: str, settings: Settings) -> None:
    """Check `Z DisjointWith A`, stated on Z, on A's tabs: on its Usage tab under Z, Z first; on its Overview, as Z."""
    triples = symmetric_triples("disjointWith", "Class")
    async with loaded(fuseki_url, settings, {None: triples}, "") as small:
        [usage] = await read_usages(small, f"{NS}A")
        [group] = await read_usage_page(small, f"{NS}A", [usage], False)
        fragment = await fetch_fragment(small.sparql, small.dataset, f"{NS}A", small.dataset_clause)
    assert group.subject == f"{NS}Z"
    assert [axiom.manchester for axiom in group.axioms] == [f"<{NS}Z> DisjointWith <{NS}A>"]
    axioms = await owlapi.run_java(entity_axioms, fragment, f"{NS}A")
    [section] = frame_groups(axioms, f"{NS}A", [], str)
    assert section.label == "Disjoint With"
    assert [owl.manchester_text(line.manchester_tokens()) for line in section.lines] == [f"<{NS}Z>"]


def render_usage(tab: UsageTab, iri: str) -> str:
    """Return a page of the Usage tab as the template renders it, for the entity with this IRI."""
    return str(templates.env.get_template("usage.html").module.usage_panel(tab, None, iri))


def test_highlight_and_columns_rendered() -> None:
    """Check that the entity is highlighted in the lines, also in their annotations, but not as a subject.

    A line in columns has its subject in text only screen readers read, not a link; one with no subject spans both.
    """
    a, z = (owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{NS}{name}") for name in "AZ")
    note = owl.Entity(kind=owl.EntityKind.ANNOTATION_PROPERTY, iri=f"{NS}note")
    annotation = owl.Annotation(property=note, value=owl.IRIReference(iri=a.iri))
    disjoint = owl.DisjointClasses(members=(z, a))
    annotated = owl.SubClassOf(sub=z, super=z, annotations=(annotation,))
    general = owl.SubClassOf(sub=owl.ObjectComplementOf(operand=a), super=z)
    links = {
        each: EntityLink(each, each.removeprefix(NS).lower(), f"/{each.removeprefix(NS)}") for each in (a.iri, z.iri)
    }
    tab = UsageTab(
        count=3,
        page=1,
        pages=1,
        groups=[
            UsageGroup(subject=IRI(a.iri), axioms=[owl.SubClassOf(sub=a, super=z)]),  # its own axiom: no mark
            UsageGroup(subject=IRI(z.iri), axioms=[disjoint, annotated]),
            UsageGroup(subject=None, axioms=[general]),
        ],
        links=links,
    )
    page = render_usage(tab, a.iri)
    mark = '<mark><a href="/A"><bdi>a</bdi></a></mark>'
    assert columns_shown(page) == [
        ("SubClassOf", "z"),
        ("DisjointWith", "a"),
        ("SubClassOf", "z"),
        "not (a) SubClassOf z",
    ]
    assert lines_shown(page) == ["a SubClassOf z", "z DisjointWith a", "z SubClassOf z", "not (a) SubClassOf z"]
    assert page.count(mark) == 3  # on Z's disjointness, its annotation, and the general class axiom
    assert "<mark>" not in "".join(re.findall(r"<dt>.*?</dt>", page, re.DOTALL))
    assert '<span class="visually-hidden"><bdi>a</bdi> </span>' in page
    assert mark in page.split('<ul class="axiom-annotations">')[1]


@pytest.mark.fuseki
@pytest.mark.jvm
def test_highlight(client: TestClient) -> None:
    """Check that the entity is highlighted in its Usage tab's axioms, but not as a subject.

    `tests/test_axioms.py`'s `test_page` checks the Overview's.
    """
    mark = '<mark><a href="/axioms#Dog"><bdi>http://example.org/axioms#Dog</bdi></a></mark>'
    dog = panel(client, "Dog").text
    assert dog.count(mark) == 4  # once in each line but the SWRL rule's, in functional syntax, as text
    assert "<mark>" not in "".join(re.findall(r"<dt>.*?</dt>", dog, re.DOTALL))
    mineral = panel(client, "Mineral").text  # in an n-ary axiom's annotations
    assert mark.replace("Dog", "Mineral") in mineral.split('<ul class="axiom-annotations">')[1]


@pytest.mark.fuseki
@pytest.mark.jvm
def test_table_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a usage table that fails to read fails only the tab's content, with a message, and is logged."""

    async def fail(*args: object) -> None:
        raise sqlite3.DatabaseError("disk gone")

    monkeypatch.setattr(browl.usage_tab, "read_table_page", fail)
    page = panel(client, "Animal").text
    assert MESSAGE in page
    assert "Couldn't read the usage table for http://example.org/axioms#Animal" in caplog.text


@pytest.mark.fuseki
@pytest.mark.jvm
async def test_missing_value(store: UsageStore, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a property's usage past its subject's values, which can't happen, is logged and left out."""
    usage = Usage(UsageSource.PREDICATE, IRI(f"{NS}Cat"), index=5)
    [group] = await read_usage_page(store, f"{NS}remark", [usage], False)
    assert group.axioms == []
    assert f"No value 5 of {NS}remark for {NS}Cat" in caplog.text
