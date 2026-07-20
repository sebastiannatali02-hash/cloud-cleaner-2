"""Tests for the blast-radius guardrail (pure, no I/O)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cloudcleaner.guardrail import (
    GuardrailLimits,
    GuardrailViolation,
    check_guardrail,
)
from cloudcleaner.models import Candidate, ScanResult, StorageObject

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def obj(key: str, size: int = 1024, storage_class: str = "STANDARD") -> StorageObject:
    return StorageObject(
        key=key,
        size_bytes=size,
        last_modified=NOW - timedelta(days=1),
        storage_class=storage_class,
    )


def make_result(
    scanned_count: int,
    candidate_count: int,
    *,
    candidate_size: int = 1024,
    bucket: str = "test-bucket",
) -> ScanResult:
    """Build a ScanResult with ``candidate_count`` candidates of ``candidate_size``.

    scanned_bytes/scanned_by_class are not exercised by the guardrail, so we
    leave scanned_bytes at a representative value.
    """
    candidates = [
        Candidate(obj=obj(f"cand-{i}", size=candidate_size), rule_name="r")
        for i in range(candidate_count)
    ]
    return ScanResult(
        bucket=bucket,
        scanned_count=scanned_count,
        scanned_bytes=scanned_count * candidate_size,
        candidates=candidates,
    )


# --------------------------------------------------------------------------
# None limits never trip
# --------------------------------------------------------------------------


def test_unlimited_never_trips_even_when_everything_matches():
    result = make_result(scanned_count=4000, candidate_count=4000)
    # No exception expected.
    check_guardrail(result, GuardrailLimits())


def test_is_unlimited_flag():
    assert GuardrailLimits().is_unlimited() is True
    assert GuardrailLimits(max_objects=1).is_unlimited() is False


def test_empty_scan_does_not_trip_fraction():
    result = make_result(scanned_count=0, candidate_count=0)
    check_guardrail(result, GuardrailLimits(max_fraction=0.5))


# --------------------------------------------------------------------------
# max_fraction boundary
# --------------------------------------------------------------------------


def test_fraction_exactly_at_limit_is_allowed():
    # 2000/4000 = 0.50 == limit -> allowed (strictly-greater semantics).
    result = make_result(scanned_count=4000, candidate_count=2000)
    check_guardrail(result, GuardrailLimits(max_fraction=0.5))


def test_fraction_just_over_limit_trips():
    # 2001/4000 = 0.500... > 0.50 -> trips.
    result = make_result(scanned_count=4000, candidate_count=2001)
    with pytest.raises(GuardrailViolation) as exc:
        check_guardrail(result, GuardrailLimits(max_fraction=0.5))
    assert exc.value.limit == "max_fraction"
    assert "max_fraction" in str(exc.value)


def test_fraction_message_reports_counts_and_percentage():
    result = make_result(scanned_count=4000, candidate_count=3800)
    with pytest.raises(GuardrailViolation) as exc:
        check_guardrail(result, GuardrailLimits(max_fraction=0.5))
    msg = str(exc.value)
    assert "3800/4000" in msg
    assert "95.0%" in msg
    assert "--force" in msg


# --------------------------------------------------------------------------
# max_objects boundary
# --------------------------------------------------------------------------


def test_objects_exactly_at_limit_is_allowed():
    result = make_result(scanned_count=1000, candidate_count=100)
    check_guardrail(result, GuardrailLimits(max_objects=100))


def test_objects_just_over_limit_trips():
    result = make_result(scanned_count=1000, candidate_count=101)
    with pytest.raises(GuardrailViolation) as exc:
        check_guardrail(result, GuardrailLimits(max_objects=100))
    assert exc.value.limit == "max_objects"
    assert "max_objects" in str(exc.value)
    assert "101" in str(exc.value)


# --------------------------------------------------------------------------
# max_bytes boundary
# --------------------------------------------------------------------------


def test_bytes_exactly_at_limit_is_allowed():
    # 10 candidates * 100 bytes = 1000 == limit -> allowed.
    result = make_result(scanned_count=50, candidate_count=10, candidate_size=100)
    check_guardrail(result, GuardrailLimits(max_bytes=1000))


def test_bytes_just_over_limit_trips():
    # 11 * 100 = 1100 > 1000 -> trips.
    result = make_result(scanned_count=50, candidate_count=11, candidate_size=100)
    with pytest.raises(GuardrailViolation) as exc:
        check_guardrail(result, GuardrailLimits(max_bytes=1000))
    assert exc.value.limit == "max_bytes"
    assert "max_bytes" in str(exc.value)


# --------------------------------------------------------------------------
# ordering / combined limits
# --------------------------------------------------------------------------


def test_fraction_checked_before_objects_and_bytes():
    # All three would trip; fraction is reported first.
    result = make_result(scanned_count=100, candidate_count=100, candidate_size=100)
    limits = GuardrailLimits(max_fraction=0.5, max_objects=10, max_bytes=100)
    with pytest.raises(GuardrailViolation) as exc:
        check_guardrail(result, limits)
    assert exc.value.limit == "max_fraction"


def test_objects_checked_before_bytes_when_fraction_ok():
    # fraction ok (10/100 = 0.1), but objects and bytes both trip.
    result = make_result(scanned_count=100, candidate_count=10, candidate_size=100)
    limits = GuardrailLimits(max_fraction=0.5, max_objects=5, max_bytes=100)
    with pytest.raises(GuardrailViolation) as exc:
        check_guardrail(result, limits)
    assert exc.value.limit == "max_objects"


def test_within_all_limits_passes():
    result = make_result(scanned_count=1000, candidate_count=10, candidate_size=100)
    limits = GuardrailLimits(max_fraction=0.5, max_objects=100, max_bytes=100_000)
    check_guardrail(result, limits)


# --------------------------------------------------------------------------
# from_dict helper
# --------------------------------------------------------------------------


def test_from_dict_empty_and_none_are_unlimited():
    assert GuardrailLimits.from_dict(None).is_unlimited()
    assert GuardrailLimits.from_dict({}).is_unlimited()


def test_from_dict_partial_keys():
    limits = GuardrailLimits.from_dict({"max_objects": 50})
    assert limits.max_objects == 50
    assert limits.max_fraction is None
    assert limits.max_bytes is None


def test_from_dict_full_and_types():
    limits = GuardrailLimits.from_dict(
        {"max_fraction": "0.25", "max_objects": "10", "max_bytes": "2048"}
    )
    assert limits.max_fraction == 0.25
    assert isinstance(limits.max_fraction, float)
    assert limits.max_objects == 10
    assert limits.max_bytes == 2048


def test_from_dict_ignores_unknown_keys():
    limits = GuardrailLimits.from_dict({"max_objects": 5, "bogus": "x"})
    assert limits.max_objects == 5


def test_from_dict_explicit_none_stays_unlimited():
    limits = GuardrailLimits.from_dict(
        {"max_fraction": None, "max_objects": None, "max_bytes": None}
    )
    assert limits.is_unlimited()
