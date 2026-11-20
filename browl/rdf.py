"""RDF terms, and strict parsing of the SPARQL TSV results and N-Triples that Fuseki sends.

Parsing is strict so that a truncated response can't pass as a short result: when a query fails after Fuseki has sent
HTTP 200, it appends the error as plain text and ends the response normally (DESIGN.md §2.14). That text doesn't parse
as RDF terms or N-Triples.
"""

import re
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

XSD = "http://www.w3.org/2001/XMLSchema#"
XSD_STRING = f"{XSD}string"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
RDF_LANG_STRING = f"{RDF}langString"
RDF_DIR_LANG_STRING = f"{RDF}dirLangString"
DIRECTIONS = frozenset({"ltr", "rtl"})
"""RDF 1.2's base directions for language-tagged strings."""


# Patterns from the Turtle and N-Triples grammars (https://www.w3.org/TR/turtle/#sec-grammar-grammar). The pieces
# first; then the term classes' checks of values, and the parsers' checks of terms' syntax in TSV and N-Triples.
_IRI_CHAR = r"[^\x00-\x20<>\"{}|^`\\]"
_BLANK_NODE_LABEL = r"[\w](?:[\w.\-\u00b7\u0300-\u036f\u203f\u2040]*[\w\-\u00b7\u0300-\u036f\u203f\u2040])?"
_LANGUAGE_TAG = r"[a-zA-Z]+(?:-[a-zA-Z0-9]+)*"
_UCHAR = r"\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8}"
# Runs of plain characters are matched possessively (`++`, `*+`): they never need to give characters back, and a long
# IRI or string is then matched in runs instead of a character at a time, with no backtracking when it fails.
_IRI = rf"<((?:{_IRI_CHAR}++|{_UCHAR})*+)>"
_BLANK_NODE = rf"_:({_BLANK_NODE_LABEL})"
_STRING = rf"\"((?:[^\"\\\n\r]++|\\[tbnrf\"'\\]|{_UCHAR})*+)\""
_LANGUAGE = rf"@({_LANGUAGE_TAG})(?:--(ltr|rtl))?"  # RDF 1.2 adds a base direction
_LITERAL = rf"{_STRING}(?:{_LANGUAGE}|\^\^{_IRI})?"

PREFIX_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9._-]*)?")
"""A CURIE prefix name browl accepts, in configuration or from an ontology: ASCII, and empty for the default prefix."""

_IRI_VALUE = re.compile(rf"[A-Za-z][A-Za-z0-9+.\-]*+:{_IRI_CHAR}*+")
_BLANK_NODE_VALUE = re.compile(_BLANK_NODE_LABEL)
_LOWERCASE_LANGUAGE_TAG = re.compile(r"[a-z]+(?:-[a-z0-9]+)*")

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
_XSD_DECIMAL = r"[+-]?+(?:[0-9]++(?:\.[0-9]*+)?+|\.[0-9]++)"
XSD_LEXICAL_FORMS = {
    f"{XSD}integer": _ABBREVIATED[f"{XSD}integer"],
    f"{XSD}decimal": re.compile(_XSD_DECIMAL),
    f"{XSD}float": re.compile(rf"{_XSD_DECIMAL}(?:[eE][+-]?+[0-9]++)?+|[+-]?+INF|NaN"),
    f"{XSD}boolean": re.compile(r"true|false|1|0"),
}
"""XSD 1.1's lexical forms for the datatypes OWLAPI 4's Manchester renderer writes unquoted (`browl.axioms`)."""
_NT_SUBJECT = rf"(?:{_IRI}|{_BLANK_NODE})"
_NT_OBJECT = rf"(?:{_IRI}|{_BLANK_NODE}|{_LITERAL})"
_NT_TRIPLE = re.compile(rf"[ \t]*{_NT_SUBJECT}[ \t]*{_IRI}[ \t]*{_NT_OBJECT}[ \t]*\.[ \t]*")

_ESCAPE = re.compile(r"\\(?:([tbnrf\"'\\])|u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8}))")
_ESCAPED_CHARS = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f", '"': '"', "'": "'", "\\": "\\"}
_KNOWN_DATATYPES = frozenset({XSD_STRING, RDF_LANG_STRING, RDF_DIR_LANG_STRING, *_ABBREVIATED})
"""Datatypes known to be IRIs, which a literal needn't check again."""
_VARIABLE = re.compile(r"\?([A-Za-z0-9_\u00b7\u00c0-\uffff]+)")


class RDFSyntaxError(ValueError):
    """Text isn't valid SPARQL TSV or N-Triples, or a value isn't a valid RDF term."""


