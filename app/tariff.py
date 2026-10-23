"""Tariff engine: which rate tier applies to a moment, and what it costs.

Idaho Power Time-of-Use rules (Schedule 5), from its published plan page:
  * Summer (June-September), Monday-Saturday:
      on-peak 7-11 p.m., mid-peak 3-7 p.m., off-peak 11 p.m.-3 p.m.
  * Non-summer (October-May), Monday-Saturday:
      on-peak 6-9 a.m. and 5-8 p.m., off-peak otherwise.
  * Sundays and holidays are off-peak all day.
  * Holidays: New Year's Day, Memorial Day, Independence Day, Labor Day,
    Thanksgiving Day and Christmas Day. When New Year's Day, Independence
    Day or Christmas falls on a Sunday, the following Monday is the holiday.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from functools import lru_cache


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th given weekday (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))
    last_day = calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset:
    days = set()
    for month, day in ((1, 1), (7, 4), (12, 25)):
        d = date(year, month, day)
        days.add(d)
        if d.weekday() == 6:  # fell on a Sunday: the Monday after counts
            days.add(d + timedelta(days=1))
    days.add(_nth_weekday(year, 5, 0, -1))   # Memorial Day
    days.add(_nth_weekday(year, 9, 0, 1))    # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))   # Thanksgiving
    return frozenset(days)


def is_holiday(d: date) -> bool:
    return d in holidays(d.year)


def season_of(plan: dict, local: datetime) -> str:
    return "summer" if local.month in plan.get("summer_months", [6, 7, 8, 9]) else "non_summer"


def tier_at(plan: dict, local: datetime) -> str:
    """Rate tier ('on', 'mid', 'off') for a local, timezone-aware moment."""
    if plan.get("type") != "tou":
        return "off"
    if local.weekday() == 6 or is_holiday(local.date()):
        return "off"
    schedule = plan.get("schedule", {}).get(season_of(plan, local), {})
    for tier in ("on", "mid"):
        for start, end in schedule.get(tier, []):
            if start <= local.hour < end:
                return tier
    return "off"


def rate_at(plan: dict, local: datetime) -> float:
    """Energy rate in dollars per kWh at a local moment (TOU plans)."""
    tiers = plan["tiers"][season_of(plan, local)]
    return float(tiers[tier_at(plan, local)])


def tier_rate(plan: dict, local: datetime, tier: str) -> float:
    return float(plan["tiers"][season_of(plan, local)].get(tier, 0.0))


def blocks_cost(blocks: list, month_kwh_before: float, kwh: float) -> float:
    """Cost of `kwh` more energy given usage already billed this month."""
    cost = 0.0
    used = month_kwh_before
    remaining = kwh
    floor = 0.0
    for block in blocks:
        limit = block.get("up_to_kwh")
        top = float("inf") if limit is None else float(limit)
        room = max(0.0, top - max(used, floor))
        take = min(remaining, room)
        cost += take * float(block["rate"])
        remaining -= take
        used += take
        floor = top
        if remaining <= 1e-12:
            break
    return cost
