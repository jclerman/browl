"""Tests for Manchester text as templates show it: segments, IRIs, one-line text, and the axioms template's HTML."""

import time

import pytest

from browl import manchester
from browl.links import EntityLink, no_link
from browl.manchester import Segment, SegmentKind
from browl.pages import templates
from browl.rdf import OWL, RDF, RDFS, XSD

A = "http://example.org/A"
P = "http://example.org/p"


def test_segments() -> None:
    """Check that text splits into IRIs, keywords (with their colons), literals and the text between."""
    assert manchester.segments(f'<{A}> SubClassOf: <{P}> some ({{"x"}} and not <{A}>)') == [
        Segment(SegmentKind.IRI, f"<{A}>"),
        Segment(SegmentKind.TEXT, " "),
        Segment(SegmentKind.KEYWORD, "SubClassOf:"),
        Segment(SegmentKind.TEXT, " "),
        Segment(SegmentKind.IRI, f"<{P}>"),
        Segment(SegmentKind.TEXT, " "),
        Segment(SegmentKind.KEYWORD, "some"),
        Segment(SegmentKind.TEXT, " ({"),
        Segment(SegmentKind.LITERAL, '"x"'),
        Segment(SegmentKind.TEXT, "} "),
        Segment(SegmentKind.KEYWORD, "and"),
        Segment(SegmentKind.TEXT, " "),
        Segment(SegmentKind.KEYWORD, "not"),
        Segment(SegmentKind.TEXT, " "),
        Segment(SegmentKind.IRI, f"<{A}>"),
        Segment(SegmentKind.TEXT, ")"),
    ]


@pytest.mark.parametrize(
    "literal",
    [
        f'"see <{A}> and some"',
        '"a \\"quoted\\" <b>bold</b> \\\\"',
        '"on two\nlines"@en',
        '"tagged"@or',  # Oriya's language tag is a keyword's word
    ],
    ids=["iri-and-keywords", "escaped-quotes", "line-break", "language-tag"],
)
def test_literal_whole(literal: str) -> None:
    """Check that a literal is one segment, with no IRIs or keywords found inside it, and its language tag."""
    text = f"<{A}> <{P}> {literal}"
    assert manchester.segments(text)[-1] == Segment(SegmentKind.LITERAL, literal)
    assert manchester.iris(text) == [A, P]


@pytest.mark.parametrize(
    ("text", "keywords"),
    [
        ("xsd:integer[>= 0, < 5]", []),  # facets aren't IRIs
        ("someone or nothing", ["or"]),  # whole words only
        ("Self", ["Self"]),
        ("Range: x", ["Range:"]),
    ],
)
def test_keywords(text: str, keywords: list[str]) -> None:
    """Check that keywords are whole words, and that facet symbols aren't taken for IRIs."""
    found = manchester.segments(text)
    assert [segment.text for segment in found if segment.kind == SegmentKind.KEYWORD] == keywords
    assert not [segment for segment in found if segment.kind == SegmentKind.IRI]


@pytest.mark.parametrize(
    ("text", "literal"),
    [
        (f'<{A}> value "x <{P}> and', f'"x <{P}> and'),
        (f'<{A}> value "x \\" <{P}> and', f'"x \\" <{P}> and'),
        (f'<{A}> value "x \\', '"x \\'),
    ],
    ids=["unclosed", "escaped-quote", "trailing-backslash"],
)
def test_unclosed_literal(text: str, literal: str) -> None:
    """Check that a literal left unclosed runs to the end of the text."""
    assert manchester.segments(text)[-1] == Segment(SegmentKind.LITERAL, literal)
    assert manchester.iris(text) == [A]


def test_linear() -> None:
    """Check that text with 50 K unclosed quotes, or a long run of spaces, takes linear time, not quadratic."""
    quotes = '"\\' * 50_000
    spaces = "x" + " " * 50_000 + "x"
    start = time.perf_counter()
    found = manchester.segments(quotes), manchester.one_line(quotes), manchester.one_line(spaces)
    assert time.perf_counter() - start < 0.5
    assert found == ([Segment(SegmentKind.LITERAL, quotes)], quotes, spaces)


def test_segments_own() -> None:
    """Check that, given the IRIs an axiom is about, any other `<…>` is text, not an IRI, and has no keywords."""
    other = "http://example.org/and"
    assert manchester.segments(f"<{A}> and <{other}>", own=[A]) == [
        Segment(SegmentKind.IRI, f"<{A}>"),
        Segment(SegmentKind.TEXT, " "),
        Segment(SegmentKind.KEYWORD, "and"),
        Segment(SegmentKind.TEXT, " "),
        Segment(SegmentKind.TEXT, f"<{other}>"),
    ]


