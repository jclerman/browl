"""Async SPARQL client for Fuseki: SELECT results read as TSV, DESCRIBE and CONSTRUCT results as N-Triples.

Every response is checked strictly, because a query that fails after Fuseki has sent HTTP 200 still ends normally,
with the error appended as text (DESIGN.md §2.14). A query fails, raising `SparqlError`, when Fuseki answers with any
status other than 2xx, when the connection fails or the body ends early, when it doesn't answer in time, and when the
result doesn't parse. A failed query never looks like a short result. A result that breaks off cleanly at a row
boundary, or fails with no error text appended, still looks whole to that parsing, so bulk queries go through
`FusekiClient.bulk_select`, which checks that the result reached its end (P-9).
"""

import asyncio
import codecs
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx2

from browl.rdf import XSD, Literal, RDFSyntaxError, Term, check_ntriples_triple, parse_tsv_header, parse_tsv_row

QUERY_TIMEOUT_S = 30.0
"""Time limit for a whole buffered query (`select`, `construct`): ample for a page's queries (2–4 ms each, S-4)."""

STREAM_READ_TIMEOUT_S = 600.0
"""Time limit for each wait for data while streaming, whose total time depends on the ontology's size."""

CONSTRUCT_MAX_BYTES = 64 * 2**20
"""Largest result `construct` holds. Its result is parsed by the OWLAPI, and the JVM exits on running out of heap, so
a huge fragment could crash the app again on every retry. The default heap (`APP_JVM_MAX_HEAP`, 1g) parsed 160 MB of
N-Triples, all of Uberon, even at 384 MB (research/P-4); a page's fragment is a few thousand triples at most."""

MAX_LINE_CHARS = 16 * 2**20
"""Longest line accepted in any result, so a malformed one can't grow without bound."""

TSV = "text/tab-separated-values"
NTRIPLES = "application/n-triples"
_ERROR_TEXT_LIMIT = 1000  # characters of an error response kept in the exception
_HTTP_TIMEOUT = httpx2.Timeout(QUERY_TIMEOUT_S, read=STREAM_READ_TIMEOUT_S)
# Fuseki's datasets have plain names; anything else could reach other endpoints, such as the unauthenticated admin
# protocol's under "/$/".
_DATASET = re.compile(r"[A-Za-z0-9_-]+")


class SparqlError(Exception):
    """A SPARQL query failed: Fuseki refused it or failed, the connection broke, or the result is malformed."""


class SparqlTimeoutError(SparqlError):
    """A SPARQL query didn't finish in time."""


@dataclass(frozen=True, slots=True)
class SelectResult:
    """A SELECT query's whole result.

    Attributes:
        variables: The result's variables, without '?', in column order.
        rows: One tuple per solution, in column order; None for an unbound variable.
    """

    variables: tuple[str, ...]
    rows: list[tuple[Term | None, ...]]


class SelectStream:
    """A SELECT result read as it arrives: iterate it for the rows, each a tuple in column order.

    Attributes:
        variables: The result's variables, without '?', in column order.
    """

    def __init__(self, variables: tuple[str, ...], batches: AsyncIterator[list[str]]) -> None:
        """Wrap the batches of lines after the header line."""
        self.variables = variables
        self._batches = batches

    async def __aiter__(self) -> AsyncIterator[tuple[Term | None, ...]]:
        """Yield each row, parsed.

        Raises:
            SparqlError: The rest of the result fails or doesn't parse.
        """
        width = len(self.variables)
        async for batch in self._batches:
            for line in batch:
                try:
                    row = parse_tsv_row(line, width)
                except RDFSyntaxError as error:
                    raise SparqlError(f"malformed SELECT result: {error}") from error
                yield row


END_VARIABLE = "browl_end"
"""The variable that marks a bulk result's last row (`FusekiClient.bulk_select`); bulk queries mustn't use it."""

_END = Literal("true", f"{XSD}boolean")


