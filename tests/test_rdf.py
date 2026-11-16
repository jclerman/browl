"""Tests for RDF terms' checks, and strict parsing of SPARQL TSV results and N-Triples."""

import copy
import pickle

import pytest
from pydantic import BaseModel, ValidationError

from browl.rdf import (
    IRI,
    RDF_DIR_LANG_STRING,
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
        ("<http://example.org/\\u00e9>", IRI("http://example.org/é")),
        ("_:b0", BlankNode("b0")),
        ("_:node1.x-y", BlankNode("node1.x-y")),
        ('"plain"', Literal("plain")),
        ('""', Literal("")),
        ('"chat"@FR', Literal("chat", RDF_LANG_STRING, "fr")),
        ('"hi"@en-GB', Literal("hi", RDF_LANG_STRING, "en-gb")),
        ('"hi"@en--ltr', Literal("hi", RDF_DIR_LANG_STRING, "en", "ltr")),
        ('"hi"@AR-EG--rtl', Literal("hi", RDF_DIR_LANG_STRING, "ar-eg", "rtl")),
        ('"5"^^<http://www.w3.org/2001/XMLSchema#int>', Literal("5", f"{XSD}int")),
        ('"a\\tb\\nc\\"d\\\\e\\u00e9\\U0001F600"', Literal('a\tb\nc"d\\eé😀')),
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
        '"too big \\U00110000"',
        '"lone surrogate \\uD800"',
        '"lone surrogate \\uDFFF"',
        '"surrogate pair \\uD83D\\uDE00"',
        '"long-form surrogate \\U0000DC00"',
        "<http://example.org/\\uD800>",
        '"x"@',
        '"x"@en--',
        '"x"@en--up',
        '"x"@en--LTR',
        '"x"@en---ltr',
        '"x"@--ltr',
        '"x"@en--ltr^^<http://www.w3.org/2001/XMLSchema#string>',
        "<<( <http://example.org/a> <http://example.org/p> <http://example.org/b> )>>",
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
        '_:b0 <http://example.org/p> "x"@en-GB--rtl .',
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
        "<a> <http://example.org/p> <http://example.org/b> .",
        "<http://example.org/a> <p> <http://example.org/b> .",
        "<http://example.org/a> <http://example.org/p> <b> .",
        "<http://example.org/a\\u0020b> <http://example.org/p> <http://example.org/b> .",
        '<http://example.org/a> <http://example.org/p> "x"^^<int> .',
        '<http://example.org/a> <http://example.org/p> "x"^^<http://www.w3.org/1999/02/22-rdf-syntax-ns#langString> .',
        '<http://example.org/a> <http://example.org/p> "\\uD800" .',
        '<http://example.org/a> <http://example.org/p> "x"@en--up .',
        "<http://example.org/a> <http://example.org/p> <<( <http://example.org/a> <http://example.org/p> _:b )>> .",
    ],
)
def test_check_ntriples_triple_rejects(line: str) -> None:
    """Check that lines other than one triple, such as truncated ones, error text and comments, are rejected.

    Terms' values are checked as in TSV results: IRIs absolute, a langString tagged, escapes valid.
    """
    with pytest.raises(RDFSyntaxError):
        check_ntriples_triple(line)


@pytest.mark.parametrize(
    "value",
    ["", "example.org/a", "http://example.org/a b", "http://example.org/<a>", 'http://example.org/"a"', "1http://a"],
)
def test_iri_rejects(value: str) -> None:
    """Check that an IRI must be absolute, with only characters IRIs allow."""
    with pytest.raises(RDFSyntaxError, match="not a valid IRI"):
        IRI(value)


@pytest.mark.parametrize("value", ["", "b 0", "-b0", "b0.", "_:b0"])
def test_blank_node_rejects(value: str) -> None:
    """Check that a blank node label must be a valid label (without the '_:')."""
    with pytest.raises(RDFSyntaxError, match="not a valid BlankNode"):
        BlankNode(value)


@pytest.mark.parametrize(
    ("datatype", "language", "direction", "message"),
    [
        ("not an IRI", None, None, "not a valid IRI"),
        (RDF_LANG_STRING, "EN", None, "not a lowercase language tag"),
        (RDF_LANG_STRING, "en_GB", None, "not a lowercase language tag"),
        (RDF_LANG_STRING, None, None, "language tag if and only if"),
        (XSD_STRING, "en", None, "language tag if and only if"),
        (RDF_DIR_LANG_STRING, None, "ltr", "language tag if and only if"),
        (RDF_DIR_LANG_STRING, "en", None, "base direction if and only if"),
        (RDF_LANG_STRING, "en", "ltr", "base direction if and only if"),
        (RDF_DIR_LANG_STRING, "en", "LTR", "not a base direction"),
    ],
)
def test_literal_rejects(datatype: str, language: str | None, direction: str | None, message: str) -> None:
    """Check a literal's datatype, its lowercase tag (just for (dir)langStrings) and its direction (dirLangStrings)."""
    with pytest.raises(RDFSyntaxError, match=message):
        Literal("x", datatype, language, direction)


@pytest.mark.parametrize("cell", ["<http://example.org/a\\u0020b>", '"x"^^<http://example.org/\\u003c>'])
def test_parse_tsv_term_checks_unescaped_values(cell: str) -> None:
    """Check that a term whose syntax is valid but whose unescaped value isn't, such as an IRI with a space, fails."""
    with pytest.raises(RDFSyntaxError, match="not a valid IRI"):
        parse_tsv_term(cell)


class Holder(BaseModel):
    """A pydantic model with term fields."""

    iri: IRI
    blank_node: BlankNode
    literal: Literal


def test_pydantic_fields() -> None:
    """Check that pydantic models' term fields make the term types, with the same checks."""
    holder = Holder.model_validate(
        {
            "iri": "http://example.org/a",
            "blank_node": "b0",
            "literal": {"value": "x", "language": "en", "datatype": RDF_LANG_STRING},
        }
    )
    assert type(holder.iri) is IRI
    assert type(holder.blank_node) is BlankNode
    assert holder.literal == Literal("x", RDF_LANG_STRING, "en")
    with pytest.raises(ValidationError, match="not a valid IRI"):
        Holder.model_validate({"iri": "not an IRI", "blank_node": "b0", "literal": {"value": "x"}})
    with pytest.raises(ValidationError, match="if and only if"):
        Holder.model_validate(
            {"iri": "http://example.org/a", "blank_node": "b0", "literal": {"value": "x", "language": "en"}}
        )


def test_terms_copy_and_pickle() -> None:
    """Check that terms keep their type when copied or pickled."""
    for term in (IRI("http://example.org/a"), BlankNode("b0"), Literal("x")):
        assert copy.copy(term) == term
        assert type(pickle.loads(pickle.dumps(term))) is type(term)
