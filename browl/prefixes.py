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

from browl.rdf import IRI, PREFIX_NAME, RDFSyntaxError

logger = logging.getLogger(__name__)

HEAD_BYTES = 4 * 1024 * 1024
"""The most of a file's start read for its prefixes: plenty for any header, little for a multi-GB file."""

_CHUNK_BYTES = 64 * 1024
_LOGGED_WARNINGS = 10  # the most warnings one file's prefixes log, so a hostile header can't flood the log
_LOGGED_CHARS = 200  # the most characters of a name or IRI a warning shows

_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_URL_SCHEMES = frozenset({"http", "https", "ftp", "file", "urn", "mailto", "data", "javascript"})

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
    namespace that isn't an absolute IRI, once resolved against a Turtle base, is left out, as is a name that isn't a
    prefix name (`PREFIX_NAME`) or is a URL scheme. A file that can't be read or parsed has none; a warning says why.
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
    prefixes = _Prefixes()
    base: str | None = None
    position = 1 if text.startswith("\ufeff") else 0
    while match := _TURTLE_DIRECTIVE.match(text, position):
        position = match.end()
        try:
            iri = _resolve(base, match["iri"])
        except ValueError as error:  # urljoin's, e.g. for a malformed IPv6 host
            message = "The ontology's %s <%s> doesn't resolve (%s); it's left out"
            prefixes.warn(message, match["keyword"], match["iri"], str(error))
            continue
        if match["keyword"].lower() == "base":
            base = iri
        elif match["name"] is not None:
            prefixes.add(match["name"], iri)
    return prefixes.found


def _resolve(base: str | None, reference: str) -> str:
    """Return an IRI reference resolved against the base, if it's relative and there is one.

    `urljoin` drops an empty fragment or query, so a namespace's trailing "#" or "?" is put back.

    Raises:
        ValueError: The base or reference doesn't parse as a URL.
    """
    if base is None or _SCHEME.match(reference):
        return reference
    resolved = urllib.parse.urljoin(base, reference)
    if reference.endswith(("#", "?")) and not resolved.endswith(reference[-1]):
        resolved += reference[-1]
    return resolved


class _RootReachedError(Exception):
    """The parser reached the root element's start, which has its namespace declarations."""


def _xml_prefixes(file: BinaryIO) -> dict[str, str]:
    """Return the namespaces declared on the root element of an XML document; the default namespace's name is ""."""
    prefixes = _Prefixes()
    parser = xml.parsers.expat.ParserCreate(namespace_separator=" ")
    parser.StartNamespaceDeclHandler = lambda name, uri: prefixes.add(name or "", uri)

    def start(*_: object) -> None:
        raise _RootReachedError

    parser.StartElementHandler = start
    remaining = HEAD_BYTES
    try:
        while remaining > 0 and (chunk := file.read(min(_CHUNK_BYTES, remaining))):
            remaining -= len(chunk)
            parser.Parse(chunk, False)
    except _RootReachedError:
        return prefixes.found
    except xml.parsers.expat.ExpatError as error:
        logger.warning("Couldn't read the ontology's prefixes: its XML doesn't parse (%s)", error)
        return {}
    logger.warning("Couldn't read the ontology's prefixes: no root element in its first %d bytes", HEAD_BYTES)
    return {}


class _Prefixes:
    """The prefixes found so far, and the warnings about those left out, of which only the first few are logged."""

    def __init__(self) -> None:
        self.found: dict[str, str] = {}
        self._warnings = 0

    def add(self, name: str, namespace: str) -> None:
        """Add a prefix, unless its name isn't a prefix name or is a URL scheme, or its namespace isn't an IRI."""
        if not PREFIX_NAME.fullmatch(name) or name.lower() in _URL_SCHEMES:
            # A scheme as a prefix makes CURIEs that look like URLs (`http://…`); other names may not parse as CURIEs.
            self.warn("The ontology's prefix name %r isn't allowed; it's left out", name)
            return
        try:
            self.found[name] = IRI(namespace)
        except RDFSyntaxError:
            self.warn("The ontology's prefix %r stands for %r, not an absolute IRI; it's left out", name, namespace)

    def warn(self, message: str, *args: str) -> None:
        """Log a warning, its values cut short, unless `_LOGGED_WARNINGS` have been; then say once that more follow."""
        self._warnings += 1
        if self._warnings <= _LOGGED_WARNINGS:
            logger.warning(message, *(_shorten(arg) for arg in args))
        elif self._warnings == _LOGGED_WARNINGS + 1:
            logger.warning("More of the ontology's prefixes are left out; their warnings aren't logged")


def _shorten(text: str) -> str:
    return text if len(text) <= _LOGGED_CHARS else f"{text[:_LOGGED_CHARS]}…"
