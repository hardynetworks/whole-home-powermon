"""Energy and cost calculations over the minute table.

Everything is computed from watts per minute:
  load      = grid + solar - battery           (whole-home consumption)
  import    = max(grid, 0)                     (energy bought from the utility)
  export    = max(-grid, 0)
  solar_used = max(solar - export, 0)          (solar that stayed in the home)
Costs use the rate tier in force at the local hour each kWh was bought.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import tariff
from .config import TIER_NAMES
from .db import Store

DERIVED_CTE = """
WITH m AS (
  SELECT ts_min,
         SUM(CASE WHEN channel='grid'    THEN avg_w END) AS grid,
         SUM(CASE WHEN channel='solar'   THEN avg_w END) AS solar,
         SUM(CASE WHEN channel='battery' THEN avg_w END) AS batt
  FROM samples_1m
  WHERE ts_min >= ? AND ts_min < ? AND channel IN ('grid','solar','battery')
  GROUP BY ts_min
  HAVING grid IS NOT NULL
)
"""


def local_tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg["timezone"])


def day_bounds(cfg: dict, day: date) -> tuple[int, int]:
    """Epoch seconds for local midnight to next local midnight."""
    tz = local_tz(cfg)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def hourly(store: Store, start: int, end: int) -> list[dict]:
    """One row per UTC hour: kWh of import, export, solar, solar_used, load."""
    rows = store.query(
        DERIVED_CTE
        + """
        SELECT ts_min / 60 AS h,
               SUM(MAX(grid, 0)) / 60000.0 AS imp,
               SUM(MAX(-grid, 0)) / 60000.0 AS exp,
               SUM(COALESCE(solar, 0)) / 60000.0 AS sol,
               SUM(MAX(COALESCE(solar, 0) - MAX(-grid, 0), 0)) / 60000.0 AS sol_used,
               SUM(MAX(grid + COALESCE(solar, 0) - COALESCE(batt, 0), 0)) / 60000.0 AS load
        FROM m GROUP BY h ORDER BY h
        """,
        (start // 60, end // 60),
    )
    return [
        {"ts": r["h"] * 3600, "imp": r["imp"], "exp": r["exp"], "sol": r["sol"],
         "sol_used": r["sol_used"], "load": r["load"]}
        for r in rows
    ]


def circuits_hourly(store: Store, start: int, end: int) -> dict[str, dict[int, float]]:
    """channel -> {hour_ts: kWh} for circuit:* and device:* channels."""
    rows = store.query(
        """
        SELECT ts_min / 60 AS h, channel, SUM(MAX(avg_w, 0)) / 60000.0 AS kwh
        FROM samples_1m
        WHERE ts_min >= ? AND ts_min < ? AND (channel LIKE 'circuit:%' OR channel LIKE 'device:%')
        GROUP BY h, channel
        """,
        (start // 60, end // 60),
    )
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for r in rows:
        out[r["channel"]][r["h"] * 3600] = r["kwh"]
    return out


def _local(ts: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def active_plan(cfg: dict) -> dict:
    return cfg["plans"][cfg["active_plan"]]


def current_tier(cfg: dict, now_ts: int) -> dict:
    plan = active_plan(cfg)
    local = _local(now_ts, local_tz(cfg))
    if plan["type"] == "tou":
        tier = tariff.tier_at(plan, local)
        rate = tariff.rate_at(plan, local)
        nxt = None
        t = (now_ts // 3600 + 1) * 3600
        tz = local_tz(cfg)
        for _ in range(96):   # look up to four days ahead for the next change
            when = _local(t, tz)
            nt = tariff.tier_at(plan, when)
            if nt != tier:
                nxt = {"at": t, "tier": nt, "label": TIER_NAMES[nt], "rate": tariff.rate_at(plan, when)}
                break
            t += 3600
        return {"tier": tier, "label": TIER_NAMES[tier], "rate": rate, "plan": plan["name"],
                "season": tariff.season_of(plan, local), "holiday": tariff.is_holiday(local.date()),
                "next": nxt}
    return {"tier": "flat", "label": "Flat rate", "rate": float(plan["blocks"][0]["rate"]),
            "plan": plan["name"], "season": None, "holiday": False, "next": None}


def bands(cfg: dict, day: date) -> list[dict]:
    """Rate tier segments across one local day, for shading charts."""
    start, end = day_bounds(cfg, day)
    plan = active_plan(cfg)
    tz = local_tz(cfg)
    out: list[dict] = []
    t = start
    while t < end:
        local = _local(t, tz)
        if plan["type"] == "tou":
            tier, rate = tariff.tier_at(plan, local), tariff.rate_at(plan, local)
        else:
            tier, rate = "flat", float(plan["blocks"][0]["rate"])
        if out and out[-1]["tier"] == tier:
            out[-1]["end"] = t + 3600
        else:
            out.append({"start": t, "end": t + 3600, "tier": tier, "rate": rate})
        t += 3600
    if out:
        out[-1]["end"] = end
    return out


def series(store: Store, cfg: dict, start: int, end: int, res_min: int) -> dict:
    """Average watts per bucket for the power chart."""
    res_min = max(1, int(res_min))
    rows = store.query(
        DERIVED_CTE
        + """
        SELECT (ts_min / ?) * ? AS b,
               AVG(grid) AS grid, AVG(COALESCE(solar, 0)) AS solar,
               AVG(COALESCE(batt, 0)) AS batt,
               AVG(MAX(grid + COALESCE(solar, 0) - COALESCE(batt, 0), 0)) AS load
        FROM m GROUP BY b ORDER BY b
        """,
        (start // 60, end // 60, res_min, res_min),
    )
    has_battery = bool(store.query(
        "SELECT 1 FROM samples_1m WHERE channel='battery' AND ts_min>=? AND ts_min<? LIMIT 1",
        (start // 60, end // 60)))
    return {
        "start": start, "end": end, "res_min": res_min, "has_battery": has_battery,
        "points": [
            {"t": r["b"] * 60, "grid": round(r["grid"], 1), "solar": round(r["solar"], 1),
             "battery": round(r["batt"], 1), "load": round(r["load"], 1)}
            for r in rows
        ],
    }


def _blank_tier() -> dict:
    return {"kwh": 0.0, "cost": 0.0}


def _plan_energy_cost(plan: dict, rows: list[dict], tz: ZoneInfo) -> float:
    """Energy-charge cost of the import in `rows` under `plan` (no fixed or adders)."""
    total = 0.0
    month_used: dict[tuple, float] = defaultdict(float)
    for row in rows:
        local = _local(row["ts"], tz)
        if plan["type"] == "tou":
            total += row["imp"] * tariff.rate_at(plan, local)
        else:
            key = (local.year, local.month)
            total += tariff.blocks_cost(plan["blocks"], month_used[key], row["imp"])
            month_used[key] += row["imp"]
    return total


def summary(store: Store, cfg: dict, start: int, end: int) -> dict:
    """Totals for [start, end): energy, cost by tier, by day and by circuit."""
    tz = local_tz(cfg)
    plan = active_plan(cfg)
    rows = hourly(store, start, end)
    adder = cfg["adder_per_kwh"]

    by_tier = {k: _blank_tier() for k in (("on", "mid", "off") if plan["type"] == "tou" else ("flat",))}
    by_day: dict[str, dict] = {}
    tot = {"import_kwh": 0.0, "export_kwh": 0.0, "solar_kwh": 0.0, "solar_used_kwh": 0.0,
           "load_kwh": 0.0, "energy_cost": 0.0, "solar_value": 0.0, "export_credit": 0.0}
    month_used: dict[tuple, float] = defaultdict(float)
    hour_rate: dict[int, tuple[str, float]] = {}

    for row in rows:
        local = _local(row["ts"], tz)
        if plan["type"] == "tou":
            tier = tariff.tier_at(plan, local)
            rate = tariff.rate_at(plan, local)
            cost = row["imp"] * rate
        else:
            tier = "flat"
            key = (local.year, local.month)
            cost = tariff.blocks_cost(plan["blocks"], month_used[key], row["imp"])
            month_used[key] += row["imp"]
            rate = cost / row["imp"] if row["imp"] > 1e-12 else float(plan["blocks"][0]["rate"])
        hour_rate[row["ts"]] = (tier, rate)
        by_tier[tier]["kwh"] += row["imp"]
        by_tier[tier]["cost"] += cost
        tot["import_kwh"] += row["imp"]
        tot["export_kwh"] += row["exp"]
        tot["solar_kwh"] += row["sol"]
        tot["solar_used_kwh"] += row["sol_used"]
        tot["load_kwh"] += row["load"]
        tot["energy_cost"] += cost
        tot["solar_value"] += row["sol_used"] * rate
        tot["export_credit"] += row["exp"] * cfg["export_credit_per_kwh"]

        dkey = local.date().isoformat()
        d = by_day.setdefault(dkey, {"date": dkey, "import_kwh": 0.0, "solar_kwh": 0.0, "load_kwh": 0.0,
                                     "cost": 0.0, "tiers": {k: 0.0 for k in by_tier}})
        d["import_kwh"] += row["imp"]
        d["solar_kwh"] += row["sol"]
        d["load_kwh"] += row["load"]
        d["cost"] += cost
        d["tiers"][tier] += cost

    # per-circuit
    circuits = []
    for channel, hours in circuits_hourly(store, start, end).items():
        kwh = cost = 0.0
        for ts, val in hours.items():
            rate = hour_rate.get(ts, (None, None))[1]
            if rate is None:
                local = _local(ts, tz)
                rate = tariff.rate_at(plan, local) if plan["type"] == "tou" else float(plan["blocks"][0]["rate"])
            kwh += val
            cost += val * rate
        circuits.append({"channel": channel, "label": cfg["labels"].get(channel) or _default_label(channel),
                         "kwh": kwh, "cost": cost})
    circuits.sort(key=lambda c: c["kwh"], reverse=True)

    # fixed charge, prorated per calendar day covered by the data window
    days_span = max(1, round((end - start) / 86400)) if rows else 0
    fixed = cfg["fixed_monthly"] / 30.4375 * days_span
    adders = tot["import_kwh"] * adder
    bill = tot["energy_cost"] + adders + fixed - tot["export_credit"]

    return {
        "start": start, "end": end, "plan": plan["name"], "plan_type": plan["type"],
        "totals": {**tot, "adders": adders, "fixed": fixed, "estimated_bill": bill,
                   "solar_value": tot["solar_value"]},
        "by_tier": by_tier,
        "by_day": [by_day[k] for k in sorted(by_day)],
        "circuits": circuits,
        "has_data": bool(rows),
    }


def _default_label(channel: str) -> str:
    name = channel.split(":", 1)[-1].replace("_", " ").replace("-", " ")
    return name[:1].upper() + name[1:]


def compare(store: Store, cfg: dict, start: int, end: int) -> dict:
    """Energy-charge cost of the same usage under every configured plan."""
    tz = local_tz(cfg)
    rows = hourly(store, start, end)
    out = []
    for pid, plan in cfg["plans"].items():
        cost = _plan_energy_cost(plan, rows, tz)
        out.append({"id": pid, "name": plan["name"], "type": plan["type"], "energy_cost": cost,
                    "active": pid == cfg["active_plan"]})
    base = next((p for p in out if p["type"] == "blocks"), out[0])
    for p in out:
        p["vs_standard"] = p["energy_cost"] - base["energy_cost"]
    return {"start": start, "end": end, "import_kwh": sum(r["imp"] for r in rows), "plans": out,
            "baseline": base["id"]}


def month_range(cfg: dict, year: int, month: int) -> tuple[int, int]:
    tz = local_tz(cfg)
    first = datetime(year, month, 1, tzinfo=tz)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    nxt = datetime(ny, nm, 1, tzinfo=tz)
    return int(first.timestamp()), int(nxt.timestamp())


def hourly_csv_rows(store: Store, cfg: dict, start: int, end: int) -> list[list]:
    tz = local_tz(cfg)
    plan = active_plan(cfg)
    rows = hourly(store, start, end)
    out = []
    for row in rows:
        local = _local(row["ts"], tz)
        tier = tariff.tier_at(plan, local) if plan["type"] == "tou" else "flat"
        rate = tariff.rate_at(plan, local) if plan["type"] == "tou" else float(plan["blocks"][0]["rate"])
        out.append([
            datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(), local.isoformat(),
            tier, f"{rate:.6f}", f"{row['load']:.4f}", f"{row['sol']:.4f}", f"{row['imp']:.4f}",
            f"{row['exp']:.4f}", f"{row['imp'] * rate:.4f}",
        ])
    return out


def live_history(store: Store, since: int, tol: int = 15) -> list[dict]:
    """Raw recent readings (about 1 per collector cycle), newest last, for the live graph.
    Solar and battery are matched to each grid reading within `tol` seconds."""
    rec = store.recent_points(since)
    grid = rec.get("grid", [])
    solar = dict(rec.get("solar", []))
    batt = dict(rec.get("battery", []))

    def near(d: dict, ts: int):
        if not d:
            return None
        for off in range(tol + 1):
            for t in (ts - off, ts + off):
                if t in d:
                    return d[t]
        return None

    out = []
    for ts, g in grid:
        so, ba = near(solar, ts), near(batt, ts)
        out.append({
            "t": ts, "grid": round(g, 1),
            "solar": None if so is None else round(so, 1),
            "battery": None if ba is None else round(ba, 1),
            "load": round(max(g + (so or 0.0) - (ba or 0.0), 0.0), 1),
        })
    return out
