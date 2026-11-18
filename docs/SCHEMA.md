# SQLite databases

browl keeps two SQLite databases, both in `DATA_DIR` on the `data` volume ([`DESIGN.md`](DESIGN.md) §2.1, Data and
persistence). Their tables and columns are documented in pages generated from the code:

| Database | Generated docs | Schema in code | Lifecycle |
| --- | --- | --- | --- |
| State | [`schema/state/`](schema/state/README.md) | `browl.jobs.MIGRATIONS` | Persistent and authoritative: the load job's state (§2.4), and later the admins (§2.11). One file, `DATA_DIR/state.sqlite3`. It records its schema version (`PRAGMA user_version`) and migrates forward from it when browl starts; a version newer than browl knows stops it. |
| Derived | [`schema/derived/`](schema/derived/README.md) | `browl.derived.SCHEMA` and `INDEXES` | Rebuilt from Fuseki's store, never authoritative: whole-ontology data computed once per load (§2.5). One file per generation, `DATA_DIR/derived/{generation}.sqlite3`, written whole by the pre-caching stage, renamed into place, then only read; the previous generation's is deleted once a new one is ready. No migrations: a file of another `SCHEMA_VERSION` isn't current, and is rebuilt. |

## Changing a schema

- **State:** append a migration to `MIGRATIONS`; never edit one that has shipped.
- **Derived:** edit `SCHEMA` or `INDEXES`, and raise `SCHEMA_VERSION`. `tests/test_derived.py` fails until the new
  version's schema is recorded there. Raise it also when what the pre-caching hooks store changes with the same SQL, so
  a file built by the old code isn't taken as current.
- Then describe any new table or column in [`.tbls/state.yml`](../.tbls/state.yml) or
  [`.tbls/derived.yml`](../.tbls/derived.yml), and run `make schema-docs` to regenerate `docs/schema/`.

[tbls](https://github.com/k1LoW/tbls) generates the docs from empty databases that `scripts/schema_dbs.py` builds from
the code. `make check-schema-docs`, part of `make ci`, fails if the docs differ from what tbls would generate, or if a
table or column has no description. `make tbls` downloads the pinned tbls into `.cache/`.