class BulkSelectStream:
    """A bulk SELECT result read as it arrives and checked to be whole: iterate it for the rows, in column order.

    The query's result ends in a marker row, which binds only `END_VARIABLE`; every other row leaves it unbound. The
    marker isn't yielded, and the column is left out of `variables` and the rows.

    Attributes:
        variables: The result's variables, without '?', in column order; the column order is Fuseki's, so look columns
            up by name.
    """

    def __init__(self, stream: SelectStream) -> None:
        """Wrap a stream whose query was wrapped by `bulk_select`.

        Raises:
            SparqlError: The result has no `END_VARIABLE` column.
        """
        if stream.variables.count(END_VARIABLE) != 1:
            raise SparqlError(f"a bulk SELECT result has no {END_VARIABLE} column: {stream.variables}")
        self._end = stream.variables.index(END_VARIABLE)
        self.variables = stream.variables[: self._end] + stream.variables[self._end + 1 :]
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[tuple[Term | None, ...]]:
        """Yield each row but the end marker.

        Raises:
            SparqlError: The rest of the result fails or doesn't parse, it has no end marker, or rows follow it.
        """
        end = self._end
        last = end == len(self.variables)  # as Fuseki puts it, so the row needn't be put back together
        ended = False
        rows = 0
        async for row in self._stream:
            if ended:
                raise SparqlError("the result has rows after its end marker")
            marker = row[end]
            if marker is None:
                rows += 1
                yield row[:end] if last else row[:end] + row[end + 1 :]
            elif marker == _END and row.count(None) == len(row) - 1:
                ended = True
            else:
                raise SparqlError(f"the result binds {END_VARIABLE} on a row that isn't its end marker")
        if not ended:
            raise SparqlError(f"the result is incomplete: it broke off after {rows:,} rows without its end marker")


UNION_FROM = "FROM <urn:x-arq:DefaultGraph> FROM <urn:x-arq:UnionGraph>"
"""The dataset clause of a query that sees the ontology and its imports: the default graph merged with every named
graph, as a set, so a triple both state counts once (DESIGN.md §2.4)."""

ADMIN_TIMEOUT_S = 120.0
"""Time limit for an admin protocol call: opening a store is quick, but removing one deletes its files."""


def dataset_clause(has_imports: bool) -> str:
    """Return the dataset clause for queries that should see the imports: `UNION_FROM` if there are any, else ""."""
    return UNION_FROM if has_imports else ""


PING_TIMEOUT_S = 2.0
"""Time limit for the whole of a ping (`ping`)."""


async def ping(http: httpx2.AsyncClient, timeout_s: float = PING_TIMEOUT_S) -> bool:
    """Return whether Fuseki answers its ping (`/$/ping`) with success within `timeout_s`, over a client for Fuseki."""
    try:
        # httpx2's own timeout limits each phase (connect, write, read) separately, not the whole call.
        async with asyncio.timeout(timeout_s):
            response = await http.get("/$/ping")
    except (httpx2.HTTPError, TimeoutError):
        return False
    return response.is_success


class FusekiAdminError(Exception):
    """A call to Fuseki's admin protocol failed."""


class FusekiAdmin:
    """Registers and removes Fuseki's TDB2 datasets through its admin protocol (`/$/datasets`, DESIGN.md §2.4)."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        """Use an HTTP client whose base URL is Fuseki's (`FUSEKI_URL`)."""
        self._http = http

    async def datasets(self) -> list[str]:
        """Return the registered datasets' names.

        Raises:
            FusekiAdminError: The call failed, or its answer isn't a dataset list.
        """
        response = await self._call("GET", "/$/datasets")
        try:
            return [entry["ds.name"].removeprefix("/") for entry in response.json()["datasets"]]
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise FusekiAdminError(f"Fuseki's dataset list is malformed: {error!r}") from error

    async def add(self, name: str) -> None:
        """Register the TDB2 dataset whose store is at `{FUSEKI_BASE}/databases/{name}`, opening it.

        Raises:
            FusekiAdminError: The call failed.
        """
        await self._call("POST", "/$/datasets", data={"dbName": _checked(name), "dbType": "tdb2"})

    async def remove(self, name: str) -> None:
        """Remove a dataset, which deletes its store.

        Raises:
            FusekiAdminError: The call failed.
        """
        await self._call("DELETE", f"/$/datasets/{_checked(name)}")

    async def _call(self, method: str, path: str, data: dict[str, str] | None = None) -> httpx2.Response:
        try:
            async with asyncio.timeout(ADMIN_TIMEOUT_S):
                # The app's client has httpx's 5 s default, too short to delete or open a large store.
                response = await self._http.request(method, path, data=data, timeout=ADMIN_TIMEOUT_S)
        except TimeoutError as error:
            raise FusekiAdminError(
                f"Fuseki HTTP request {method} {path} timed out after {ADMIN_TIMEOUT_S:g} s"
            ) from error
        except httpx2.HTTPError as error:
            raise FusekiAdminError(f"Fuseki HTTP request {method} {path} failed: {error!r}") from error
        if not response.is_success:
            text = response.text[:_ERROR_TEXT_LIMIT].strip()
            raise FusekiAdminError(f"Fuseki HTTP request {method} {path} answered {response.status_code}: {text}")
        return response


def _checked(name: str) -> str:
    if not _DATASET.fullmatch(name):
        raise ValueError(f"not a dataset name: {name!r}")
    return name


