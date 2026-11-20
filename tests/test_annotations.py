"""Tests for the Overview tab's annotations panel (`browl.annotations`, DESIGN.md §2.8).

They check which triples are annotations, their order, and their HTML.
"""

import logging
import re
import urllib.parse
from collections.abc import Iterator

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

import browl.annotations
import browl.overview
from browl.annotations import (
    Annotations,
    IRIValue,
    LiteralValue,
    TooManyAnnotationsError,
    fetch_annotations,
    group_annotations,
)
from browl.derived import EntityType
from browl.fuseki import UNION_FROM, FusekiClient, SparqlError
from browl.jobs import Stage
from browl.links import EntityLink
from browl.pages import MAX_IRI_CHARS, MAX_LANGUAGE_CHARS, templates
from browl.rdf import IRI, OWL, RDF, RDFS, XSD
from tests.helpers import derived_stage, fuseki_dataset, make_app

NS = "http://example.org/ann#"
SKOS = "http://www.w3.org/2004/02/skos/core#"
LANG_STRING = f"{RDF}langString"
A = f"{NS}A"
PANEL = {"path": "/ann", "fragment": "A", "panel": "1"}

TRIPLES = f"""\
<{A}> <{RDF}type> <{OWL}Class> .
<{NS}B> <{RDF}type> <{OWL}Class> .
<{NS}definition> <{RDF}type> <{OWL}AnnotationProperty> .
<{NS}related> <{RDF}type> <{OWL}AnnotationProperty> .
<{NS}weight> <{RDF}type> <{OWL}DatatypeProperty> .
<{A}> <{RDFS}label> "Heart"@en .
<{A}> <{RDFS}label> "c\\u0153ur" .
<{A}> <{RDFS}label> "Herz"@de .
<{A}> <{SKOS}prefLabel> "heart" .
<{A}> <{RDFS}comment> "A <b>comment</b>" .
<{A}> <{NS}definition> "A hollow muscular organ."@en .
<{A}> <{NS}related> <{NS}B> .
<{A}> <{NS}seeAlsoPage> <https://example.org/heart?a=1&b=2> .
<{A}> <{NS}code> <urn:example:1> .
<{A}> <{NS}code> <javascript:alert(1)> .
<{A}> <{NS}arabic> "\\u0645\\u0631\\u062D\\u0628\\u0627"@ar--rtl .
<{A}> <{OWL}deprecated> "true"^^<{XSD}boolean> .
<{A}> <{NS}weight> "5" .
<{A}> <{NS}partOf> <{NS}B> .
<{A}> <{RDFS}subClassOf> <{NS}B> .
<{A}> <{NS}note> _:b .
""".encode()

IMPORTED = f"<{NS}partOf> <{RDF}type> <{OWL}ObjectProperty> .\n".encode()
"""In an import's graph: a query that doesn't see the imports takes `partOf` for an undeclared annotation property."""

ENTITIES = [
    (A, EntityType.CLASS),
    (f"{NS}B", EntityType.CLASS),
    (f"{NS}definition", EntityType.ANNOTATION_PROPERTY),
    (f"{NS}related", EntityType.ANNOTATION_PROPERTY),
    (f"{NS}weight", EntityType.DATA_PROPERTY),
]
NAMES = {
    A: ("heart", None),
    f"{NS}B": ("Lung", None),
    f"{NS}definition": ("definition", None),
    f"{NS}related": ("related to", None),
}


@pytest.fixture(scope="module")
def dataset(fuseki_url: str) -> Iterator[str]:
    """Yield the name of a dataset holding `TRIPLES`, and `IMPORTED` in an import's graph."""
    with fuseki_dataset(fuseki_url, {None: TRIPLES, "http://example.org/imported": IMPORTED}) as name:
        yield name


def literal(lexical: str, language: str | None = None) -> LiteralValue:
    """Return a plain or language-tagged string."""
    return LiteralValue(lexical=lexical, datatype=IRI(LANG_STRING if language else f"{XSD}string"), language=language)


@pytest.mark.fuseki
@pytest.mark.anyio
@pytest.mark.parametrize(("clause", "imported"), [("", True), (UNION_FROM, False)], ids=["own", "with-imports"])
async def test_fetch(fuseki_url: str, dataset: str, clause: str, imported: bool) -> None:
    """Check which triples are annotations: not logical ones, blank values, or (with the imports) object properties."""
    async with httpx2.AsyncClient(base_url=fuseki_url) as http:
        pairs = await fetch_annotations(FusekiClient(http), dataset, A, clause)
    properties = {property_ for property_, _ in pairs}
    expected = {
        f"{RDFS}label",
        f"{SKOS}prefLabel",
        f"{RDFS}comment",
        f"{OWL}deprecated",
        *(f"{NS}{name}" for name in ("definition", "related", "seeAlsoPage", "code", "arabic")),
    }
    assert properties == expected | ({f"{NS}partOf"} if imported else set())
    assert (IRI(f"{NS}related"), IRIValue(iri=IRI(f"{NS}B"))) in pairs
    arabic = LiteralValue(lexical="مرحبا", datatype=IRI(f"{RDF}dirLangString"), language="ar", direction="rtl")
    assert (IRI(f"{NS}arabic"), arabic) in pairs


