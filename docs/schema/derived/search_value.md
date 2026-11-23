# search_value

## Description

The search index (browl.search.SqliteSearch): each name value that isn't empty once normalized, keyed in the order search ranks values. The rows that share the key's first four columns (type, property_rank, language_rank, entailed) form a lane, ordered within by length, normalized value, then value. A query seeks its matches in one lane and of one length at a time, shortest first, so it never sorts them. The key holds at most browl.search.KEY_CHARS characters of each value, so a query seeks at most that many lengths per lane however long the values are. Ranks are positions in NAME_PROPERTIES and LANGUAGE_RANKING as built, which the search hook's build key covers. Of an entity's values that differ only in language tags that rank alike, one row is kept.

<details>
<summary><strong>Table Definition</strong></summary>

```sql
CREATE TABLE search_value (
    type TEXT NOT NULL,
    property_rank INTEGER NOT NULL,
    language_rank INTEGER NOT NULL,
    entailed INTEGER NOT NULL,
    length INTEGER NOT NULL,
    normalized TEXT NOT NULL,
    value TEXT NOT NULL,
    entity INTEGER NOT NULL,
    normalized_rest TEXT,
    value_rest TEXT,
    language TEXT,
    PRIMARY KEY (type, property_rank, language_rank, entailed, length, normalized, value, entity)
) WITHOUT ROWID
```

</details>

## Columns

| Name            | Type    | Default | Nullable | Children | Parents             | Comment                                                                                                                                                               |
| --------------- | ------- | ------- | -------- | -------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| type            | TEXT    |         | false    |          |                     | The entity's type (entity.type), so a type filter seeks only its own lanes.                                                                                           |
| property_rank   | INTEGER |         | false    |          |                     | The name property's position in NAME_PROPERTIES, from 0.                                                                                                              |
| language_rank   | INTEGER |         | false    |          |                     | The language's rank among the values' languages by LANGUAGE_RANKING (browl.names.language_rank), from 0; languages that rank alike, such as unlisted ones, share one. |
| entailed        | INTEGER |         | false    |          |                     | 0 if the entity has the value under the name property itself, 1 if through a sub-property.                                                                            |
| length          | INTEGER |         | false    |          |                     | The normalized value's length in characters, counted in Python, up to KEY_CHARS.                                                                                      |
| normalized      | TEXT    |         | false    |          |                     | The value normalized for search (browl.names.normalize), its first KEY_CHARS characters.                                                                              |
| value           | TEXT    |         | false    |          |                     | The value's lexical form, its first KEY_CHARS characters.                                                                                                             |
| entity          | INTEGER |         | false    |          | [entity](entity.md) | The entity (entity.id).                                                                                                                                               |
| normalized_rest | TEXT    |         | true     |          |                     | The normalized value's characters after the first KEY_CHARS; NULL if there are none.                                                                                  |
| value_rest      | TEXT    |         | true     |          |                     | The value's characters after the first KEY_CHARS; NULL if there are none.                                                                                             |
| language        | TEXT    |         | true     |          |                     | The value's language tag, lowercased; NULL if it has none.                                                                                                            |

## Constraints

| Name                            | Type        | Definition                                                                                    |
| ------------------------------- | ----------- | --------------------------------------------------------------------------------------------- |
| type                            | PRIMARY KEY | PRIMARY KEY (type)                                                                            |
| property_rank                   | PRIMARY KEY | PRIMARY KEY (property_rank)                                                                   |
| language_rank                   | PRIMARY KEY | PRIMARY KEY (language_rank)                                                                   |
| entailed                        | PRIMARY KEY | PRIMARY KEY (entailed)                                                                        |
| length                          | PRIMARY KEY | PRIMARY KEY (length)                                                                          |
| normalized                      | PRIMARY KEY | PRIMARY KEY (normalized)                                                                      |
| value                           | PRIMARY KEY | PRIMARY KEY (value)                                                                           |
| entity                          | PRIMARY KEY | PRIMARY KEY (entity)                                                                          |
| sqlite_autoindex_search_value_1 | PRIMARY KEY | PRIMARY KEY (type, property_rank, language_rank, entailed, length, normalized, value, entity) |

## Indexes

| Name                            | Definition                                                                                    |
| ------------------------------- | --------------------------------------------------------------------------------------------- |
| sqlite_autoindex_search_value_1 | PRIMARY KEY (type, property_rank, language_rank, entailed, length, normalized, value, entity) |

## Relations

```mermaid
erDiagram

"search_value" }o--|| "entity" : "search_value.entity references entity.id"

"search_value" {
  TEXT type PK
  INTEGER property_rank PK
  INTEGER language_rank PK
  INTEGER entailed PK
  INTEGER length PK
  TEXT normalized PK
  TEXT value PK
  INTEGER entity PK
  TEXT normalized_rest
  TEXT value_rest
  TEXT language
}
"entity" {
  INTEGER id
  TEXT iri
  TEXT type
  TEXT path
  TEXT fragment
  TEXT display_name
  TEXT curie
}
```

---

> Generated by [tbls](https://github.com/k1LoW/tbls)