class FusekiClient:
    """Sends SPARQL queries to Fuseki's datasets, over the app's HTTP client for Fuseki."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        """Use an HTTP client whose base URL is Fuseki's (`FUSEKI_URL`)."""
        self._http = http

    async def select(self, dataset: str, query: str, timeout_s: float = QUERY_TIMEOUT_S) -> SelectResult:
        """Run a SELECT query on a dataset, returning its whole result.

        Raises:
            SparqlError: The query failed, or its result is malformed.
            SparqlTimeoutError: The query took longer than `timeout_s`.
        """
        async with _time_limit(timeout_s), self.select_stream(dataset, query) as stream:
            return SelectResult(stream.variables, [row async for row in stream])

    async def construct(
        self, dataset: str, query: str, timeout_s: float = QUERY_TIMEOUT_S, max_bytes: int = CONSTRUCT_MAX_BYTES
    ) -> bytes:
        """Run a DESCRIBE or CONSTRUCT query on a dataset, returning its result as checked N-Triples.

        Raises:
            SparqlError: The query failed, its result isn't N-Triples, or it's larger than `max_bytes`.
            SparqlTimeoutError: The query took longer than `timeout_s`.
        """
        lines = []
        size = 0
        async with _time_limit(timeout_s), self.construct_stream(dataset, query) as stream:
            async for line in stream:
                encoded = line.encode()
                size += len(encoded) + 1
                if size > max_bytes:
                    raise SparqlError(f"the result is larger than {max_bytes} bytes")
                lines.append(encoded)
        lines.append(b"")  # for the last line's end
        return b"\n".join(lines)

    @asynccontextmanager
    async def select_stream(self, dataset: str, query: str) -> AsyncIterator[SelectStream]:
        """Run a SELECT query on a dataset, reading its result as it arrives, for results too large to hold.

        Each wait for data has a time limit (`STREAM_READ_TIMEOUT_S`), but the whole query has none.

        Raises:
            SparqlError: The query failed, or its result is malformed (also while iterating).
            SparqlTimeoutError: Fuseki sent no data for `STREAM_READ_TIMEOUT_S` (also while iterating).
        """
        async with self._query(dataset, query, TSV) as batches:
            first = await anext(batches, None)
            if first is None:
                raise SparqlError("empty SELECT result: no header line")
            header, *rows = first
            try:
                variables = parse_tsv_header(header)
            except RDFSyntaxError as error:
                raise SparqlError(f"malformed SELECT result: {error}") from error
            yield SelectStream(variables, _prepend(rows, batches))

    @asynccontextmanager
    async def bulk_select(
        self, dataset: str, query: str, prologue: str = "", dataset_clause: str = ""
    ) -> AsyncIterator[BulkSelectStream]:
        """Run a SELECT query on a dataset, reading its result as it arrives, and check that the result is whole.

        Use it for every bulk query: a result can break off cleanly at a row boundary, or fail with no error text
        appended, and still parse. The query becomes a subquery, followed by a marker row, which `BulkSelectStream`
        checks comes last (why that works and costs nothing: DESIGN.md §2.14, the truncated-result row):

            {prologue} SELECT * {dataset_clause} WHERE { { {query} } UNION { BIND(true AS ?browl_end) } }

        Each wait for data has a time limit (`STREAM_READ_TIMEOUT_S`), but the whole query has none.

        Args:
            dataset: The dataset's name.
            query: A SELECT query without a prologue or dataset clause, which can't be in a subquery. Its solutions
                come in no particular order, and its columns in Fuseki's (`BulkSelectStream.variables`).
            prologue: The query's `PREFIX` and `BASE` declarations.
            dataset_clause: Its `FROM` clauses (e.g. `dataset_clause`).

        Raises:
            ValueError: The query uses `END_VARIABLE`, or the dataset's name isn't letters, digits, '_' and '-'.
            SparqlError: The query failed, or its result is malformed or incomplete (also while iterating).
            SparqlTimeoutError: Fuseki sent no data for `STREAM_READ_TIMEOUT_S` (also while iterating).
        """
        if END_VARIABLE in query:
            raise ValueError(f"a bulk query mustn't use the variable {END_VARIABLE}")
        wrapped = (
            f"{prologue}\nSELECT * {dataset_clause}\n"
            f"WHERE {{\n{{\n{query}\n}}\nUNION\n{{ BIND(true AS ?{END_VARIABLE}) }}\n}}\n"
        )
        async with self.select_stream(dataset, wrapped) as stream:
            yield BulkSelectStream(stream)

    @asynccontextmanager
    async def construct_stream(self, dataset: str, query: str) -> AsyncIterator[AsyncIterator[str]]:
        """Run a DESCRIBE or CONSTRUCT query on a dataset, yielding its N-Triples lines, each checked, as they arrive.

        Each wait for data has a time limit (`STREAM_READ_TIMEOUT_S`), but the whole query has none.

        Raises:
            SparqlError: The query failed, or its result isn't N-Triples (also while iterating).
            SparqlTimeoutError: Fuseki sent no data for `STREAM_READ_TIMEOUT_S` (also while iterating).
        """

        async def checked(batches: AsyncIterator[list[str]]) -> AsyncIterator[str]:
            async for batch in batches:
                for line in batch:
                    try:
                        check_ntriples_triple(line)
                    except RDFSyntaxError as error:
                        raise SparqlError(f"malformed N-Triples result: {error}") from error
                    yield line

        async with self._query(dataset, query, NTRIPLES) as batches:
            yield checked(batches)

    @asynccontextmanager
    async def _query(self, dataset: str, query: str, accept: str) -> AsyncIterator[AsyncIterator[list[str]]]:
        """Send a query and yield the response's lines in batches, once its status and content type are checked.

        Raises:
            ValueError: The dataset's name isn't letters, digits, '_' and '-'.
        """
        if not _DATASET.fullmatch(dataset):
            raise ValueError(f"not a dataset name: {dataset!r}")
        request = self._http.build_request(
            "POST", f"/{dataset}/sparql", data={"query": query}, headers={"Accept": accept}, timeout=_HTTP_TIMEOUT
        )
        try:
            response = await self._http.send(request, stream=True)
        except httpx2.TimeoutException as error:
            raise SparqlTimeoutError(f"Fuseki didn't answer in time: {error!r}") from error
        except httpx2.HTTPError as error:
            raise SparqlError(f"couldn't reach Fuseki: {error!r}") from error
        try:
            if not response.is_success:
                raise SparqlError(f"Fuseki answered {response.status_code}: {await _error_text(response)}")
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != accept:
                raise SparqlError(f"Fuseki sent {content_type or 'no content type'}, not {accept}")
            yield _batches(response)
        finally:
            await response.aclose()


