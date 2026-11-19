"""Manchester syntax text, as the OWLAPI renders it with full IRIs, split into segments for templates (DESIGN.md §2.8).

The renderer writes every entity as its full IRI, `<…>`, so a template can show each IRI that's an entity as a link,
and set keywords apart (§2.9). Splitting the text here, rather than making HTML, leaves escaping to Jinja's autoescape:
templates render each segment as text, a link or a keyword.

IRIs and keywords are found only outside quoted literals, so `<…>` in a literal's text stays part of the literal; an
unclosed literal runs to the end of the text. Given the IRIs an axiom is about, `segments` takes no other `<…>` for an
IRI. An IRI in a standard vocabulary (`xsd:`, `rdf:`, `rdfs:`, `owl:`), such as a built-in datatype, has its prefixed
name, which a template shows unless the IRI has a page.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from browl.names import BUILTIN_PREFIXES

KEYWORDS = frozenset(
    {
        "AnnotationProperty",
        "Annotations",
        "AntiSymmetric",
        "Asymmetric",
        "Characteristics",
        "Class",
        "DataProperty",
        "Datatype",
        "DifferentFrom",
        "DifferentIndividuals",
        "DisjointClasses",
        "DisjointProperties",
        "DisjointUnionOf",
        "DisjointWith",
        "Domain",
        "EquivalentClasses",
        "EquivalentProperties",
        "EquivalentTo",
        "Facts",
        "Functional",
        "HasKey",
        "Import",
        "Individual",
        "Individuals",
        "InverseFunctional",
        "InverseOf",
        "Inverses",
        "Irreflexive",
        "ObjectProperty",
        "Ontology",
        "Prefix",
        "Range",
        "Reflexive",
        "Rule",
        "SameAs",
        "SameIndividual",
        "Self",
        "SubClassOf",
        "SubPropertyChain",
        "SubPropertyOf",
        "SuperClassOf",
        "SuperPropertyOf",
        "Symmetric",
        "Transitive",
        "Type",
        "Types",
        "ValuePartition",
        "and",
        "exactly",
        "false",
        "inv",
        "inverse",
        "max",
        "min",
        "not",
        "o",
        "only",
        "onlysome",
        "or",
        "some",
        "that",
        "true",
        "value",
    }
)
"""The words of the OWLAPI's Manchester syntax keywords (`ManchesterOWLSyntax`), without their colons; a test checks
them against the OWLAPI's own list, so the template layer needs no JVM."""

STANDARD_PREFIXES = {BUILTIN_PREFIXES[prefix]: prefix for prefix in ("xsd", "rdf", "rdfs", "owl")}
"""The standard vocabularies' namespaces, and the prefix each IRI in them is shown with."""

_IRI = r"<[^\x00-\x20<>\"{}|^`\\]*+>"
# A literal left unclosed runs to the end of the text, so the scan stays linear.
_LITERAL = r'"(?:[^"\\]++|\\[\s\S]?)*+(?:"|\Z)(?:@[A-Za-z0-9-]+)?'
_TOKEN = re.compile(rf"(?P<literal>{_LITERAL})|(?P<iri>{_IRI})")
_KEYWORD = re.compile(rf"\b(?:{'|'.join(sorted(KEYWORDS, key=len, reverse=True))})\b:?")
_LINE_BREAK = re.compile(r"(?<!\s)\s*\n\s*")  # matched only from the start of a run of spaces, so linear
_LOCAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


class SegmentKind(StrEnum):
    """What a segment of Manchester text is."""

    TEXT = "text"
    KEYWORD = "keyword"
    IRI = "iri"
    LITERAL = "literal"


@dataclass(frozen=True)
class Segment:
    """A piece of Manchester text.

    Attributes:
        kind: What it is.
        text: Its text, as rendered: an IRI's includes its angle brackets, a literal's its quotes and language tag.
    """

    kind: SegmentKind
    text: str

    @property
    def iri(self) -> str:
        """Return an IRI segment's IRI, without its angle brackets."""
        return self.text[1:-1]

    @property
    def standard_name(self) -> str | None:
        """Return an IRI segment's prefixed name if it's in a standard vocabulary (`xsd:integer`), else None."""
        return standard_name(self.iri) if self.kind == SegmentKind.IRI else None


def standard_name(iri: str) -> str | None:
    """Return an IRI's prefixed name if it's in a standard vocabulary (`STANDARD_PREFIXES`), else None."""
    for namespace, prefix in STANDARD_PREFIXES.items():
        local = iri.removeprefix(namespace)
        if local != iri and _LOCAL_NAME.fullmatch(local):
            return f"{prefix}:{local}"
    return None


def _split(text: str) -> list[tuple[SegmentKind, str]]:
    """Split text into literals, IRIs and the text between them, which holds keywords and punctuation."""
    pieces = []
    end = 0
    for match in _TOKEN.finditer(text):
        if match.start() > end:
            pieces.append((SegmentKind.TEXT, text[end : match.start()]))
        pieces.append((SegmentKind.LITERAL if match["literal"] else SegmentKind.IRI, match[0]))
        end = match.end()
    if end < len(text):
        pieces.append((SegmentKind.TEXT, text[end:]))
    return pieces


def segments(text: str, own: Iterable[str] | None = None) -> list[Segment]:
    """Return Manchester text split into segments: literals, IRIs, keywords, and the text between.

    Args:
        text: The text.
        own: The IRIs of the entities and datatypes the text is about (`browl.axioms.Axiom.iris`); any other `<…>` is a
            text segment. None takes every `<…>` for an IRI.
    """
    known = None if own is None else frozenset(own)
    result = []
    for kind, piece in _split(text):
        if kind == SegmentKind.IRI and known is not None and piece[1:-1] not in known:
            result.append(Segment(SegmentKind.TEXT, piece))
            continue
        if kind != SegmentKind.TEXT:
            result.append(Segment(kind, piece))
            continue
        end = 0
        for match in _KEYWORD.finditer(piece):
            if match.start() > end:
                result.append(Segment(SegmentKind.TEXT, piece[end : match.start()]))
            result.append(Segment(SegmentKind.KEYWORD, match[0]))
            end = match.end()
        if end < len(piece):
            result.append(Segment(SegmentKind.TEXT, piece[end:]))
    return result


def iris(text: str) -> list[str]:
    """Return the IRIs in Manchester text, outside literals, in order of first appearance."""
    return list(dict.fromkeys(piece[1:-1] for kind, piece in _split(text) if kind == SegmentKind.IRI))


def one_line(text: str) -> str:
    """Return Manchester text on one line: the renderer breaks lines in long expressions, but not inside literals."""
    return "".join(
        _LINE_BREAK.sub(" ", piece) if kind == SegmentKind.TEXT else piece for kind, piece in _split(text)
    ).strip()
