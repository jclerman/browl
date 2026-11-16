"""Async SPARQL client for Fuseki: SELECT results read as TSV, DESCRIBE and CONSTRUCT results as N-Triples.

Every response is checked strictly, because a query that fails after Fuseki has sent HTTP 200 still ends normally,
with the error appended as text (DESIGN.md §2.14). A query fails, raising `SparqlError`, when Fuseki answers with any
status other than 2xx, when the connection fails or the body ends early, when it doesn't answer in time, and when the
result doesn't parse. A failed query never looks like a short result. Bulk results that may break off cleanly at a row
boundary need their own completeness check (P-8).
"""

import asyncio
import codecs
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx2

from browl.rdf import RDFSyntaxError, Term, check_ntriples_triple, parse_tsv_header, parse_tsv_row

QUERY_TIMEOUT_S = 30.0
"""Time limit for a whole buffered query (`select`, `construct`): ample for a page's queries (2–4 ms each, S-4)."""

STREAM_READ_TIMEOUT_S = 600.0
"""Time limit for each wait for data while streaming, whose total time depends on the ontology's size."""

TSV = "text/tab-separated-values"
NTRIPLES = "application/n-triples"
_ERROR_TEXT_LIMIT = 1000  # characters of an error response kept in the exception


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

    def __init__(self, variables: tuple[str, ...], lines: AsyncIterator[str]) -> None:
        """Wrap the lines after the header line."""
        self.variables = variables
        self._lines = lines

    async def __aiter__(self) -> AsyncIterator[tuple[Term | None, ...]]:
        """Yield each row, parsed.

        Raises:
            SparqlError: The rest of the result fails or doesn't parse.
        """
        async for line in self._lines:
            try:
                yield parse_tsv_row(line, len(self.variables))
            except RDFSyntaxError as error:
                raise SparqlError(f"malformed SELECT result: {error}") from error


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

    async def construct(self, dataset: str, query: str, timeout_s: float = QUERY_TIMEOUT_S) -> bytes:
        """Run a DESCRIBE or CONSTRUCT query on a dataset, returning its result as checked N-Triples.

        Raises:
            SparqlError: The query failed, or its result isn't N-Triples.
            SparqlTimeoutError: The query took longer than `timeout_s`.
        """
        async with _time_limit(timeout_s), self.construct_stream(dataset, query) as lines:
            return b"".join([line.encode() + b"\n" async for line in lines])

    @asynccontextmanager
    async def select_stream(self, dataset: str, query: str) -> AsyncIterator[SelectStream]:
        """Run a SELECT query on a dataset, reading its result as it arrives, for results too large to hold.

        Each wait for data has a time limit (`STREAM_READ_TIMEOUT_S`), but the whole query has none.

        Raises:
            SparqlError: The query failed, or its result is malformed (also while iterating).
            SparqlTimeoutError: Fuseki sent no data for `STREAM_READ_TIMEOUT_S` (also while iterating).
        """
        async with self._query(dataset, query, TSV) as lines:
            try:
                header = await anext(lines)
            except StopAsyncIteration:
                raise SparqlError("empty SELECT result: no header line") from None
            try:
                variables = parse_tsv_header(header)
            except RDFSyntaxError as error:
                raise SparqlError(f"malformed SELECT result: {error}") from error
            yield SelectStream(variables, lines)

    @asynccontextmanager
    async def construct_stream(self, dataset: str, query: str) -> AsyncIterator[AsyncIterator[str]]:
        """Run a DESCRIBE or CONSTRUCT query on a dataset, yielding its N-Triples lines, each checked, as they arrive.

        Each wait for data has a time limit (`STREAM_READ_TIMEOUT_S`), but the whole query has none.

        Raises:
            SparqlError: The query failed, or its result isn't N-Triples (also while iterating).
            SparqlTimeoutError: Fuseki sent no data for `STREAM_READ_TIMEOUT_S` (also while iterating).
        """

        async def checked(lines: AsyncIterator[str]) -> AsyncIterator[str]:
            async for line in lines:
                try:
                    check_ntriples_triple(line)
                except RDFSyntaxError as error:
                    raise SparqlError(f"malformed N-Triples result: {error}") from error
                yield line

        async with self._query(dataset, query, NTRIPLES) as lines:
            yield checked(lines)

    @asynccontextmanager
    async def _query(self, dataset: str, query: str, accept: str) -> AsyncIterator[AsyncIterator[str]]:
        """Send a query and yield the response's lines, once its status and content type are checked."""
        timeout = httpx2.Timeout(QUERY_TIMEOUT_S, read=STREAM_READ_TIMEOUT_S)
        request = self._http.build_request(
            "POST", f"/{dataset}/sparql", data={"query": query}, headers={"Accept": accept}, timeout=timeout
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
            yield _lines(response)
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


async def _lines(response: httpx2.Response) -> AsyncIterator[str]:
    """Yield the body's lines as they arrive, decoded as UTF-8, without their line ends.

    Lines end only at LF (or CRLF): unlike `aiter_lines`, no other character counts as a line end, so a
    literal with a raw U+2028 stays on its line.

    Raises:
        SparqlError: The body ends early, the connection breaks, or the body isn't UTF-8.
        SparqlTimeoutError: Fuseki sent no data for the read time limit.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    try:
        async for chunk in response.aiter_bytes():
            pending += decoder.decode(chunk)
            *lines, pending = pending.split("\n")
            for line in lines:
                yield line.removesuffix("\r")
        pending += decoder.decode(b"", final=True)
    except httpx2.TimeoutException as error:
        raise SparqlTimeoutError(f"Fuseki stopped sending the result: {error!r}") from error
    except httpx2.HTTPError as error:  # e.g. the body ended before its last chunk: Fuseki exited or restarted
        raise SparqlError(f"the result broke off: {error!r}") from error
    except UnicodeDecodeError as error:
        raise SparqlError(f"the result isn't UTF-8: {error}") from error
    if pending:
        yield pending.removesuffix("\r")


async def _error_text(response: httpx2.Response) -> str:
    """Return the start of an error response's body, or a note that it couldn't be read."""
    try:
        body = await response.aread()
    except httpx2.HTTPError as error:
        return f"(error body not read: {error!r})"
    return body.decode("utf-8", errors="replace")[:_ERROR_TEXT_LIMIT].strip()