@pytest.mark.fuseki
@pytest.mark.usefixtures("data_dir")
def test_page(fuseki_url: str, dataset: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the panel's HTML: name properties first, the rest by name; values one per line, escaped and linked."""

    async def no_axioms(*args: object) -> browl.overview.AxiomsPanel:
        return browl.overview.AxiomsPanel([], {})

    monkeypatch.setattr(browl.overview, "dataset_name", lambda generation: dataset)
    monkeypatch.setattr(browl.overview, "_read_axioms", no_axioms)
    app = make_app(FUSEKI_URL=fuseki_url, NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"])
    with TestClient(app) as client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage(ENTITIES, names=NAMES))
        client.portal.call(app.state.jobs.start)
        client.portal.call(app.state.jobs.wait)
        page = client.get("/_browl/hash", params=PANEL).text
    terms = re.findall(r"<dt>(.*?)</dt>", page)
    code = '<code dir="ltr">{}</code>'.format
    assert terms == [
        code("rdfs:label"),
        code(f"{SKOS}prefLabel"),  # not a standard vocabulary's
        '<a href="/ann#definition"><bdi>definition</bdi></a>',
        code(f"{NS}arabic"),
        code(f"{NS}code"),
        code(f"{NS}partOf"),  # the job has no imports, so the page doesn't see the import's declaration
        code(f"{NS}seeAlsoPage"),
        code("owl:deprecated"),
        code("rdfs:comment"),
        '<a href="/ann#related"><bdi>related to</bdi></a>',
    ]
    values = re.findall(r"<dd>(.*?)</dd>", page)
    assert values[:4] == [
        '<bdi class="value" lang="en">Heart</bdi> <small class="lang">en</small>',
        '<bdi class="value" lang="de">Herz</bdi> <small class="lang">de</small>',  # an unranked language, then untagged
        '<bdi class="value">cœur</bdi>',
        '<bdi class="value">heart</bdi>',
    ]
    assert '<dd><bdi class="value" lang="ar" dir="rtl">مرحبا</bdi> <small class="lang">ar</small></dd>' in page
    assert '<dd><bdi class="value">A &lt;b&gt;comment&lt;/b&gt;</bdi></dd>' in page
    assert '<dd><a href="/ann#B"><bdi>Lung</bdi></a></dd>' in page
    link = '<a href="https://example.org/heart?a=1&amp;b=2" rel="nofollow">'
    assert f'<dd>{link}<code dir="ltr">https://example.org/heart?a=1&amp;b=2</code></a></dd>' in page
    assert f"<dd>{code('javascript:alert(1)')}</dd>" in page  # text, not a link
    assert f"<dd>{code('urn:example:1')}</dd>" in page
    assert '<dd><bdi class="value">true</bdi></dd>' in page
    assert "weight" not in page and "subClassOf" not in page and "#note" not in page


def test_group_order() -> None:
    """Check the order of properties and values, and that a repeated value comes once.

    `first` comes in its order, the rest by shown name case-insensitively; literals by language, then IRIs.
    """
    label, alias, zeta, alpha = (IRI(f"{NS}{name}") for name in ("label", "alias", "zeta", "Alpha"))
    shown = {zeta: "zeta", alpha: "Alpha", label: "label", alias: "alias"}
    pairs = [
        (zeta, literal("z")),
        (alpha, IRIValue(iri=IRI(f"{NS}x"))),
        (alpha, literal("b", "fr")),
        (alpha, literal("c")),
        (alpha, literal("a", "en")),
        (alias, literal("x")),
        (label, literal("L")),
        (label, literal("L")),
    ]
    groups = group_annotations(pairs, [label, alias], shown.__getitem__, ["en", "none"])
    assert [group.property for group in groups] == [label, alias, alpha, zeta]
    assert groups[0].values == (literal("L"),)
    assert groups[2].values == (literal("a", "en"), literal("b", "fr"), literal("c"), IRIValue(iri=IRI(f"{NS}x")))


def test_json() -> None:
    """Check that annotations round-trip through JSON, each value by its type."""
    groups = [Annotations(property=IRI(A), values=(literal("a", "en"), IRIValue(iri=IRI(A))))]
    adapter = TypeAdapter(list[Annotations])
    assert adapter.validate_json(adapter.dump_json(groups)) == groups


def answer(body: str, queries: list[str] | None = None) -> FusekiClient:
    """Return a client whose stubbed Fuseki answers every query with this SELECT result, as TSV.

    Args:
        body: The result.
        queries: If given, each query is appended to it.
    """

    def fuseki(request: httpx2.Request) -> httpx2.Response:
        if queries is not None:
            queries.append(urllib.parse.parse_qs(request.content.decode())["query"][0])
        return httpx2.Response(200, text=body, headers={"Content-Type": "text/tab-separated-values"})

    return FusekiClient(httpx2.AsyncClient(transport=httpx2.MockTransport(fuseki), base_url="http://fuseki"))


@pytest.mark.anyio
async def test_too_many(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that more than `MAX_ANNOTATIONS` values raise, and that the query asks for one more than that."""
    monkeypatch.setattr(browl.annotations, "MAX_ANNOTATIONS", 1)
    row = f'<{RDFS}label>\t"x"\n'
    with pytest.raises(TooManyAnnotationsError):
        await fetch_annotations(answer(f"?property\t?value\n{row}{row}"), "ds", A)
    assert len(await fetch_annotations(answer(f"?property\t?value\n{row}"), "ds", A)) == 1


@pytest.mark.anyio
async def test_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that annotations longer in all than `MAX_ANNOTATIONS_CHARS` raise, as too many do (DESIGN.md §2.8)."""
    body = f'?property\t?value\n<{RDFS}comment>\t"{"x" * 100}"\n'
    monkeypatch.setattr(browl.annotations, "MAX_ANNOTATIONS_CHARS", len(body) - 1)
    with pytest.raises(TooManyAnnotationsError, match="longer than"):
        await fetch_annotations(answer(body), "ds", A)
    monkeypatch.setattr(browl.annotations, "MAX_ANNOTATIONS_CHARS", len(body))
    assert len(await fetch_annotations(answer(body), "ds", A)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("row", "error"),
    [(f"<{RDFS}label>\t_:b", "isn't an IRI or a literal"), ('_:p\t"x"', "isn't an IRI")],
    ids=["blank-value", "blank-property"],
)
async def test_unexpected_term(row: str, error: str) -> None:
    """Check that a blank value, which the query leaves out, or a blank property is an error if it comes anyway."""
    with pytest.raises(SparqlError, match=error):
        await fetch_annotations(answer(f"?property\t?value\n{row}\n"), "ds", A)


@pytest.mark.usefixtures("data_dir")
def test_too_many_page(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that the panel says when there are too many annotations, and the log why, and the axioms still show."""

    async def too_many(*args: object) -> list[object]:
        raise TooManyAnnotationsError("the entity has more than 1000 annotation values")

    async def no_axioms(*args: object) -> browl.overview.AxiomsPanel:
        return browl.overview.AxiomsPanel([], {})

    monkeypatch.setattr(browl.overview, "fetch_annotations", too_many)
    monkeypatch.setattr(browl.overview, "_read_axioms", no_axioms)
    app = make_app()
    with TestClient(app) as client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage([(A, EntityType.CLASS)]))
        client.portal.call(app.state.jobs.start)
        client.portal.call(app.state.jobs.wait)
        with caplog.at_level(logging.WARNING, logger="browl.overview"):
            page = client.get("/_browl/hash", params=PANEL).text
    assert "This entity has too many annotations to show here." in page
    assert '<p class="note">None.</p>' in page  # the axioms panel
    [record] = caplog.records
    assert record.getMessage() == f"Not showing the annotations of {A}: the entity has more than 1000 annotation values"


def rendered(value: IRIValue | LiteralValue, links: dict[str, EntityLink] | None = None) -> str:
    """Return a value's HTML, as the panel renders it."""
    template = templates.env.from_string(
        '{% from "annotations.html" import annotation_value %}{{ annotation_value(value, links) }}'
    )
    return template.render(value=value, links=links or {})


def test_long_iri_not_linked() -> None:
    """Check that a web IRI too long to show whole is text, as its cut text could hide where a link goes."""
    fits = f"https://example.org/{'a' * (MAX_IRI_CHARS - 20)}"
    assert len(fits) == MAX_IRI_CHARS
    assert rendered(IRIValue(iri=IRI(fits))) == f'<a href="{fits}" rel="nofollow"><code dir="ltr">{fits}</code></a>'
    long = f"https://trusted.example.{'a' * MAX_IRI_CHARS}.evil.example/"
    assert rendered(IRIValue(iri=IRI(long))) == f'<code dir="ltr">{long[: MAX_IRI_CHARS - 1]}…</code>'


def test_long_language_tag() -> None:
    """Check that a language tag longer than `MAX_LANGUAGE_CHARS` isn't given as `lang`, and is shown cut short."""
    fits = "-".join(["en"] + ["abcdefgh"] * 3)  # 2 + 3 * 9 = 29 characters
    assert (
        rendered(literal("x", fits)) == f'<bdi class="value" lang="{fits}">x</bdi> <small class="lang">{fits}</small>'
    )
    long = "-".join(["en"] + ["abcdefgh"] * 500)
    shown = f"{long[: MAX_LANGUAGE_CHARS - 1]}…"
    assert rendered(literal("x", long)) == f'<bdi class="value">x</bdi> <small class="lang">{shown}</small>'


@pytest.mark.parametrize(
    "fields",
    [{"language": "EN"}, {"language": "en us"}, {"language": "en", "direction": "up"}],
    ids=["upper-case-tag", "not-a-tag", "not-a-direction"],
)
def test_literal_checked(fields: dict[str, str]) -> None:
    """Check that a literal value refuses a language tag that isn't a lowercase tag, and an unknown base direction."""
    with pytest.raises(ValidationError):
        LiteralValue(lexical="x", datatype=IRI(LANG_STRING), **fields)
