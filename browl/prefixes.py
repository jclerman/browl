"""The prefixes an ontology file declares, for CURIEs (DESIGN.md §2.7, O-11).

Only the start of the file is read: the prefix declarations before its first statement, in Turtle, or on its root
element, in RDF/XML, which is where the OWLAPI, ROBOT and other tools write them. A declaration further on is missed.
"""

import logging
import re
import urllib.parse
import xml.parsers.expat
from pathlib import Path
from typing import BinaryIO

from browl.rdf import IRI, RDFSyntaxError

logger = logging.getLogger(__name__)

HEAD_BYTES = 4 * 1024 * 1024
"""The most of a file's start read for its prefixes: plenty for any header, little for a multi-GB file."""

_CHUNK_BYTES = 64 * 1024

_TURTLE_DIRECTIVE = re.compile(
    r"""(?:\s|\#[^\n]*(?:\n|$))*  # whitespace and comments
    (?:
        (?P<at>@)?(?P<keyword>prefix|base)\s+  # @prefix, @base, or SPARQL-style PREFIX, BASE
        (?:(?P<name>[^\s:<>]*):\s*)?  # a prefix's name (PN_PREFIX); a base has none
        <(?P<iri>[^<>"{}|^`\\\s]*)>
        (?(at)\s*\.)  # @-directives end with "."
    )""",
    re.IGNORECASE | re.VERBOSE,
)
"""A Turtle prefix or base directive, after any whitespace and comments. `@prefix` and `@base` are case-sensitive in
Turtle, but a wrongly cased one is harmless to accept here. IRIs with escapes are skipped (the match fails)."""


def read_prefixes(path: Path) -> dict[str, str]:
    """Return the prefixes a Turtle or RDF/XML file declares at its start, name → namespace IRI.

    The syntax is the one its suffix names: `.ttl` is Turtle, any other RDF/XML (`browl.upload.STORED_SUFFIXES`). A
    namespace that isn't an absolute IRI, once resolved against a Turtle base, is left out. A file that can't be read
    or parsed has none; a warning says why.
    """
    try:
        with path.open("rb") as file:
            if path.suffix == ".ttl":
                return _turtle_prefixes(file.read(HEAD_BYTES).decode("utf-8", "replace"))
            return _xml_prefixes(file)
    except OSError as error:
        logger.warning("Couldn't read the ontology's prefixes from %s: %s", path, error)
        return {}


def _turtle_prefixes(text: str) -> dict[str, str]:
    """Return the prefixes declared by the directives at the start of a Turtle document, until its first statement."""
    prefixes: dict[str, str] = {}
    base: str | None = None
    position = 1 if text.startswith("﻿") else 0
    while match := _TURTLE_DIRECTIVE.match(text, position):
        position = match.end()
        iri = urllib.parse.urljoin(base, match["iri"]) if base else match["iri"]
        if match["keyword"].lower() == "base":
            base = iri
        elif match["name"] is not None:
            _add(prefixes, match["name"], iri)
    return prefixes


class _RootReachedError(Exception):
    """The parser reached the root element's start, which has its namespace declarations."""


def _xml_prefixes(file: BinaryIO) -> dict[str, str]:
    """Return the namespaces declared on the root element of an XML document; the default namespace's name is ""."""
    prefixes: dict[str, str] = {}
    parser = xml.parsers.expat.ParserCreate(namespace_separator=" ")

    def declare(name: str | None, uri: str) -> None:
        _add(prefixes, name or "", uri)

    def start(name: str, attributes: dict[str, str]) -> None:
        raise _RootReachedError

    parser.StartNamespaceDeclHandler = declare
    parser.StartElementHandler = start
    remaining = HEAD_BYTES
    try:
        while remaining > 0 and (chunk := file.read(min(_CHUNK_BYTES, remaining))):
            remaining -= len(chunk)
            parser.Parse(chunk, False)
    except _RootReachedError:
        return prefixes
    except xml.parsers.expat.ExpatError as error:
        logger.warning("Couldn't read the ontology's prefixes: its XML doesn't parse (%s)", error)
        return {}
    logger.warning("Couldn't read the ontology's prefixes: no root element in its first %d bytes", HEAD_BYTES)
    return {}


def _add(prefixes: dict[str, str], name: str, namespace: str) -> None:
    try:
        prefixes[name] = IRI(namespace)
    except RDFSyntaxError:
        logger.warning("The ontology's prefix %r stands for %r, not an absolute IRI; it's left out", name, namespace)