def test_iris_once() -> None:
    """Check that `iris` lists each IRI once, in order of first appearance."""
    assert manchester.iris(f"<{P}> <{A}> <{P}>") == [P, A]


def test_one_line() -> None:
    """Check that line breaks the renderer adds become single spaces, but those in literals are kept."""
    text = f'<{A}> SubClassOf <{P}> some \n    (<{A}> and <{P}> value "two\n  lines")\n'
    assert manchester.one_line(text) == f'<{A}> SubClassOf <{P}> some (<{A}> and <{P}> value "two\n  lines")'


def render(text: str, links: dict[str, EntityLink], iris: list[str] | None = None) -> str:
    """Return Manchester text as the axioms template renders it, by default with every IRI in it the axiom's own."""
    source = '{% from "axioms.html" import manchester %}{{ manchester(text, links, iris) }}'
    return templates.env.from_string(source).render(
        text=text, links=links, iris=manchester.iris(text) if iris is None else iris
    )


def test_html() -> None:
    """Check that entities with a page become links, keywords are styled, and the rest is escaped text."""
    links = {A: EntityLink(A, "<b>A</b> & \"a\" 'b'", "/A"), P: no_link(P)}
    html = render(f'<{A}> SubClassOf <{P}> value "<{A}> & <i>"@en', links)
    assert html == (
        '<a href="/A"><bdi>&lt;b&gt;A&lt;/b&gt; &amp; &#34;a&#34; &#39;b&#39;</bdi></a> '
        '<span class="keyword">SubClassOf</span> '
        f'<code dir="ltr">&lt;{P}&gt;</code> '
        '<span class="keyword">value</span> '
        f"<bdi>&#34;&lt;{A}&gt; &amp; &lt;i&gt;&#34;@en</bdi>"
    )


def test_html_unknown_iri() -> None:
    """Check that an IRI missing from the links is shown as text, as one with no page is."""
    assert render(f"<{A}>", {}) == f'<code dir="ltr">&lt;{A}&gt;</code>'


def test_html_not_manchester() -> None:
    """Check that text that isn't well-formed, such as an unclosed literal or a bad IRI, is still escaped."""
    html = render('<javascript:alert(1)">x</a> "<script>', {})
    assert "<script" not in html
    assert "<a" not in html
    assert "</a>" not in html


@pytest.mark.parametrize(
    ("iri", "name"),
    [
        (f"{XSD}integer", "xsd:integer"),
        (f"{RDF}PlainLiteral", "rdf:PlainLiteral"),
        (f"{RDFS}Literal", "rdfs:Literal"),
        (f"{OWL}Thing", "owl:Thing"),
        (XSD, None),  # the namespace itself
        (f"{XSD}a/b", None),  # not a local name
        (f"{XSD}integer#x", None),
        ("http://www.w3.org/2001/XMLSchema", None),
        ("http://example.org/integer", None),
    ],
)
def test_standard_name(iri: str, name: str | None) -> None:
    """Check that only IRIs in the standard vocabularies, with a plain local name, have a prefixed name."""
    assert manchester.standard_name(iri) == name
    assert Segment(SegmentKind.IRI, f"<{iri}>").standard_name == name


def test_html_not_own() -> None:
    """Check that an IRI the axiom isn't about is plain text, even if it has a page or a prefixed name."""
    links = {A: EntityLink(A, "A", "/A"), P: EntityLink(P, "P", "/P")}
    html = render(f"<{A}> SubClassOf <{P}> and <{XSD}integer>", links, iris=[A])
    assert html == (
        '<a href="/A"><bdi>A</bdi></a> <span class="keyword">SubClassOf</span> '
        f'&lt;{P}&gt; <span class="keyword">and</span> &lt;{XSD}integer&gt;'
    )


def test_html_standard_names() -> None:
    """Check that a standard vocabulary's IRI shows its prefixed name, unless it has a page, when the link wins."""
    links = {f"{XSD}integer": no_link(f"{XSD}integer"), f"{OWL}Thing": EntityLink(f"{OWL}Thing", "Thing", "/Thing")}
    html = render(f"<{P}> some <{XSD}integer> and <{OWL}Thing> or <{A}>", links)
    assert html == (
        f'<code dir="ltr">&lt;{P}&gt;</code> <span class="keyword">some</span> <code dir="ltr">xsd:integer</code> '
        '<span class="keyword">and</span> <a href="/Thing"><bdi>Thing</bdi></a> '
        f'<span class="keyword">or</span> <code dir="ltr">&lt;{A}&gt;</code>'
    )