class _CheckedStr(str):
    """A string that must match a pattern, checked whenever one is made, inside pydantic models or not.

    A `str` subclass, not an `Annotated[str, StringConstraints(pattern=...)]` alias, because most are made outside
    pydantic, by the SPARQL and N-Triples parsers, and code tells the kinds of term apart by type (`Term`,
    `isinstance(value, IRI)`); an alias is a plain `str` at runtime, with no check on construction and no type of its
    own. `__get_pydantic_core_schema__`, pydantic's hook for custom types, gives models' fields the same check.
    """

    __slots__ = ()
    _pattern: ClassVar[re.Pattern[str]]

    def __new__(cls, value: str) -> Self:
        """Check the value.

        Raises:
            RDFSyntaxError: The value doesn't match the pattern.
        """
        if not cls._pattern.fullmatch(value):
            raise RDFSyntaxError(f"not a valid {cls.__name__}: {value[:200]!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        """Make one of these from a string, so pydantic models' fields get the same check; take one as it is."""
        from_str = core_schema.no_info_after_validator_function(cls, core_schema.str_schema())

        def checked(value: Any, handler: core_schema.ValidatorFunctionWrapHandler) -> Any:
            return value if isinstance(value, cls) else handler(value)

        return core_schema.no_info_wrap_validator_function(checked, from_str)


class IRI(_CheckedStr):
    """An absolute IRI: a scheme, then characters that IRIs allow (no spaces, '<', '>', '"', …)."""

    __slots__ = ()
    _pattern = _IRI_VALUE


class BlankNode(_CheckedStr):
    """A blank node, by its label in one result; labels mean nothing across results."""

    __slots__ = ()
    _pattern = _BLANK_NODE_VALUE


@dataclass(frozen=True, slots=True)
class Literal:
    """An RDF literal, checked when made as RDF 1.2 defines it.

    Its datatype is an IRI; it has a language tag just when it's a langString or a dirLangString, and a base direction
    just when it's a dirLangString.

    Attributes:
        value: The lexical form.
        datatype: The datatype IRI; `RDF_LANG_STRING` for a language-tagged string, `RDF_DIR_LANG_STRING` for one with
            a base direction.
        language: The language tag, lowercased, for a language-tagged string; otherwise None.
        direction: The base direction, "ltr" or "rtl", for a dirLangString; otherwise None.
    """

    value: str
    datatype: str = XSD_STRING
    language: str | None = None
    direction: str | None = None

    def __post_init__(self) -> None:
        """Check the datatype, language tag and base direction.

        Raises:
            RDFSyntaxError: The datatype isn't an IRI, the language tag isn't a lowercase tag, the direction isn't
                one of `DIRECTIONS`, or the tag and direction don't match the datatype.
        """
        if self.datatype not in _KNOWN_DATATYPES:
            IRI(self.datatype)
        if self.language is not None and not _LOWERCASE_LANGUAGE_TAG.fullmatch(self.language):
            raise RDFSyntaxError(f"not a lowercase language tag: {self.language[:200]!r}")
        if self.direction is not None and self.direction not in DIRECTIONS:
            raise RDFSyntaxError(f"not a base direction: {self.direction[:200]!r}")
        if (self.language is not None) != (self.datatype in (RDF_LANG_STRING, RDF_DIR_LANG_STRING)):
            raise RDFSyntaxError(
                "a literal has a language tag if and only if its datatype is rdf:langString or rdf:dirLangString"
            )
        if (self.direction is not None) != (self.datatype == RDF_DIR_LANG_STRING):
            raise RDFSyntaxError("a literal has a base direction if and only if its datatype is rdf:dirLangString")


Term = IRI | BlankNode | Literal


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        escaped, short_hex, long_hex = match.groups()
        if escaped is not None:
            return _ESCAPED_CHARS[escaped]
        code_point = int(short_hex or long_hex, 16)
        # Beyond U+10FFFF, or a UTF-16 surrogate, which isn't a character on its own.
        if code_point > 0x10FFFF or 0xD800 <= code_point <= 0xDFFF:
            raise RDFSyntaxError(f"invalid escape {match.group()}")
        return chr(code_point)

    return _ESCAPE.sub(replace, text)


def _literal(value: str, language: str | None, direction: str | None, datatype: str | None) -> Literal:
    """Make a literal from the escaped parts of its Turtle or N-Triples syntax."""
    if language is not None:  # with or without a direction
        string_type = RDF_LANG_STRING if direction is None else RDF_DIR_LANG_STRING
        return Literal(_unescape(value), string_type, language.lower(), direction)
    return Literal(_unescape(value), XSD_STRING if datatype is None else _unescape(datatype))


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
                return _literal(*match.groups())
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


def _ntriples_triple(line: str) -> tuple[list[IRI], re.Match[str]]:
    """Return a triple's subject, predicate and object IRIs, unescaped and checked, and the line's match.

    Raises:
        RDFSyntaxError: The line isn't a triple, or an IRI isn't valid.
    """
    match = _NT_TRIPLE.fullmatch(line)
    if match is None:
        raise RDFSyntaxError(f"not an N-Triples triple: {line[:200]!r}")
    subject, _, predicate, object_iri = match.groups()[:4]
    return [IRI(_unescape(iri)) for iri in (subject, predicate, object_iri) if iri is not None], match


def check_ntriples_triple(line: str) -> None:
    """Check that a line is one N-Triples triple, whose terms' values are valid as the term classes check them.

    Jena writes N-Triples one triple per line, without comments or blank lines, so anything else is rejected: notably
    the text Fuseki appends to a result that fails partway, which can start with '#' (S-6).

    Raises:
        RDFSyntaxError: The line isn't a triple, or a term's value isn't valid (e.g. a relative IRI).
    """
    _, match = _ntriples_triple(line)
    value, language, direction, datatype = match.groups()[5:]
    if value is not None:
        _literal(value, language, direction, datatype)


def ntriples_iris(ntriples: bytes) -> set[IRI]:
    """Return the IRIs that N-Triples name as a subject, predicate or object (not as a datatype), unescaped.

    Raises:
        RDFSyntaxError: The N-Triples aren't UTF-8, a line isn't a triple, or an IRI isn't valid
            (`check_ntriples_triple`).
    """
    try:
        text = ntriples.decode()
    except UnicodeDecodeError as error:
        raise RDFSyntaxError(f"N-Triples that aren't UTF-8: {error}") from error
    found: set[IRI] = set()
    for line in text.splitlines():
        if line:
            found.update(_ntriples_triple(line)[0])
    return found