@asynccontextmanager
async def _time_limit(timeout_s: float) -> AsyncIterator[None]:
    """Limit the whole of a block's time, not each phase of an HTTP call as httpx2's timeouts do."""
    try:
        async with asyncio.timeout(timeout_s):
            yield
    except TimeoutError as error:
        raise SparqlTimeoutError(f"query took longer than {timeout_s} s") from error


async def _batches(response: httpx2.Response) -> AsyncIterator[list[str]]:
    """Yield the body's lines as they arrive, decoded as UTF-8, without their line ends: each chunk's complete lines.

    Lines end only at LF (or CRLF): unlike `aiter_lines`, no other character counts as a line end, so a literal with a
    raw U+2028 stays on its line. Yielding a chunk's lines together, never an empty list, halves the async generator
    steps per line, which bulk results of millions of lines notice.

    Raises:
        SparqlError: The body ends early, the connection breaks, the body isn't UTF-8, or a line is longer than
            `MAX_LINE_CHARS`.
        SparqlTimeoutError: Fuseki sent no data for the read time limit.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    unfinished: list[str] = []  # the pieces of the line that the chunks so far end in, joined once it ends
    unfinished_chars = 0
    try:
        async for chunk in response.aiter_bytes():
            *lines, rest = decoder.decode(chunk).split("\n")
            if lines:
                unfinished.append(lines[0])
                lines[0] = "".join(unfinished)
                unfinished = []
                unfinished_chars = 0
            if lines:
                if max(map(len, lines)) > MAX_LINE_CHARS:
                    raise _line_too_long()
                yield [line.removesuffix("\r") for line in lines]
            unfinished.append(rest)
            unfinished_chars += len(rest)
            if unfinished_chars > MAX_LINE_CHARS:
                raise _line_too_long()
        unfinished.append(decoder.decode(b"", final=True))
    except httpx2.TimeoutException as error:
        raise SparqlTimeoutError(f"Fuseki stopped sending the result: {error!r}") from error
    except httpx2.HTTPError as error:  # e.g. the body ended before its last chunk: Fuseki exited or restarted
        raise SparqlError(f"the result broke off: {error!r}") from error
    except UnicodeDecodeError as error:
        raise SparqlError(f"the result isn't UTF-8: {error}") from error
    if last := "".join(unfinished):
        yield [last.removesuffix("\r")]


async def _prepend(batch: list[str], batches: AsyncIterator[list[str]]) -> AsyncIterator[list[str]]:
    """Yield a batch, then the rest."""
    yield batch
    async for rest in batches:
        yield rest


def _line_too_long() -> SparqlError:
    return SparqlError(f"the result has a line longer than {MAX_LINE_CHARS} characters")


async def _error_text(response: httpx2.Response) -> str:
    """Return the start of an error response's body, or a note that it couldn't be read."""
    try:
        body = await response.aread()
    except httpx2.HTTPError as error:
        return f"(error body not read: {error!r})"
    return body.decode("utf-8", errors="replace")[:_ERROR_TEXT_LIMIT].strip()
