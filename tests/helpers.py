"""Helpers shared by the tests."""

import os

import httpx2
from fastapi import FastAPI

from browl.app import create_app
from browl.settings import Settings


def make_settings(**overrides: object) -> Settings:
    """Return settings for tests: authentication off, DATA_DIR from the environment, and any overrides."""
    defaults = {"BASE_URL": "http://example.org/", "AUTH_DISABLED": True, "DATA_DIR": os.environ["DATA_DIR"]}
    return Settings.model_validate(defaults | overrides)


def make_app(fuseki_transport: httpx2.AsyncBaseTransport | None = None, **overrides: object) -> FastAPI:
    """Return an app that doesn't start the JVM, with `make_settings(**overrides)` and the Fuseki transport given."""
    return create_app(make_settings(**overrides), fuseki_transport=fuseki_transport, start_jvm=False)
