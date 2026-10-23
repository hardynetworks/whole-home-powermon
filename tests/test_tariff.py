from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app import tariff
from app.config import DEFAULT_CONFIG, ConfigError, validate

TZ = ZoneInfo("America/Boise")
TOU = DEFAULT_CONFIG["plans"]["tou"]


def at(y, m, d, h, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=TZ)


# 2026-07-08 is a Wednesday, 2026-01-14 is a Wednesday.
@pytest.mark.parametrize(
    "moment, expected",
    [
        # Summer weekday boundaries: off 11pm-3pm, mid 3-7pm, on 7-11pm
        (at(2026, 7, 8, 0), "off"),
        (at(2026, 7, 8, 14, 59), "off"),
        (at(2026, 7, 8, 15), "mid"),
        (at(2026, 7, 8, 18, 59), "mid"),
        (at(2026, 7, 8, 19), "on"),
        (at(2026, 7, 8, 22, 59), "on"),
        (at(2026, 7, 8, 23), "off"),
        # Non-summer weekday: on 6-9am and 5-8pm, off otherwise (no mid)
        (at(2026, 1, 14, 5, 59), "off"),
        (at(2026, 1, 14, 6), "on"),
        (at(2026, 1, 14, 8, 59), "on"),
        (at(2026, 1, 14, 9), "off"),
        (at(2026, 1, 14, 16, 59), "off"),
        (at(2026, 1, 14, 17), "on"),
        (at(2026, 1, 14, 19, 59), "on"),
        (at(2026, 1, 14, 20), "off"),
        # Season edges: Sept 30 is summer, Oct 1 is non-summer
        (at(2026, 9, 30, 19), "on"),
        (at(2026, 10, 1, 19), "on"),
        (at(2026, 10, 1, 15), "off"),
        (at(2026, 5, 29, 15), "off"),
        (at(2026, 6, 1, 15), "mid"),
    ],
)
def test_weekday_tiers(moment, expected):
    assert tariff.tier_at(TOU, moment) == expected


def test_saturday_follows_weekday_schedule():
    # 2026-07-11 is a Saturday
    assert date(2026, 7, 11).weekday() == 5
    assert tariff.tier_at(TOU, at(2026, 7, 11, 20)) == "on"


def test_sunday_is_off_peak_all_day():
    # 2026-07-12 is a Sunday; 2026-01-18 is a Sunday
    for hour in range(24):
        assert tariff.tier_at(TOU, at(2026, 7, 12, hour)) == "off"
        assert tariff.tier_at(TOU, at(2026, 1, 18, hour)) == "off"


def test_holidays_2026():
    h = tariff.holidays(2026)
    assert date(2026, 1, 1) in h          # Thursday
    assert date(2026, 5, 25) in h         # Memorial Day (last Monday of May)
    assert date(2026, 7, 4) in h          # Saturday: observed as-is
    assert date(2026, 9, 7) in h          # Labor Day
    assert date(2026, 11, 26) in h        # Thanksgiving
    assert date(2026, 12, 25) in h
    assert len(h) == 6


def test_holiday_is_off_peak():
    assert tariff.tier_at(TOU, at(2026, 7, 4, 20)) == "off"   # July 4 on a Saturday
    assert tariff.tier_at(TOU, at(2026, 11, 26, 18)) == "off"  # Thanksgiving, 6pm
    assert tariff.tier_at(TOU, at(2026, 11, 25, 18)) == "on"   # day before


def test_sunday_holiday_moves_to_monday():
    # 2027: New Year's Day is a Friday; July 4 is a Sunday; Christmas is a Saturday
    assert date(2027, 7, 4).weekday() == 6
    h = tariff.holidays(2027)
    assert date(2027, 7, 5) in h
    # 2023: New Year's Day was a Sunday, so Monday Jan 2 counts
    assert date(2023, 1, 1).weekday() == 6
    assert date(2023, 1, 2) in tariff.holidays(2023)
    # 2022: Christmas was a Sunday, so Monday Dec 26 counts
    assert date(2022, 12, 26) in tariff.holidays(2022)
    # A holiday on a Saturday is not moved
    assert date(2027, 12, 24) not in tariff.holidays(2027)


def test_rates():
    assert tariff.rate_at(TOU, at(2026, 7, 8, 20)) == pytest.approx(0.299185)
    assert tariff.rate_at(TOU, at(2026, 7, 8, 16)) == pytest.approx(0.149594)
    assert tariff.rate_at(TOU, at(2026, 7, 8, 2)) == pytest.approx(0.074797)
    assert tariff.rate_at(TOU, at(2026, 1, 14, 7)) == pytest.approx(0.138347)
    assert tariff.rate_at(TOU, at(2026, 1, 14, 12)) == pytest.approx(0.092231)


def test_dst_transition_day_uses_local_hours():
    # 2026-03-08 is a Sunday (spring forward) - off-peak either way
    assert tariff.tier_at(TOU, at(2026, 3, 8, 18)) == "off"
    # 2026-11-01 is a Sunday (fall back)
    assert tariff.tier_at(TOU, at(2026, 11, 1, 18)) == "off"
    # Monday after fall-back: 6pm is non-summer on-peak
    assert tariff.tier_at(TOU, at(2026, 11, 2, 18)) == "on"


def test_blocks_cost():
    blocks = [{"up_to_kwh": 800, "rate": 0.10}, {"up_to_kwh": None, "rate": 0.20}]
    assert tariff.blocks_cost(blocks, 0, 100) == pytest.approx(10.0)
    # Crossing the 800 kWh boundary: 50 kWh at 0.10 + 50 kWh at 0.20
    assert tariff.blocks_cost(blocks, 750, 100) == pytest.approx(5.0 + 10.0)
    assert tariff.blocks_cost(blocks, 900, 10) == pytest.approx(2.0)


def test_default_config_validates():
    cfg = validate({})
    assert cfg["active_plan"] == "tou"
    assert cfg["plans"]["tou"]["tiers"]["summer"]["on"] == 0.299185


def test_validate_rejects_bad_input():
    with pytest.raises(ConfigError):
        validate({"timezone": "Mars/Olympus"})
    with pytest.raises(ConfigError):
        validate({"active_plan": "missing"})
    with pytest.raises(ConfigError):
        validate({"fixed_monthly": -3})
    bad = {"plans": {"tou": {"name": "x", "type": "tou", "tiers": {"summer": {"off": 0.1}, "non_summer": {"off": 0.1}},
                             "schedule": {"summer": {"on": [[19, 23]]}, "non_summer": {}}}}}
    with pytest.raises(ConfigError):
        validate(bad)
