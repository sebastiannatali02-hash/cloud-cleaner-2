"""Storage-class transition recommendations.

Not every object should be deleted. Cold-but-kept data (compliance
archives, old-but-referenced assets) can instead be *transitioned* from
STANDARD to a colder class (GLACIER, DEEP_ARCHIVE, ...) at a fraction of
the per-GB price. This estimates the monthly saving from moving all the
STANDARD volume in a scan to a target class, without deleting anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from cloudcleaner.models import ScanResult
from cloudcleaner.pricing import PricingModel


@dataclass(frozen=True)
class TransitionSavings:
    """Cost picture for transitioning STANDARD data to a colder class."""

    target_class: str
    standard_bytes: int
    current_monthly: float
    after_monthly: float
    currency: str = "USD"

    @property
    def monthly(self) -> float:
        return self.current_monthly - self.after_monthly

    @property
    def yearly(self) -> float:
        return self.monthly * 12


def recommend_transitions(
    result: ScanResult,
    pricing: PricingModel,
    target_class: str = "GLACIER",
) -> TransitionSavings:
    """Estimate savings from moving all STANDARD data to ``target_class``.

    Prices the scanned STANDARD volume at its current (tiered STANDARD)
    rate versus the flat rate of ``target_class`` and reports the
    difference. Nothing is deleted; this models a lifecycle transition.
    """
    standard_bytes = result.scanned_by_class.get("STANDARD", 0)
    current_monthly = pricing.monthly_cost({"STANDARD": standard_bytes})
    after_monthly = pricing.monthly_cost({target_class: standard_bytes})
    return TransitionSavings(
        target_class=target_class,
        standard_bytes=standard_bytes,
        current_monthly=current_monthly,
        after_monthly=after_monthly,
        currency=pricing.currency,
    )
