# SQLite databases

browl keeps two SQLite databases, both in `DATA_DIR` on the `data` volume ([`DESIGN.md`](DESIGN.md) §2.1, Data and
persistence). Their tables and columns are documented in pages generated from the code:

| Database | Generated docs | Schema in code | Lifecycle |
| --- | --- | --- | --- |
| State | [`schema/state/`](schema/state/README.md) | `browl.jobs.MIGRATIONS` | Persistent and authoritative: the load job's state (§2.4), and later the admins (§2.11). One file, `DATA_DIR/state.sqlite3`. It records its schema version (`PRAGMA user_version`) and migrates forward from it when browl starts; a version newer than browl knows stops it. |
| Derived | [`schema/derived/`](schema/derived/README.md) | `browl.derived.SCHEMA` and `INDEXES` | Rebuilt from Fuseki's store, never authoritative: whole-ontology data computed once per load (§2.5). One file per generation, `DATA_DIR/derived/{generation}.sqlite3`, written whole by the pre-caching stage, renamed into place, then only read; the previous generation's is deleted once a new one is ready. No migrations: a file whose build keys (`build_key`) differ from the ones the running code computes isn't current, and is rebuilt. |

## Changing a schema

- **State:** append a migration to `MIGRATIONS`; never edit one that has shipped.
- **Derived:** edit `SCHEMA` or `INDEXES`, or the pre-caching code, with no other step: the build keys
  (`browl.precache.keys`, [`DESIGN.md`](DESIGN.md) §2.5) cover the schema's SQL and the code that fills it, so a file
  built by other code isn't taken as current. For that, a hook's module imports only what the hook uses, as its key
  covers its import closure; the hook declares the settings it reads (`@hook("base_url", …)`); and it loads data files,
  such as queries, only through `browl.precache.core.sparql_file`, with a string literal. `tests/test_precache_keys.py`
  traces each hook to check the last two. Update `COLUMNS` in `tests/test_derived.py` with the schema.
- Then describe any new table or column in [`.tbls/state.yml`](../.tbls/state.yml) or
  [`.tbls/derived.yml`](../.tbls/derived.yml), and run `make schema-docs` to regenerate `docs/schema/`.

[tbls](https://github.com/k1LoW/tbls) generates the docs from empty databases that `scripts/schema_dbs.py` builds from
the code. `make check-schema-docs`, part of `make ci`, fails if the docs differ from what tbls would generate, or if a
table or column has no description. `make tbls` downloads the pinned tbls into `.cache/`.
