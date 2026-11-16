"""RDF terms, and strict parsing of the SPARQL TSV results and N-Triples that Fuseki sends.

Parsing is strict so that a truncated response can't pass as a short result: when a query fails after Fuseki has sent
HTTP 200, it appends the error as plain text and ends the response normally (DESIGN.md §2.14). That text doesn't parse
as RDF terms or N-Triples.
"""

import re
from dataclasses import dataclass

XSD = "http://www.w3.org/2001/XMLSchema#"
XSD_STRING = f"{XSD}string"
RDF_LANG_STRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"


class IRI(str):
    """An IRI."""

    __slots__ = ()


class BlankNode(str):
    """A blank node, by its label in one result; labels mean nothing across results."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Literal:
    """An RDF literal.

    Attributes:
        value: The lexical form.
        datatype: The datatype IRI; `RDF_LANG_STRING` for a language-tagged string.
        language: The language tag, lowercased, for a language-tagged string; otherwise None.
    """

    value: str
    datatype: str = XSD_STRING
    language: str | None = None


Term = IRI | BlankNode | Literal


class RDFSyntaxError(ValueError):
    """Text isn't a valid RDF term or N-Triples line."""


# Patterns from the Turtle and N-Triples grammars (https://www.w3.org/TR/turtle/#sec-grammar-grammar).
_UCHAR = r"\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8}"
_IRI = rf"<((?:[^\x00-\x20<>\"{{}}|^`\\]|{_UCHAR})*)>"
_BLANK_NODE = r"_:([\w](?:[\w.\-\u00b7\u0300-\u036f\u203f\u2040]*[\w\-\u00b7\u0300-\u036f\u203f\u2040])?)"
_STRING = rf"\"((?:[^\"\\\n\r]|\\[tbnrf\"'\\]|{_UCHAR})*)\""
_LANGUAGE = r"@([a-zA-Z]+(?:-[a-zA-Z0-9]+)*)"
_LITERAL = rf"{_STRING}(?:{_LANGUAGE}|\^\^{_IRI})?"

_IRI_TERM = re.compile(_IRI)
_BLANK_NODE_TERM = re.compile(_BLANK_NODE)
_LITERAL_TERM = re.compile(_LITERAL)
# Turtle's abbreviated literals, which SPARQL TSV may use.
_ABBREVIATED = {
    f"{XSD}integer": re.compile(r"[+-]?[0-9]+"),
    f"{XSD}decimal": re.compile(r"[+-]?[0-9]*\.[0-9]+"),
    f"{XSD}double": re.compile(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)[eE][+-]?[0-9]+"),
    f"{XSD}boolean": re.compile(r"true|false"),
}
_NT_SUBJECT = rf"(?:{_IRI}|{_BLANK_NODE})"
_NT_OBJECT = rf"(?:{_IRI}|{_BLANK_NODE}|{_LITERAL})"
_NT_TRIPLE = re.compile(rf"[ \t]*{_NT_SUBJECT}[ \t]*{_IRI}[ \t]*{_NT_OBJECT}[ \t]*\.[ \t]*")

_ESCAPE = re.compile(rf"\\[tbnrf\"'\\]|{_UCHAR}")
_ESCAPED_CHARS = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f", '"': '"', "'": "'", "\\": "\\"}
_VARIABLE = re.compile(r"\?([A-Za-z0-9_\u00b7\u00c0-\uffff]+)")


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        escape = match.group()
        if escape[1] in "uU":
            try:
                return chr(int.from_bytes(bytes.fromhex(escape[2:])))
            except (ValueError, OverflowError) as error:  # beyond U+10FFFF
                raise RDFSyntaxError(f"invalid escape {escape}") from error
        return _ESCAPED_CHARS[escape[1]]

    return _ESCAPE.sub(replace, text)


def parse_tsv_term(text: str) -> Term | None:
    """Parse one cell of a SPARQL TSV result: an RDF term in Turtle syntax, or empty for an unbound variable.

    Raises:
        RDFSyntaxError: The cell isn't a term.
    """
    if not text:
        return None
    match text[0]:
        case "<":
            if match := _IRI_TERM.fullmatch(text):
                return IRI(_unescape(match.group(1)))
        case "_":
            if match := _BLANK_NODE_TERM.fullmatch(text):
                return BlankNode(match.group(1))
        case '"':
            if match := _LITERAL_TERM.fullmatch(text):
                value, language, datatype = match.groups()
                if language is not None:
                    return Literal(_unescape(value), RDF_LANG_STRING, language.lower())
                return Literal(_unescape(value), XSD_STRING if datatype is None else _unescape(datatype))
        case _:
            for datatype, pattern in _ABBREVIATED.items():
                if pattern.fullmatch(text):
                    return Literal(text, datatype)
    raise RDFSyntaxError(f"not an RDF term: {text[:200]!r}")


def parse_tsv_header(line: str) -> tuple[str, ...]:
    """Parse the header line of a SPARQL TSV result into its variable names, without the '?'.

    Raises:
        RDFSyntaxError: The line isn't a list of variables.
    """
    if not line:  # a query with no variables
        return ()
    variables = []
    for cell in line.split("\t"):
        match = _VARIABLE.fullmatch(cell)
        if match is None:
            raise RDFSyntaxError(f"not a SPARQL TSV header: {line[:200]!r}")
        variables.append(match.group(1))
    return tuple(variables)


def parse_tsv_row(line: str, width: int) -> tuple[Term | None, ...]:
    """Parse a row of a SPARQL TSV result, which must have `width` cells.

    Raises:
        RDFSyntaxError: The row has the wrong number of cells, or a cell isn't a term.
    """
    cells = line.split("\t") if line or width else []  # a query with no variables has empty rows
    if len(cells) != width:
        raise RDFSyntaxError(f"expected {width} cells, got {len(cells)}: {line[:200]!r}")
    return tuple(parse_tsv_term(cell) for cell in cells)


def check_ntriples_triple(line: str) -> None:
    """Check that a line is one N-Triples triple.

    Jena writes N-Triples one triple per line, without comments or blank lines, so anything else is rejected: notably
    the text Fuseki appends to a result that fails partway, which can start with '#' (S-6).

    Raises:
        RDFSyntaxError: The line isn't a triple.
    """
    if _NT_TRIPLE.fullmatch(line) is None:
        raise RDFSyntaxError(f"not an N-Triples triple: {line[:200]!r}")
