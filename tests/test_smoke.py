"""Smoke tests: the package can be imported."""

import importlib


def test_import_browl() -> None:
    """The browl package imports without error."""
    importlib.import_module("browl")
