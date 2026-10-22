from datetime import date

import pytest

from shared.finance import (PartPrepayment, RateChange, add_months, build_schedule, emi, prepayment_impact, project,
                            remaining_tenure)

START = [RateChange(date(2020, 1, 1), 10.0, "sanction")]


def test_emi_matches_standard_formula():
    # Rs 10 lakh, 10% p.a., 10 years: widely published EMI is Rs 13,215
    assert round(emi(1_000_000, 10, 120)) == 13215


def test_remaining_tenure_inverts_emi():
    assert remaining_tenure(1_000_000, 10, emi(1_000_000, 10, 120)) == 120
    with pytest.raises(ValueError):
        remaining_tenure(1_000_000, 12, 10_000)  # EMI below monthly interest


def test_add_months_clamps_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_schedule_amortizes_to_zero_and_repays_principal():
    s = build_schedule(1_000_000, 120, date(2024, 1, 5), date(2024, 2, 5), START)
    assert len(s) == 120
    assert s[-1].closing_principal == 0
    assert sum(i.principal for i in s) == pytest.approx(1_000_000, abs=0.5)


def test_broken_period_interest_added_to_first_emi():
    s = build_schedule(1_000_000, 120, date(2024, 1, 20), date(2024, 3, 5), START)
    assert s[0].broken_period_interest == pytest.approx(1_000_000 * 0.10 * 16 / 365, abs=0.01)
    assert s[0].emi > s[1].emi


def test_rate_reset_emi_mode_raises_emi_from_next_instalment():
    rates = START + [RateChange(date(2024, 6, 15), 10.5, "hike")]
    s = build_schedule(1_000_000, 120, date(2024, 1, 5), date(2024, 2, 5), rates, "EMI")
    jun, jul = s[4], s[5]  # due 5-Jun (before hike), 5-Jul (after)
    assert jun.rate_pct == 10.0 and jul.rate_pct == 10.5
    assert jul.emi > jun.emi and len(s) == 120


def test_rate_reset_tenure_mode_keeps_emi_and_extends_tenure():
    rates = START + [RateChange(date(2024, 6, 15), 10.5, "hike")]
    s = build_schedule(1_000_000, 120, date(2024, 1, 5), date(2024, 2, 5), rates, "TENURE")
    assert s[5].emi == s[4].emi
    assert len(s) > 120


def test_tenure_mode_respects_max_tenure():
    rates = START + [RateChange(date(2024, 6, 15), 12.0, "big hike")]
    s = build_schedule(1_000_000, 120, date(2024, 1, 5), date(2024, 2, 5), rates, "TENURE", max_tenure_months=120)
    assert len(s) == 120 and s[5].emi > s[4].emi


def test_part_prepayment_reduce_tenure():
    pp = [PartPrepayment(date(2024, 6, 20), 200_000, "REDUCE_TENURE")]
    s = build_schedule(1_000_000, 120, date(2024, 1, 5), date(2024, 2, 5), START, prepayments=pp)
    assert len(s) < 120
    assert sum(i.principal for i in s) == pytest.approx(800_000, abs=0.5)


def test_prepayment_impact_saves_interest():
    base = round(emi(1_000_000, 10, 120))
    tenure = prepayment_impact(1_000_000, 10, base, 100_000, "REDUCE_TENURE")
    lower_emi = prepayment_impact(1_000_000, 10, base, 100_000, "REDUCE_EMI")
    assert tenure.interest_saved > lower_emi.interest_saved > 0  # shortening tenure saves more
    assert tenure.months_saved > 0 and tenure.emi_change == 0
    assert lower_emi.months_saved == 0 and lower_emi.emi_change < 0


def test_project_total_interest_matches_schedule():
    s = build_schedule(1_000_000, 120, date(2024, 1, 5), date(2024, 2, 5), START)
    p = project(1_000_000, 10, s[0].emi)
    assert p.months == 120
    assert p.total_interest == pytest.approx(sum(i.interest for i in s), abs=1)
