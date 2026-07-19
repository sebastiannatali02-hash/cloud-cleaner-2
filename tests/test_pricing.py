import pytest

from cloudcleaner.pricing import estimate_savings, monthly_cost

GIB = 1024**3


class TestMonthlyCost:
    def test_standard_first_tier(self):
        assert monthly_cost({"STANDARD": 100 * GIB}) == pytest.approx(100 * 0.023)

    def test_standard_crosses_tiers(self):
        # 60 TiB: first 50 TiB(=51200 GiB) at 0.023, the rest at 0.022
        gb = 60 * 1024
        expected = 51200 * 0.023 + (gb - 51200) * 0.022
        assert monthly_cost({"STANDARD": gb * GIB}) == pytest.approx(expected)

    def test_flat_class(self):
        assert monthly_cost({"GLACIER": 1000 * GIB}) == pytest.approx(1000 * 0.0036)

    def test_override_wins(self):
        assert monthly_cost({"STANDARD": 10 * GIB}, {"STANDARD": 0.05}) == pytest.approx(0.5)

    def test_unknown_class_falls_back_to_standard_price(self):
        assert monthly_cost({"MYSTERY": 10 * GIB}) == pytest.approx(10 * 0.023)


class TestSavings:
    def test_before_after(self):
        scanned = {"STANDARD": 1000 * GIB}
        candidates = {"STANDARD": 400 * GIB}
        savings = estimate_savings(scanned, candidates, currency="EUR")
        assert savings.current_monthly == pytest.approx(1000 * 0.023)
        assert savings.after_monthly == pytest.approx(600 * 0.023)
        assert savings.monthly == pytest.approx(400 * 0.023)
        assert savings.yearly == pytest.approx(400 * 0.023 * 12)
        assert savings.currency == "EUR"

    def test_no_candidates_means_no_savings(self):
        savings = estimate_savings({"STANDARD": 10 * GIB}, {})
        assert savings.monthly == 0
