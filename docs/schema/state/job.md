# job

## Description

The latest load job (browl.jobs.JobState), at most one row: each job replaces the previous one's. Times are ISO 8601 with a time zone.

<details>
<summary><strong>Table Definition</strong></summary>

```sql
CREATE TABLE job (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        generation INTEGER NOT NULL,
        stage TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        failed_stage TEXT,
        reason TEXT,
        ready_generation INTEGER
    , reasoner TEXT NOT NULL DEFAULT 'none', upload_name TEXT, upload_syntax TEXT, upload_bytes INTEGER, imports TEXT NOT NULL DEFAULT '[]', triples INTEGER, prefixes TEXT NOT NULL DEFAULT '{}', stage_started_at TEXT, stage_seconds TEXT NOT NULL DEFAULT '{}', detail TEXT, fraction REAL)
```

</details>

## Columns

| Name             | Type    | Default | Nullable | Children | Parents | Comment                                                                                                                                                                    |
| ---------------- | ------- | ------- | -------- | -------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| id               | INTEGER |         | true     |          |         | Always 1, so the table has at most one row.                                                                                                                                |
| generation       | INTEGER |         | false    |          |         | The job's generation number, one more than the previous job's.                                                                                                             |
| stage            | TEXT    |         | false    |          |         | The stage the job is in, or reached (ready), or failed in (browl.jobs.Stage).                                                                                              |
| started_at       | TEXT    |         | false    |          |         | When the job started.                                                                                                                                                      |
| finished_at      | TEXT    |         | true     |          |         | When it reached ready or failed; NULL while it runs.                                                                                                                       |
| failed_stage     | TEXT    |         | true     |          |         | The stage it failed in; NULL unless it failed.                                                                                                                             |
| reason           | TEXT    |         | true     |          |         | Why it failed, cut to MAX_REASON_CHARS; NULL unless it failed.                                                                                                             |
| ready_generation | INTEGER |         | true     |          |         | The generation of the latest job that reached ready, this one or an earlier one; NULL if none has.                                                                         |
| reasoner         | TEXT    | 'none'  | false    |          |         | The reasoner the admin chose (browl.jobtypes.Reasoner); 'none' skips the reasoning stage.                                                                                  |
| upload_name      | TEXT    |         | true     |          |         | The uploaded file's name, as the admin's browser gave it; NULL if none.                                                                                                    |
| upload_syntax    | TEXT    |         | true     |          |         | The file's syntax, e.g. 'RDF/XML'.                                                                                                                                         |
| upload_bytes     | INTEGER |         | true     |          |         | The file's size; while uploading, or after a failed upload, how much arrived.                                                                                              |
| imports          | TEXT    | '[]'    | false    |          |         | The ontology's imports, direct and indirect, as a JSON list of browl.jobtypes.ImportResult; '[]' until the imports stage lists them.                                       |
| triples          | INTEGER |         | true     |          |         | How many triples the store's default graph holds once loaded, the ontology without its imports.                                                                            |
| prefixes         | TEXT    | '{}'    | false    |          |         | The prefixes the ontology file declares, as a JSON object of name to namespace IRI, for CURIEs; '{}' until the loading stage reads them.                                   |
| stage_started_at | TEXT    |         | true     |          |         | When the current stage started (P-31); NULL in a row stored before it was recorded.                                                                                        |
| stage_seconds    | TEXT    | '{}'    | false    |          |         | How long each stage that ended took, in seconds, as a JSON object of stage (browl.jobs.Stage) to seconds, the stage the job failed in included; '{}' before any has ended. |
| detail           | TEXT    |         | true     |          |         | What the running stage is doing, e.g. 'Checking for unsatisfiable classes', recorded at most every PROGRESS_S (P-31); NULL if it hasn't said, and once it ends.            |
| fraction         | REAL    |         | true     |          |         | How far the running stage has got, from 0 to 1; NULL if it can't tell, and once it ends.                                                                                   |

## Constraints

| Name | Type        | Definition       |
| ---- | ----------- | ---------------- |
| id   | PRIMARY KEY | PRIMARY KEY (id) |
| -    | CHECK       | CHECK (id = 1)   |

## Relations

```mermaid
erDiagram


"job" {
  INTEGER id
  INTEGER generation
  TEXT stage
  TEXT started_at
  TEXT finished_at
  TEXT failed_stage
  TEXT reason
  INTEGER ready_generation
  TEXT reasoner
  TEXT upload_name
  TEXT upload_syntax
  INTEGER upload_bytes
  TEXT imports
  INTEGER triples
  TEXT prefixes
  TEXT stage_started_at
  TEXT stage_seconds
  TEXT detail
  REAL fraction
}
```

---

> Generated by [tbls](https://github.com/k1LoW/tbls)
