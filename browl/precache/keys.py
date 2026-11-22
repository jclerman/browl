"""The derived database's build keys: what decides whether a file is current (DESIGN.md §2.5, Build keys).

A file stores a key per component: `schema` (`browl.derived.SCHEMA` and `INDEXES`), `meta` (the ontology header,
`browl.precache.header`) and each hook, by name. The key of the header or a hook hashes its import closure within browl
(`closure`, `code_digest`) and the `Settings` fields it declares (`settings_values`). DESIGN.md §2.5 gives the full
rules and the reasons; `docs/SCHEMA.md` says what a contributor must do. A usage table's file stores one key, of its
code and the derived database's keys (`usage_table_key`).
"""

import ast
import functools
import hashlib
import importlib.metadata
import importlib.util
import json
import sys
import typing
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import Secret, SecretBytes, SecretStr

from browl import derived
from browl.precache import header, usage_table
from browl.precache.core import SPARQL_DIR, Hook, sparql_file
from browl.settings import Settings

SCHEMA = "schema"
"""The component of the schema's key."""

META = "meta"
"""The component of the ontology header's key."""

ROOT = "browl"
"""The package whose modules a closure follows; it records the distributions of the others."""

_LOADER = sparql_file.__name__


@dataclass(frozen=True)
class Closure:
    """What a module's code depends on, for its key.

    Attributes:
        modules: The source file of each module in its import closure within the root package, by module name.
        files: The data files its modules load through `sparql_file`, by the name they pass.
        distributions: The version of each installed distribution that provides a third-party module they import, by
            name; for an imported module no distribution provides, its top-level name, with None.
    """

    modules: Mapping[str, Path]
    files: Mapping[str, Path]
    distributions: Mapping[str, str | None]


def closure(module: str, root: str = ROOT, sparql_dir: Path = SPARQL_DIR) -> Closure:
    """Return a module's import closure within the root package, the files it loads, and the distributions it imports.

    The closure is found by parsing each module's imports, wherever they are (`import browl.x`, `from browl import x`,
    `from browl.x import y`, relative imports), not by running it. A package's `__init__` is in it only if imported
    by name, e.g. `from browl.p import f` where `f` isn't a submodule.

    Args:
        module: The module's name.
        root: The package to follow imports into.
        sparql_dir: Where `sparql_file` finds files.

    Raises:
        ValueError: A module has no Python source, or calls `sparql_file` with anything but one string literal.
    """
    modules: dict[str, Path] = {}
    files: dict[str, Path] = {}
    others: set[str] = set()
    pending = [module]
    while pending:
        name = pending.pop()
        if name in modules:
            continue
        path, is_package = _source(name)
        modules[name] = path
        tree = ast.parse(path.read_bytes(), str(path))
        for imported in _imports(tree, name if is_package else name.rpartition(".")[0], root):
            top = imported.partition(".")[0]
            if top == root:
                pending.append(imported)
            elif top not in sys.stdlib_module_names:
                others.add(top)
        files |= {file: sparql_dir / file for file in _loaded_files(tree, path)}
    return Closure(modules, files, _distributions(others))


def code_digest(closure: Closure) -> str:
    """Return a hash of the code a closure holds: its modules, as parsed, files, distributions, and Python's version.

    A module's docstrings, and any other string-only statements, are left out, as are its comments and formatting.
    """
    return _hash(
        {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "modules": {name: _code_hash(path) for name, path in closure.modules.items()},
            "files": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in closure.files.items()},
            "distributions": dict(closure.distributions),
        }
    )


@functools.cache
def code_key(module: str) -> str:
    """Return the hash of a module's code (`code_digest` of its `closure`), computed once per process."""
    return code_digest(closure(module))


def settings_values(settings: Settings, names: Collection[str]) -> dict[str, object]:
    """Return the values of these `Settings` fields, as JSON.

    Raises:
        ValueError: A name isn't a `Settings` field, or is a secret one (`SecretStr`, `SecretBytes` or `Secret`): a
            secret dumps masked, so a key would miss its changes, and the file would store a hash of it.
    """
    unknown = set(names) - Settings.model_fields.keys()
    if unknown:
        raise ValueError(f"not Settings fields: {sorted(unknown)}")
    secret = {name for name in names if _is_secret(Settings.model_fields[name].annotation)}
    if secret:
        raise ValueError(f"secret Settings fields can't be in a build key: {sorted(secret)}")
    return settings.model_dump(mode="json", include=set(names))


def component_key(module: str, names: Collection[str], settings: Settings) -> str:
    """Return the key of code in a module (`code_key`) that reads these `Settings` fields (`settings_values`)."""
    return _hash({"code": code_key(module), "settings": settings_values(settings, names)})


