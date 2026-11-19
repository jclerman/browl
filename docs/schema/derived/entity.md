# entity

## Description

The ontology itself and every declared class and property, with the URL its IRI implies (DESIGN.md 2.3, browl.urls), filled by the entities pre-caching hook. An IRI declared with several types (punning) has a row for each. Several IRIs can share a URL, e.g. the http and https forms of one (U-15).

<details>
<summary><strong>Table Definition</strong></summary>

```sql
CREATE TABLE entity (
    id INTEGER PRIMARY KEY,
    iri TEXT NOT NULL,
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    fragment TEXT,
    display_name TEXT,
    curie TEXT
)
```

</details>

## Columns

| Name         | Type    | Default | Nullable | Children                                                                                      | Parents | Comment                                                                                                                                                                                              |
| ------------ | ------- | ------- | -------- | --------------------------------------------------------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| id           | INTEGER |         | true     | [name_value](name_value.md) [edge](edge.md) [equiv_group](equiv_group.md) [counts](counts.md) |         | The entity's number, dense, in the order the entity list returned it; the other tables refer to it.                                                                                                  |
| iri          | TEXT    |         | false    |                                                                                               |         | The entity's IRI, as the ontology has it.                                                                                                                                                            |
| type         | TEXT    |         | false    |                                                                                               |         | "Its type (browl.derived.EntityType): ontology, class, object_property, data_property or annotation_property."                                                                                       |
| path         | TEXT    |         | false    |                                                                                               |         | Its URL's path from the site's root (BASE_URL), in URI form (non-ASCII percent-encoded as UTF-8). For a fallback URL, the path and query string, /{RESERVED_PREFIX}/iri?u={the percent-encoded IRI}. |
| fragment     | TEXT    |         | true     |                                                                                               |         | The fragment of a hash IRI's URL, in URI form; NULL for any other, and for every fallback URL.                                                                                                       |
| display_name | TEXT    |         | true     |                                                                                               |         | Its display name (DESIGN.md 2.7): its best name value, else its CURIE, else its IRI; for the ontology, its title (meta.title) if it has one.                                                         |
| curie        | TEXT    |         | true     |                                                                                               |         | Its CURIE, from the longest namespace in the prefix table that covers it; NULL if none does.                                                                                                         |

## Constraints

| Name | Type        | Definition       |
| ---- | ----------- | ---------------- |
| id   | PRIMARY KEY | PRIMARY KEY (id) |

## Indexes

| Name            | Definition                                                |
| --------------- | --------------------------------------------------------- |
| entity_path     | CREATE INDEX entity_path ON entity (path, fragment)       |
| entity_iri_type | CREATE UNIQUE INDEX entity_iri_type ON entity (iri, type) |

## Relations

```mermaid
erDiagram

"name_value" }o--|| "entity" : "name_value.entity references entity.id"
"edge" }o--|| "entity" : "edge.parent references entity.id"
"edge" }o--|| "entity" : "edge.child references entity.id"
"equiv_group" |o--|| "entity" : "equiv_group.entity references entity.id"
"equiv_group" }o--|| "entity" : "equiv_group.representative references entity.id"
"counts" }o--|| "entity" : "counts.node references entity.id"

"entity" {
  INTEGER id
  TEXT iri
  TEXT type
  TEXT path
  TEXT fragment
  TEXT display_name
  TEXT curie
}
"name_value" {
  INTEGER entity
  TEXT property
  TEXT value
  TEXT normalized
  TEXT language
  INTEGER direct
}
"edge" {
  TEXT hierarchy PK
  INTEGER parent PK
  INTEGER child PK
}
"equiv_group" {
  INTEGER entity
  INTEGER representative
}
"counts" {
  TEXT hierarchy PK
  INTEGER node PK
  INTEGER children
  INTEGER descendants
}
```

---

> Generated by [tbls](https://github.com/k1LoW/tbls)
