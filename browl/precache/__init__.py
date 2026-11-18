"""Pre-caching: build the derived database from bulk queries to the loaded store (DESIGN.md §2.5).

- `core`: what the hooks share: their context, the database writer, `batched`, the `Hook` type, `sparql_file`.
- `header`: the ontology header, for the `meta` row.
- `entities`, and a module for each later hook: one pre-caching hook each.
- `keys`: the build keys that decide whether a derived file is current.
- `stage`: the stage itself, which runs the hooks in `HOOKS`, and the rebuild after a restart.

A hook's module imports only what the hook uses, as its build key covers its import closure.
"""

from browl.precache.keys import build_keys
from browl.precache.stage import HOOKS, pre_caching_stage, rebuild, remove_previous

__all__ = ["HOOKS", "build_keys", "pre_caching_stage", "rebuild", "remove_previous"]
