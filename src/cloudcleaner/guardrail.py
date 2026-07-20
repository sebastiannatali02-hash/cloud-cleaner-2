"""Blast-radius guardrail for the deletion tool.

A misconfigured rule can match nearly an entire bucket. Before anything is
quarantined or deleted, :func:`check_guardrail` compares the candidate set
against configured limits and *refuses* (raises :class:`GuardrailViolation`)
when the candidate set is too large a fraction, count, or size of what was
scanned. The caller is expected to allow an explicit override (e.g. a
``--force`` flag) by simply not calling this function, or by catching the
exception.

This module is intentionally PURE: no I/O, no AWS, no logging. It only reads
counts and byte totals off a :class:`~cloudcleaner.models.ScanResult`, so it
is trivially unit-testable and safe to call from anywhere in the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from cloudcleaner.models import ScanResult


@dataclass(frozen=True)
class GuardrailLimits:
    """Upper bounds on how much of a bucket a single run may touch.

    Every limit is optional; ``None`` means "no limit for this dimension".

    * ``max_fraction`` — refuse if the candidate objects make up more than
      this fraction of the scanned objects (0.5 = more than 50%). Evaluated
      only when at least one object was scanned.
    * ``max_objects`` — refuse if more than this many objects are candidates.
    * ``max_bytes`` — refuse if the candidate objects total more than this
      many bytes.

    A limit "trips" only when the observed value is *strictly greater* than
    the limit, so a run that lands exactly on a limit is allowed.
    """

    max_fraction: float | None = None
    max_objects: int | None = None
    max_bytes: int | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "GuardrailLimits":
        """Build limits from a plain mapping (config YAML / CLI flags).

        Tolerant of missing keys and a ``None`` mapping (returns unlimited).
        Only the three known keys are read; anything else is ignored so the
        caller can pass a larger settings block through unchanged.
        """
        if not data:
            return cls()

        def _opt_float(key: str) -> float | None:
            value = data.get(key)
            return None if value is None else float(value)

        def _opt_int(key: str) -> int | None:
            value = data.get(key)
            return None if value is None else int(value)

        return cls(
            max_fraction=_opt_float("max_fraction"),
            max_objects=_opt_int("max_objects"),
            max_bytes=_opt_int("max_bytes"),
        )

    def is_unlimited(self) -> bool:
        """True when no limit is configured (nothing can ever trip)."""
        return (
            self.max_fraction is None
            and self.max_objects is None
            and self.max_bytes is None
        )


class GuardrailViolation(Exception):
    """Raised when a candidate set exceeds a configured guardrail limit.

    ``limit`` names the tripped dimension ("max_fraction", "max_objects" or
    "max_bytes") so callers can react programmatically; ``str(exc)`` is a
    human-readable explanation suitable for showing on the CLI.
    """

    def __init__(self, message: str, limit: str):
        super().__init__(message)
        self.limit = limit


def check_guardrail(result: ScanResult, limits: GuardrailLimits) -> None:
    """Raise :class:`GuardrailViolation` if ``result`` exceeds any limit.

    Limits are checked in a fixed order (fraction, then count, then bytes)
    and the first one that trips is reported. Returns ``None`` when the run
    is within every configured limit (or when no limits are configured).
    """
    candidate_count = result.candidate_count
    candidate_bytes = result.candidate_bytes

    if limits.max_fraction is not None and result.scanned_count > 0:
        fraction = candidate_count / result.scanned_count
        if fraction > limits.max_fraction:
            raise GuardrailViolation(
                f"refusing: {candidate_count}/{result.scanned_count} objects "
                f"({fraction:.1%}) exceeds max_fraction {limits.max_fraction:.2f}; "
                f"re-run with --force to override",
                limit="max_fraction",
            )

    if limits.max_objects is not None and candidate_count > limits.max_objects:
        raise GuardrailViolation(
            f"refusing: {candidate_count} candidate objects exceeds "
            f"max_objects {limits.max_objects}; re-run with --force to override",
            limit="max_objects",
        )

    if limits.max_bytes is not None and candidate_bytes > limits.max_bytes:
        raise GuardrailViolation(
            f"refusing: {candidate_bytes} candidate bytes exceeds "
            f"max_bytes {limits.max_bytes}; re-run with --force to override",
            limit="max_bytes",
        )
