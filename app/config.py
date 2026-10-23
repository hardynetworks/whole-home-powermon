"""Default configuration and validation.

All rates are stored in dollars per kWh. The UI shows cents.
The defaults match Idaho Power's residential Time-of-Use plan (Schedule 5)
as published on its Time-of-Use page (energy charge only).
"""
from __future__ import annotations

import copy
from zoneinfo import ZoneInfo

DEFAULT_CONFIG: dict = {
    "timezone": "America/Boise",
    "active_plan": "tou",
    "plans": {
        "tou": {
            "name": "Time-of-Use (Schedule 5)",
            "type": "tou",
            "summer_months": [6, 7, 8, 9],
            "tiers": {
                "summer": {"on": 0.299185, "mid": 0.149594, "off": 0.074797},
                "non_summer": {"on": 0.138347, "off": 0.092231},
            },
            # Hour windows [start, end) for Monday-Saturday. Any hour not
            # listed is off-peak. Sundays and holidays are off-peak all day.
            "schedule": {
                "summer": {"on": [[19, 23]], "mid": [[15, 19]]},
                "non_summer": {"on": [[6, 9], [17, 20]]},
            },
        },
        "standard": {
            "name": "Standard (flat estimate)",
            "type": "blocks",
            # The real Standard plan is tiered by monthly usage. Add blocks
            # here to model it: {"up_to_kwh": 800, "rate": 0.12}, then a last
            # block with "up_to_kwh": null.
            "blocks": [{"up_to_kwh": None, "rate": 0.132995}],
        },
    },
    # Monthly fixed charge in dollars (prorated per day in reports).
    "fixed_monthly": 0.0,
    # Riders, taxes and other per-kWh charges added on top of the energy rate.
    "adder_per_kwh": 0.0,
    # Credit for energy sent to the grid. 0 means no export credit.
    "export_credit_per_kwh": 0.0,
    # Friendly names: {"circuit:hvac": "Heating and cooling"}
    "labels": {},
}

TIER_NAMES = {"on": "On-peak", "mid": "Mid-peak", "off": "Off-peak"}


class ConfigError(ValueError):
    pass


def merged(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key not in ("plans",):
            out[key] = merged(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _num(value, name: str, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number")
    if number < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return number


def validate(cfg: dict) -> dict:
    """Return a cleaned copy of cfg or raise ConfigError."""
    cfg = merged(DEFAULT_CONFIG, cfg)
    try:
        ZoneInfo(cfg["timezone"])
    except Exception:
        raise ConfigError(f"Unknown timezone: {cfg['timezone']}")

    plans = cfg.get("plans") or {}
    if not plans:
        raise ConfigError("At least one plan is required")
    if cfg["active_plan"] not in plans:
        raise ConfigError("active_plan must match a plan id")

    for pid, plan in plans.items():
        if "name" not in plan or "type" not in plan:
            raise ConfigError(f"Plan {pid} needs a name and type")
        if plan["type"] == "tou":
            for season in ("summer", "non_summer"):
                tiers = plan.get("tiers", {}).get(season)
                if not tiers or "off" not in tiers:
                    raise ConfigError(f"Plan {pid}: {season} needs an off-peak rate")
                for tier, rate in tiers.items():
                    if tier not in TIER_NAMES:
                        raise ConfigError(f"Plan {pid}: unknown tier {tier}")
                    _num(rate, f"{pid} {season} {tier} rate")
                for tier, windows in plan.get("schedule", {}).get(season, {}).items():
                    if tier not in tiers:
                        raise ConfigError(f"Plan {pid}: schedule uses {tier} but {season} has no {tier} rate")
                    for window in windows:
                        if len(window) != 2 or not (0 <= window[0] < window[1] <= 24):
                            raise ConfigError(f"Plan {pid}: bad hour window {window}")
            months = plan.get("summer_months", [])
            if any((not isinstance(m, int)) or m < 1 or m > 12 for m in months):
                raise ConfigError(f"Plan {pid}: summer_months must be numbers from 1 to 12")
        elif plan["type"] == "blocks":
            blocks = plan.get("blocks") or []
            if not blocks:
                raise ConfigError(f"Plan {pid} needs at least one block")
            for i, block in enumerate(blocks):
                _num(block.get("rate"), f"{pid} block {i + 1} rate")
                limit = block.get("up_to_kwh")
                if limit is None and i != len(blocks) - 1:
                    raise ConfigError(f"Plan {pid}: only the last block can be unlimited")
                if limit is not None:
                    _num(limit, f"{pid} block {i + 1} limit")
        else:
            raise ConfigError(f"Plan {pid}: unknown type {plan['type']}")

    cfg["fixed_monthly"] = _num(cfg["fixed_monthly"], "fixed_monthly")
    cfg["adder_per_kwh"] = _num(cfg["adder_per_kwh"], "adder_per_kwh")
    cfg["export_credit_per_kwh"] = _num(cfg["export_credit_per_kwh"], "export_credit_per_kwh")
    if not isinstance(cfg.get("labels"), dict):
        raise ConfigError("labels must be an object")
    return cfg
