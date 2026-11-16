"""Tests for strict parsing of SPARQL TSV results and N-Triples."""

import pytest

from browl.rdf import (
    IRI,
    RDF_LANG_STRING,
    XSD,
    XSD_STRING,
    BlankNode,
    Literal,
    RDFSyntaxError,
    check_ntriples_triple,
    parse_tsv_header,
    parse_tsv_row,
    parse_tsv_term,
)


@pytest.mark.parametrize(
    ("cell", "term"),
    [
        ("<http://example.org/a>", IRI("http://example.org/a")),
        (r"<http://example.org/é>", IRI("http://example.org/é")),
        ("_:b0", BlankNode("b0")),
        ("_:node1.x-y", BlankNode("node1.x-y")),
        ('"plain"', Literal("plain")),
        ('""', Literal("")),
        ('"chat"@FR', Literal("chat", RDF_LANG_STRING, "fr")),
        ('"hi"@en-GB', Literal("hi", RDF_LANG_STRING, "en-gb")),
        ('"5"^^<http://www.w3.org/2001/XMLSchema#int>', Literal("5", f"{XSD}int")),
        (r'"a\tb\nc\"d\\eé\U0001F600"', Literal('a\tb\nc"d\\eé😀')),
        ('"raw é and \u2028 stay"', Literal("raw é and \u2028 stay")),
        ("42", Literal("42", f"{XSD}integer")),
        ("-7", Literal("-7", f"{XSD}integer")),
        ("1.5", Literal("1.5", f"{XSD}decimal")),
        (".5", Literal(".5", f"{XSD}decimal")),
        ("1e3", Literal("1e3", f"{XSD}double")),
        ("1.5E-3", Literal("1.5E-3", f"{XSD}double")),
        ("true", Literal("true", f"{XSD}boolean")),
        ("", None),
    ],
)
def test_parse_tsv_term(cell: str, term: object) -> None:
    """Check that each kind of term in SPARQL TSV's Turtle syntax parses, including escapes and abbreviations."""
    assert parse_tsv_term(cell) == term


def test_literal_default_datatype() -> None:
    """Check that a plain literal equals the same literal typed as xsd:string."""
    assert parse_tsv_term('"x"') == parse_tsv_term(f'"x"^^<{XSD_STRING}>')


@pytest.mark.parametrize(
    "cell",
    [
        "## Query cancelled due to timeout during execution ##",
        "Error 500: Server Error",
        "http://example.org/a",
        "<http://example.org/a",
        "<http://example.org/a b>",
        '"unterminated',
        '"bad escape \\q"',
        '"too big \\UFFFFFFFF"',
        '"x"@',
        '"x"^^xsd:int',
        "1.",
        "TRUE",
        "_:",
        "ex:prefixed",
    ],
)
def test_parse_tsv_term_rejects(cell: str) -> None:
    """Check that text that isn't a term, such as error text Fuseki appends to a result, is rejected."""
    with pytest.raises(RDFSyntaxError):
        parse_tsv_term(cell)


def test_parse_tsv_header() -> None:
    """Check that a header line gives the variable names, and that an empty one means no variables."""
    assert parse_tsv_header("?s\t?label\t?n_1") == ("s", "label", "n_1")
    assert parse_tsv_header("") == ()


@pytest.mark.parametrize("line", ["s\tp", "?s\t", "?s ?p", "## Query cancelled ##"])
def test_parse_tsv_header_rejects(line: str) -> None:
    """Check that a header line that isn't a list of variables is rejected."""
    with pytest.raises(RDFSyntaxError):
        parse_tsv_header(line)


def test_parse_tsv_row() -> None:
    """Check that a row parses cell by cell, with empty cells unbound."""
    assert parse_tsv_row('<http://example.org/a>\t\t"x"', 3) == (IRI("http://example.org/a"), None, Literal("x"))
    assert parse_tsv_row("", 1) == (None,)
    assert parse_tsv_row("", 0) == ()


@pytest.mark.parametrize(
    ("line", "width"),
    [
        ("<http://example.org/a>", 2),
        ("<http://example.org/a>\t<http://example.org/b>", 1),
        ("## Query cancelled due to timeout during execution ##", 1),
        ("<http://example.org/a>\tError", 2),
        ("x", 0),
    ],
)
def test_parse_tsv_row_rejects(line: str, width: int) -> None:
    """Check that a row with the wrong number of cells, or a cell that isn't a term, is rejected."""
    with pytest.raises(RDFSyntaxError):
        parse_tsv_row(line, width)


@pytest.mark.parametrize(
    "line",
    [
        "<http://example.org/a> <http://example.org/p> <http://example.org/b> .",
        '_:b0 <http://example.org/p> "x"@en .',
        '<http://example.org/a> <http://example.org/p> "1"^^<http://www.w3.org/2001/XMLSchema#int> .',
        '<http://example.org/a>\t<http://example.org/p>\t"raw é \u2028"\t.\t',
    ],
)
def test_check_ntriples_triple(line: str) -> None:
    """Check that triples pass, with spaces or tabs between terms and raw non-ASCII characters."""
    check_ntriples_triple(line)


@pytest.mark.parametrize(
    "line",
    [
        "Error 500: Server Error",
        "## Query cancelled due to timeout during execution ##",
        "",
        "<http://example.org/a> <http://example.org/p> <http://example.org/b> . # comment",
        "<http://example.org/a> <http://example.org/p> <http://example.org/b>",
        "<http://example.org/a> <http://example.org/p> 12 .",
        '"x" <http://example.org/p> <http://example.org/b> .',
        "<http://example.org/a> _:p <http://example.org/b> .",
        "<http://example.org/a> <http://example.org/p> <http://example.org/b> . extra",
        "<http://example.org/a> <http://example.org/p> <http://exa",
    ],
)
def test_check_ntriples_triple_rejects(line: str) -> None:
    """Check that lines other than one triple, such as truncated ones, error text and comments, are rejected."""
    with pytest.raises(RDFSyntaxError):
        check_ntriples_triple(line)
