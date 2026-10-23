"""Demo data generator, so the dashboard can be explored before any hardware
is connected. It models a house in southern Idaho with a small solar array and a
24 V battery bank. Not meant to match any real installation.

Energy balance per step (all watts):
    load    = sum of circuits
    covered = min(solar_potential, load)      solar serving the load directly
    surplus -> charges the battery (any leftover is curtailed, no export)
    deficit -> battery discharges down to a floor, then the grid supplies it
    grid    = deficit - discharge
    solar   = covered + charge                what the panels actually produced
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Store

CIRCUITS = ["servers", "hvac", "water_heater", "kitchen_laundry", "other"]
DEMO_LABELS = {
    "circuit:servers": "Server rack",
    "circuit:hvac": "Heating and cooling",
    "circuit:water_heater": "Water heater",
    "circuit:kitchen_laundry": "Kitchen and laundry",
    "circuit:other": "Lights and plugs",
}

LATITUDE = 43.6
PEAK_SOLAR_W = 2400.0
BATTERY_WH = 4800.0
BATTERY_FLOOR = 0.20
MAX_CHARGE_W = 1800.0
MAX_DISCHARGE_W = 2500.0


def _sun_factor(local: datetime) -> float:
    doy = local.timetuple().tm_yday
    decl = math.radians(23.44 * math.sin(2 * math.pi * (284 + doy) / 365))
    hour = local.hour + local.minute / 60 + local.second / 3600
    ha = math.radians(15 * (hour - 12.8))   # solar noon runs a bit after 12:00 local
    lat = math.radians(LATITUDE)
    alt = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    return max(alt, 0.0) ** 1.15


class Simulator:
    def __init__(self, tz: str = "America/Boise", seed: int = 7):
        self.tz = ZoneInfo(tz)
        self.rng = random.Random(seed)
        self.soc = 0.55
        self._day = None
        self._day_state: dict = {}

    def _new_day(self, local: datetime) -> dict:
        r = self.rng
        weekend = local.weekday() >= 5
        return {
            "cloud": r.choice([1.0, 1.0, 0.95, 0.85, 0.7, 0.5, 0.3]),
            "wh_morning": 6 * 60 + r.randint(0, 50),
            "wh_evening": (19 * 60 + r.randint(0, 90)) if r.random() < 0.7 else None,
            "laundry": (9 * 60 + r.randint(0, 150)) if (weekend or r.random() < 0.3) else None,
            "heat": r.uniform(0.7, 1.3),
        }

    def _circuits(self, local: datetime, dt: float) -> dict[str, float]:
        r = self.rng
        minute = local.hour * 60 + local.minute
        month = local.month
        st = self._day_state

        servers = 1000 + r.gauss(0, 18)
        other = 140 + (105 if (minute % 38) < 13 else 0)          # fridge cycling
        if 17 <= local.hour < 23:
            other += 260 + r.gauss(0, 30)
        elif 6 <= local.hour < 9:
            other += 130
        else:
            other += 60

        hvac = 0.0
        if month in (6, 7, 8, 9):                                   # cooling
            strength = {6: 0.45, 7: 0.9, 8: 0.85, 9: 0.4}[month]
            ramp = max(0.0, math.sin(math.pi * (local.hour - 10) / 13)) if 10 <= local.hour < 23 else 0.05
            duty = min(0.95, strength * ramp * st["cloud"] ** -0.3 * 0.7)
            hvac = 2600.0 if (minute % 30) < duty * 30 else 90.0
        elif month in (11, 12, 1, 2, 3):                            # heating
            ramp = 0.55 if (5 <= local.hour < 8 or 17 <= local.hour < 22) else 0.18
            duty = min(0.9, ramp * st["heat"])
            hvac = 1500.0 if (minute % 30) < duty * 30 else 60.0
        else:
            hvac = 70.0

        water = 0.0
        for start in (st["wh_morning"], st["wh_evening"]):
            if start is not None and start <= minute < start + 25:
                water = 4500.0

        kitchen = 0.0
        if 7 * 60 <= minute < 7 * 60 + 30:
            kitchen += 1800.0
        if 18 * 60 <= minute < 18 * 60 + 45:
            kitchen += 1900.0
        if st["laundry"] is not None and st["laundry"] <= minute < st["laundry"] + 70:
            kitchen += 2100.0 if (minute - st["laundry"]) % 70 < 40 else 600.0

        return {"servers": servers, "hvac": hvac, "water_heater": water,
                "kitchen_laundry": kitchen, "other": other}

    def step(self, ts: int, dt: float = 60.0) -> dict[str, float]:
        local = datetime.fromtimestamp(ts, self.tz)
        if self._day != local.date():
            self._day = local.date()
            self._day_state = self._new_day(local)
        r = self.rng

        circuits = {k: max(0.0, v) for k, v in self._circuits(local, dt).items()}
        load = sum(circuits.values())

        passing = 1.0 - 0.25 * max(0.0, math.sin(ts / 420.0) * math.sin(ts / 97.0))
        potential = PEAK_SOLAR_W * _sun_factor(local) * self._day_state["cloud"] * passing
        potential = max(0.0, potential + (r.gauss(0, 15) if potential > 20 else 0.0))

        covered = min(potential, load)
        surplus = potential - covered
        deficit = load - covered

        room_w = (1.0 - self.soc) * BATTERY_WH * 3600.0 / dt
        charge = min(surplus, MAX_CHARGE_W, room_w) if surplus > 0 else 0.0
        avail_w = max(0.0, (self.soc - BATTERY_FLOOR)) * BATTERY_WH * 3600.0 / dt
        discharge = min(deficit, MAX_DISCHARGE_W, avail_w) if deficit > 0 else 0.0
        self.soc += (charge - discharge) * dt / 3600.0 / BATTERY_WH
        self.soc = min(1.0, max(0.0, self.soc))

        grid = deficit - discharge
        solar = covered + charge
        battery = charge - discharge

        out = {"grid": grid, "solar": solar, "battery": battery}
        out.update({f"circuit:{k}": v for k, v in circuits.items()})
        return {k: round(v, 1) for k, v in out.items()}


def backfill(store: Store, tz: str, days: int, now: int | None = None) -> int:
    """Fill `days` of minute data up to the previous whole minute."""
    now = now or int(time.time())
    end_min = now // 60
    start_min = end_min - days * 1440
    sim = Simulator(tz)
    rows = []
    for m in range(start_min, end_min):
        values = sim.step(m * 60 + 30, 60.0)
        for ch, w in values.items():
            rows.append((m, ch, w))
    store.bulk_insert(rows)
    store.set_setting("sim_soc", sim.soc)
    return len(rows)


async def live_loop(store: Store, tz: str, interval: float = 1.0) -> None:
    """Append a simulated reading every `interval` seconds (1 s, so the live graph moves)."""
    sim = Simulator(tz, seed=int(time.time()))
    sim.soc = store.get_setting("sim_soc", 0.55)
    while True:
        values = sim.step(int(time.time()), interval)
        ts = int(time.time())
        store.ingest([(ts, ch, w) for ch, w in values.items()])
        await asyncio.sleep(interval)
