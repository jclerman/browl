"""Tests that the manual (docs/manual/) keeps up with the settings."""

import re
from pathlib import Path

from browl import precache
from browl.precache import header
from browl.settings import Settings

OPERATOR = Path(__file__).resolve().parent.parent / "docs" / "manual" / "operator.md"

REBUILD = re.compile(r"A change to (.*?) makes the app rebuild its derived data", re.DOTALL)
"""The operator manual's sentence naming the settings whose change makes the app rebuild its derived data."""


def test_every_setting_is_documented() -> None:
    """Every setting is named in the operator manual, as its environment variable."""
    text = OPERATOR.read_text()
    assert [name.upper() for name in Settings.model_fields if f"`{name.upper()}`" not in text] == []


def test_rebuild_settings_are_documented() -> None:
    """The operator manual names exactly the settings the derived database's build keys cover."""
    match = REBUILD.search(OPERATOR.read_text())
    assert match, "the operator manual has no sentence on the settings that trigger a rebuild"
    named = set(re.findall(r"`([A-Z_]+)`", match.group(1)))
    declared = header.SETTINGS.union(*(hook.settings for hook in precache.HOOKS))
    assert named == {name.upper() for name in declared}
