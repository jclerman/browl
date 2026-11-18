"""What load jobs share with the code of their stages: the reasoner chosen, the imports' results, and `StageError`.

They're apart from the job runner (`browl.jobs`) so that pre-caching's build keys, which cover the code pre-caching
imports, don't cover the runner (DESIGN.md §2.5, Build keys).
"""

from dataclasses import dataclass
from enum import StrEnum


class Reasoner(StrEnum):
    """The reasoner an admin chooses for a job (A-3); `NONE` skips the reasoning stage."""

    NONE = "none"
    ELK = "elk"
    HERMIT = "hermit"


@dataclass(frozen=True)
class ImportResult:
    """An ontology the served ontology imports, directly or not, and whether it was loaded (A-4).

    Attributes:
        iri: Its IRI, as declared in the `owl:imports` that imports it.
        imported_by: The IRI of the import that imports it; None if the upload does.
        reason: Why it was dropped, e.g. "only http and https imports are fetched"; None if it was fetched.
        triples: How many triples its named graph holds, once loaded; None before then, or if it was dropped.
    """

    iri: str
    imported_by: str | None = None
    reason: str | None = None
    triples: int | None = None

    @property
    def dropped(self) -> bool:
        """Whether it couldn't be fetched, or wasn't allowed, so it isn't part of the served ontology."""
        return self.reason is not None


class StageError(Exception):
    """A stage failed for a reason worth showing to admins as it is (A-6), e.g. "time limit"."""
