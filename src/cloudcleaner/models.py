"""Core data structures shared across the tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class StorageObject:
    """A single object living in a cloud storage bucket."""

    key: str
    size_bytes: int
    last_modified: datetime
    storage_class: str = "STANDARD"


@dataclass(frozen=True)
class Candidate:
    """An object selected for cleanup, with the rule that matched it."""

    obj: StorageObject
    rule_name: str


@dataclass
class ScanResult:
    """Outcome of applying the rule set to a bucket."""

    bucket: str
    scanned_count: int = 0
    scanned_bytes: int = 0
    scanned_by_class: dict[str, int] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def candidate_bytes(self) -> int:
        return sum(c.obj.size_bytes for c in self.candidates)

    @property
    def candidates_by_class(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for c in self.candidates:
            totals[c.obj.storage_class] = totals.get(c.obj.storage_class, 0) + c.obj.size_bytes
        return totals
