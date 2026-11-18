"""Tests for the OWL Functional Syntax lister and converter (`browl.ofn`): exact imports, no loads, limits."""

import os
from pathlib import Path

import pytest

from browl.jobs import StageError
from browl.ofn import convert_ofn
from browl.settings import Settings
from tests.conftest import find_robot_jar
from tests.helpers import StaticServer, make_settings, turtle

pytestmark = [pytest.mark.anyio, pytest.mark.jvm]

EX = "http://example.org/"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """Return test settings with the tests' robot.jar."""
    return make_settings(ROBOT_JAR=str(find_robot_jar()), REASONER_MAX_HEAP="512m")


@pytest.mark.usefixtures("jvm")
async def test_lists_what_the_owlapi_reads(site: StaticServer, settings: Settings, tmp_path: Path) -> None:
    """Check that the imports a regex scan missed (PR #70's review, M1) are listed, and nothing is fetched or read.

    Each import is one the OWLAPI reads in a way a scan didn't: a prefix redefined in a comment, a non-ASCII prefix
    name, a comment inside `Import(`, the predeclared `rdfs:` prefix, an IRI kept as written (`a/../b`), and an
    `Import` after an annotation. The local file imports a page of the server, which would be requested if it were
    read.
    """
    url, requested = site.url, site.requested
    secret = tmp_path / "secret.ttl"
    secret.write_text(turtle(f"{EX}secret", f"{url}/from-secret"))
    source = tmp_path / "o.ofn"
    source.write_text(
        f"Prefix(a:=<{url}/>)\n# Prefix(a:=<{EX}decoy/>)\nPrefix(é:=<{url}/uni/>)\n"
        f"Ontology(<{EX}o>\nImport(a:prefixdup)\nImport(é:unicode)\nImport(# c\n<{url}/comment>)\n"
        f"Import(rdfs:builtin)\nImport(<{url}/a/../dotdot>)\nImport(<{secret.as_uri()}>)\n"
        f'Annotation(rdfs:comment "x")\nImport(<{url}/late>)\nDeclaration(Class(<{EX}A>))\n)\n',
        encoding="utf-8",
    )
    output = tmp_path / "o.ttl"
    found = await convert_ofn(source, output, settings)
    assert sorted(found) == sorted(
        [
            f"{url}/prefixdup",
            f"{url}/uni/unicode",
            f"{url}/comment",
            "http://www.w3.org/2000/01/rdf-schema#builtin",
            f"{url}/a/../dotdot",
            secret.as_uri(),
            f"{url}/late",
        ]
    )
    assert requested == []
    text = output.read_text()
    assert f"<{EX}A>" in text or ":A" in text
    assert f"{url}/a/../dotdot" in text
    assert sorted(path.name for path in tmp_path.iterdir()) == ["o.ofn", "o.ttl", "secret.ttl"]


async def test_relative_import_left_out(settings: Settings, tmp_path: Path) -> None:
    """Check that a relative import is listed but left out of the Turtle, whose base would resolve it differently."""
    source = tmp_path / "o.ofn"
    source.write_text(f"Ontology(<{EX}o>\nImport(<rel.owl>)\nImport(<{EX}b>)\n)\n")
    output = tmp_path / "o.ttl"
    assert sorted(await convert_ofn(source, output, settings)) == [f"{EX}b", "rel.owl"]
    text = output.read_text()
    assert "rel.owl" not in text
    assert f"{EX}b" in text


async def test_parse_error(settings: Settings, tmp_path: Path) -> None:
    """Check that a file the OWLAPI can't read as OFN fails with a parse error, leaving no output."""
    source = tmp_path / "o.ofn"
    source.write_text(f"Ontology(<{EX}o>\nSubClassOf(\n")
    output = tmp_path / "o.ttl"
    with pytest.raises(StageError, match=r"^parse error: the OWLAPI couldn't read it as OWL Functional Syntax:\n"):
        await convert_ofn(source, output, settings)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["o.ofn"]


async def test_time_limit(tmp_path: Path, data_dir: Path) -> None:
    """Check that the conversion is held to the reasoner time limit: its source is a pipe nothing is written to."""
    settings = make_settings(ROBOT_JAR=str(find_robot_jar()), REASONER_TIMEOUT_S="1")
    source = tmp_path / "o.ofn"
    os.mkfifo(source)
    with pytest.raises(
        StageError, match=r"^time limit: the OFN conversion took longer than REASONER_TIMEOUT_S \(1 s\)$"
    ):
        await convert_ofn(source, tmp_path / "o.ttl", settings)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["o.ofn"]
