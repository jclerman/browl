"""Helpers shared by the tests."""

import http.server
import os
import threading

import httpx2
from fastapi import FastAPI

from browl.app import create_app
from browl.imports import OWL_IMPORTS
from browl.settings import Settings


def make_settings(**overrides: object) -> Settings:
    """Return settings for tests: authentication off, DATA_DIR from the environment, and any overrides."""
    defaults = {"BASE_URL": "http://example.org/", "AUTH_DISABLED": True, "DATA_DIR": os.environ["DATA_DIR"]}
    return Settings.model_validate(defaults | overrides)


def make_app(
    fuseki_transport: httpx2.AsyncBaseTransport | None = None, real_stages: bool = False, **overrides: object
) -> FastAPI:
    """Return an app that doesn't start the JVM, with `make_settings(**overrides)` and the Fuseki transport given.

    Its load jobs run no background stages, unless `real_stages`, when they run the app's own.
    """
    return create_app(
        make_settings(**overrides),
        fuseki_transport=fuseki_transport,
        start_jvm=False,
        stages=None if real_stages else {},
    )


class StaticServer:
    """A local HTTP server with fixed responses, recording the paths requested.

    Attributes:
        url: Its base URL.
        pages: Each path's status, headers and body.
        requested: The paths requested, in order.
    """

    def __init__(self) -> None:
        """Start serving on a free port."""
        self.pages: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requested: list[str] = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                site.requested.append(self.path)
                status, headers, body = site.pages.get(self.path, (404, {}, b"not found"))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def page(self, path: str, body: str | bytes, content_type: str = "text/turtle", status: int = 200) -> str:
        """Serve a page, returning its URL."""
        self.pages[path] = (status, {"Content-Type": content_type}, body.encode() if isinstance(body, str) else body)
        return self.url + path

    def close(self) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()


def turtle(iri: str, *imports: str, extra: str = "") -> str:
    """Return a Turtle ontology with the IRI and imports."""
    lines = [f"<{iri}> a <http://www.w3.org/2002/07/owl#Ontology> ."]
    lines += [f"<{iri}> <{OWL_IMPORTS}> <{imported}> ." for imported in imports]
    return "\n".join([*lines, extra, ""])