@functools.cache
def schema_key() -> str:
    """Return the schema's key: a hash of `browl.derived.SCHEMA` and `INDEXES`."""
    return _hash({"schema": derived.SCHEMA, "indexes": derived.INDEXES})


def build_keys(settings: Settings, hooks: Sequence[Hook]) -> dict[str, str]:
    """Return the build keys of a derived database built with these settings and hooks, by component.

    Raises:
        ValueError: Two components have the same name, or a hook declares a setting that isn't a `Settings` field.
    """
    keys = {SCHEMA: schema_key(), META: component_key(header.__name__, header.SETTINGS, settings)}
    for hook in hooks:
        if hook.name in keys:
            raise ValueError(f"two build key components are named {hook.name!r}")
        keys[hook.name] = component_key(hook.module, hook.settings, settings)
    return keys


def usage_table_key(derived_keys: Mapping[str, str]) -> str:
    """Return the key of a usage table built from a derived database with these build keys (DESIGN.md §2.5).

    It hashes the table's build code (`code_key` of `browl.precache.usage_table`), which reads no settings, and the
    derived database's keys, as the table refers to its entities and owner map by number.
    """
    return _hash({"code": code_key(usage_table.__name__), "derived": dict(derived_keys)})


def _is_secret(annotation: object) -> bool:
    """Return whether a type is, or has as an argument (e.g. `SecretStr | None`), one of Pydantic's secret types."""
    origin = typing.get_origin(annotation) or annotation
    if isinstance(origin, type) and issubclass(origin, SecretStr | SecretBytes | Secret):
        return True
    return any(_is_secret(argument) for argument in typing.get_args(annotation))


def _source(module: str) -> tuple[Path, bool]:
    """Return a module's source file, and whether it's a package."""
    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
        raise ValueError(f"the module {module} has no Python source")
    return Path(spec.origin), spec.submodule_search_locations is not None


def _imports(tree: ast.Module, package: str, root: str) -> Iterator[str]:
    """Yield the names of the modules a module imports; for `from m import x`, `m.x` if that's a module in `root`.

    Imports in the body of an `if TYPE_CHECKING:` don't run, so they're left out.
    """
    for node in _runtime_nodes(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            if base.partition(".")[0] != root:
                yield base
                continue
            for alias in node.names:
                yield f"{base}.{alias.name}" if _is_module(f"{base}.{alias.name}") else base


def _runtime_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield a node and the nodes under it, except those in the body of an `if TYPE_CHECKING:`."""
    yield node
    if isinstance(node, ast.If) and _is_type_checking(node.test):
        children: list[ast.AST] = [node.test, *node.orelse]
    else:
        children = list(ast.iter_child_nodes(node))
    for child in children:
        yield from _runtime_nodes(child)


def _is_type_checking(test: ast.expr) -> bool:
    """Return whether a condition is `TYPE_CHECKING` or `typing.TYPE_CHECKING`."""
    match test:
        case ast.Name(id="TYPE_CHECKING") | ast.Attribute(value=ast.Name(id="typing"), attr="TYPE_CHECKING"):
            return True
    return False


def _is_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:  # its parent isn't a package
        return False


def _loaded_files(tree: ast.Module, path: Path) -> Iterator[str]:
    """Yield the names of the files a module loads through `sparql_file`.

    Raises:
        ValueError: A call passes anything but one string literal.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (function.id if isinstance(function, ast.Name) else getattr(function, "attr", None)) != _LOADER:
            continue
        match node.args, node.keywords:
            case [ast.Constant(value=str(name))], []:
                yield name
            case _:
                raise ValueError(
                    f"{path}:{node.lineno}: pass {_LOADER} one string literal, so the build keys can cover the file"
                )


def _distributions(modules: Collection[str]) -> dict[str, str | None]:
    """Return the version of each distribution that provides one of these top-level modules, or None if none does."""
    providers = _packages_distributions()
    versions: dict[str, str | None] = {}
    for module in modules:
        names = providers.get(module, [])
        if not names:
            versions[module] = None
        for name in names:
            versions[name] = importlib.metadata.version(name)
    return versions


@functools.cache
def _packages_distributions() -> Mapping[str, list[str]]:
    return importlib.metadata.packages_distributions()


class _StripStrings(ast.NodeTransformer):
    """Removes statements that are only a string, such as docstrings, which don't change what code does."""

    def visit_Expr(self, node: ast.Expr) -> ast.Expr | None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return None
        return node


def _code_hash(path: Path) -> str:
    """Return a hash of a module's syntax tree, without docstrings (`_StripStrings`); comments aren't in it."""
    tree = _StripStrings().visit(ast.parse(path.read_bytes(), str(path)))
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()


def _hash(value: object) -> str:
    """Return the SHA-256 of a value's canonical JSON."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
