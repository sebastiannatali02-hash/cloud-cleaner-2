"""Storage cost estimation.

This is the heart of the business model: the customer pays a share of
the difference between what they spend today and what they spend after
the cleanup, so every scan produces a before/after monthly cost.

Prices default to public AWS S3 us-east-1 list prices (USD per GB-month)
and can be overridden per storage class from the YAML config to match
the customer's region or negotiated rates.
"""

from __future__ import annotations

from dataclasses import dataclass

GIB = 1024**3

# S3 Standard is tiered by total volume; other classes are flat.
STANDARD_TIERS: list[tuple[float, float]] = [
    # (tier ceiling in GB, price per GB-month)
    (50 * 1024, 0.023),
    (450 * 1024, 0.022),
    (float("inf"), 0.021),
]

FLAT_PRICES: dict[str, float] = {
    "STANDARD_IA": 0.0125,
    "ONEZONE_IA": 0.01,
    "INTELLIGENT_TIERING": 0.023,
    "GLACIER_IR": 0.004,
    "GLACIER": 0.0036,
    "DEEP_ARCHIVE": 0.00099,
    "REDUCED_REDUNDANCY": 0.023,
}


def monthly_cost(bytes_by_class: dict[str, int], overrides: dict[str, float] | None = None) -> float:
    """Estimated monthly storage cost for the given per-class byte totals."""
    overrides = overrides or {}
    total = 0.0
    for storage_class, size_bytes in bytes_by_class.items():
        gb = size_bytes / GIB
        if storage_class in overrides:
            total += gb * overrides[storage_class]
        elif storage_class == "STANDARD":
            total += _tiered_standard_cost(gb)
        else:
            total += gb * FLAT_PRICES.get(storage_class, STANDARD_TIERS[0][1])
    return total


def _tiered_standard_cost(gb: float) -> float:
    cost, floor = 0.0, 0.0
    for ceiling, price in STANDARD_TIERS:
        if gb <= floor:
            break
        cost += (min(gb, ceiling) - floor) * price
        floor = ceiling
    return cost


@dataclass(frozen=True)
class Savings:
    """Before/after cost picture for a scan."""

    current_monthly: float
    after_monthly: float
    currency: str = "USD"

    @property
    def monthly(self) -> float:
        return self.current_monthly - self.after_monthly

    @property
    def yearly(self) -> float:
        return self.monthly * 12


def estimate_savings(
    scanned_by_class: dict[str, int],
    candidates_by_class: dict[str, int],
    overrides: dict[str, float] | None = None,
    currency: str = "USD",
) -> Savings:
    """Cost now vs. cost once the candidate objects are gone.

    Standard-tier pricing is volume dependent, so the "after" cost is
    computed on the remaining volume rather than by subtracting the
    candidates' standalone cost.
    """
    remaining = {
        cls: scanned_by_class.get(cls, 0) - candidates_by_class.get(cls, 0)
        for cls in scanned_by_class
    }
    return Savings(
        current_monthly=monthly_cost(scanned_by_class, overrides),
        after_monthly=monthly_cost(remaining, overrides),
        currency=currency,
    )
