import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import analytics, simulator
from app.config import validate
from app.db import Store
from app.main import create_app

TZ = ZoneInfo("America/Boise")
KEY = "test-key"


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), demo=False, ingest_key=KEY)
    with TestClient(app) as c:
        c.store = app.state.store
        yield c


def ts_local(y, m, d, h, minute=0):
    return int(datetime(y, m, d, h, minute, tzinfo=TZ).timestamp())


def put_minutes(store, start_ts, minutes, grid, solar=0.0, battery=None, circuits=None):
    rows = []
    for i in range(minutes):
        m = start_ts // 60 + i
        rows.append((m, "grid", grid))
        if solar is not None:
            rows.append((m, "solar", solar))
        if battery is not None:
            rows.append((m, "battery", battery))
        for ch, w in (circuits or {}).items():
            rows.append((m, ch, w))
    store.bulk_insert(rows)


# ------------------------------------------------------------------ ingest
def test_ingest_requires_key(client):
    r = client.post("/api/ingest", json={"readings": {"grid": 100}})
    assert r.status_code == 401
    r = client.post("/api/ingest", json={"readings": {"grid": 100}}, headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_ingest_off_without_key(tmp_path):
    app = create_app(db_path=str(tmp_path / "x.db"), demo=False, ingest_key="")
    with TestClient(app) as c:
        r = c.post("/api/ingest", json={"readings": {"grid": 1}}, headers={"X-API-Key": "anything"})
        assert r.status_code == 403


def test_ingest_validates_input(client):
    h = {"X-API-Key": KEY}
    assert client.post("/api/ingest", json={}, headers=h).status_code == 422
    assert client.post("/api/ingest", json={"readings": {"bad channel!": 5}}, headers=h).status_code == 422
    assert client.post("/api/ingest", json={"readings": {"grid": 1e12}}, headers=h).status_code == 422
    future = int(time.time()) + 10_000
    assert client.post("/api/ingest", json={"ts": future, "readings": {"grid": 5}}, headers=h).status_code == 422


def test_ingest_averages_within_a_minute_and_updates_live(client):
    h = {"X-API-Key": KEY}
    now = int(time.time())
    base = now - (now % 60)
    for w in (100, 200, 300):
        r = client.post("/api/ingest", json={"readings": {"grid": w}, "ts": base + 5}, headers=h)
        assert r.status_code == 200
    row = client.store.query("SELECT avg_w, n FROM samples_1m WHERE channel='grid'")[0]
    assert row["avg_w"] == pytest.approx(200.0)
    assert row["n"] == 3


def test_live_derives_load(client):
    h = {"X-API-Key": KEY}
    r = client.post("/api/ingest", json={"readings": {"grid": 400, "solar": 1500, "battery": 300,
                                                       "circuit:servers": 1000}}, headers=h)
    assert r.status_code == 200
    live = client.get("/api/live").json()
    assert live["online"] is True
    assert live["load"] == pytest.approx(400 + 1500 - 300)
    assert live["circuits"][0]["channel"] == "circuit:servers"
    assert live["tier"]["rate"] > 0


# ------------------------------------------------------------------ analytics
def test_cost_by_tier_summer_weekday(client):
    # Wed 2026-07-08. 1 kW grid import for each hour: 15:00-16:00 mid, 19:00-20:00 on, 02:00-03:00 off
    for hour in (15, 19, 2):
        put_minutes(client.store, ts_local(2026, 7, 8, hour), 60, grid=1000.0, solar=0.0)
    r = client.get("/api/range", params={"start_date": "2026-07-08", "end_date": "2026-07-08"}).json()
    s = r["summary"]
    assert s["by_tier"]["mid"]["kwh"] == pytest.approx(1.0)
    assert s["by_tier"]["on"]["kwh"] == pytest.approx(1.0)
    assert s["by_tier"]["off"]["kwh"] == pytest.approx(1.0)
    assert s["by_tier"]["on"]["cost"] == pytest.approx(0.299185)
    assert s["by_tier"]["mid"]["cost"] == pytest.approx(0.149594)
    assert s["by_tier"]["off"]["cost"] == pytest.approx(0.074797)
    assert s["totals"]["energy_cost"] == pytest.approx(0.299185 + 0.149594 + 0.074797)
    assert s["totals"]["import_kwh"] == pytest.approx(3.0)


def test_solar_value_and_load_balance(client):
    # Noon Wed in July: 2 kW solar, 0.5 kW still imported, battery charging 0.5 kW.
    # load = grid + solar - battery = 0.5 + 2.0 - 0.5 = 2.0 kW
    put_minutes(client.store, ts_local(2026, 7, 8, 12), 60, grid=500.0, solar=2000.0, battery=500.0)
    s = client.get("/api/range", params={"start_date": "2026-07-08", "end_date": "2026-07-08"}).json()["summary"]
    t = s["totals"]
    assert t["load_kwh"] == pytest.approx(2.0)
    assert t["solar_kwh"] == pytest.approx(2.0)
    assert t["import_kwh"] == pytest.approx(0.5)
    # off-peak at noon in summer: solar value = 2.0 kWh * 7.4797 cents
    assert t["solar_value"] == pytest.approx(2.0 * 0.074797)


def test_export_is_not_counted_as_import_or_used_solar(client):
    # 3 kW solar, exporting 1 kW (grid = -1000) -> load = 2 kW, solar used = 2 kW
    put_minutes(client.store, ts_local(2026, 7, 8, 12), 60, grid=-1000.0, solar=3000.0)
    t = client.get("/api/range", params={"start_date": "2026-07-08", "end_date": "2026-07-08"}).json()["summary"]["totals"]
    assert t["import_kwh"] == pytest.approx(0.0)
    assert t["export_kwh"] == pytest.approx(1.0)
    assert t["solar_used_kwh"] == pytest.approx(2.0)
    assert t["load_kwh"] == pytest.approx(2.0)


def test_sunday_and_holiday_are_off_peak(client):
    put_minutes(client.store, ts_local(2026, 7, 12, 20), 60, grid=1000.0, solar=0.0)   # Sunday 8pm
    put_minutes(client.store, ts_local(2026, 11, 26, 18), 60, grid=1000.0, solar=0.0)  # Thanksgiving 6pm
    a = client.get("/api/range", params={"start_date": "2026-07-12", "end_date": "2026-07-12"}).json()["summary"]
    b = client.get("/api/range", params={"start_date": "2026-11-26", "end_date": "2026-11-26"}).json()["summary"]
    assert a["by_tier"]["off"]["kwh"] == pytest.approx(1.0)
    assert a["by_tier"]["on"]["kwh"] == 0
    assert b["by_tier"]["off"]["cost"] == pytest.approx(0.092231)


def test_circuit_costs_use_tier_rates(client):
    # Wed July 8, 7pm: servers 1 kW, water heater 4.5 kW, on-peak
    put_minutes(client.store, ts_local(2026, 7, 8, 19), 60, grid=5500.0, solar=0.0,
                circuits={"circuit:servers": 1000.0, "circuit:water_heater": 4500.0})
    s = client.get("/api/range", params={"start_date": "2026-07-08", "end_date": "2026-07-08"}).json()["summary"]
    by = {c["channel"]: c for c in s["circuits"]}
    assert by["circuit:servers"]["kwh"] == pytest.approx(1.0)
    assert by["circuit:servers"]["cost"] == pytest.approx(0.299185)
    assert by["circuit:water_heater"]["cost"] == pytest.approx(4.5 * 0.299185)
    assert s["circuits"][0]["channel"] == "circuit:water_heater"   # sorted by kWh


def test_adders_fixed_and_bill_estimate(client):
    client.put("/api/config", json={"config": {"fixed_monthly": 30.4375, "adder_per_kwh": 0.01}})
    put_minutes(client.store, ts_local(2026, 7, 8, 2), 60, grid=2000.0, solar=0.0)   # 2 kWh off-peak
    t = client.get("/api/range", params={"start_date": "2026-07-08", "end_date": "2026-07-08"}).json()["summary"]["totals"]
    assert t["energy_cost"] == pytest.approx(2 * 0.074797)
    assert t["adders"] == pytest.approx(0.02)
    assert t["fixed"] == pytest.approx(1.0)       # one day of a 30.4375 monthly charge
    assert t["estimated_bill"] == pytest.approx(2 * 0.074797 + 0.02 + 1.0)


def test_compare_plans(client):
    # 1 kWh at 7pm summer (29.9185c TOU) vs 13.2995c flat standard
    put_minutes(client.store, ts_local(2026, 7, 8, 19), 60, grid=1000.0, solar=0.0)
    c = client.get("/api/range", params={"start_date": "2026-07-08", "end_date": "2026-07-08"}).json()["compare"]
    plans = {p["id"]: p for p in c["plans"]}
    assert plans["tou"]["energy_cost"] == pytest.approx(0.299185)
    assert plans["standard"]["energy_cost"] == pytest.approx(0.132995)
    assert plans["tou"]["vs_standard"] == pytest.approx(0.299185 - 0.132995)


def test_blocks_plan_is_monthly_cumulative(client):
    cfg = {"plans": {
        "tou": client.get("/api/config").json()["config"]["plans"]["tou"],
        "standard": {"name": "Standard", "type": "blocks",
                     "blocks": [{"up_to_kwh": 1.5, "rate": 0.10}, {"up_to_kwh": None, "rate": 0.20}]}}}
    assert client.put("/api/config", json={"config": cfg}).status_code == 200
    put_minutes(client.store, ts_local(2026, 7, 8, 2), 120, grid=1000.0, solar=0.0)   # 2 kWh
    c = client.get("/api/range", params={"start_date": "2026-07-08", "end_date": "2026-07-08"}).json()["compare"]
    std = next(p for p in c["plans"] if p["id"] == "standard")
    assert std["energy_cost"] == pytest.approx(1.5 * 0.10 + 0.5 * 0.20)


def test_day_endpoint_shape_and_bands(client):
    put_minutes(client.store, ts_local(2026, 7, 8, 10), 120, grid=800.0, solar=1200.0)
    d = client.get("/api/day", params={"date": "2026-07-08"}).json()
    assert d["date"] == "2026-07-08"
    assert d["series"]["points"], "expected chart points"
    tiers = [b["tier"] for b in d["bands"]]
    assert tiers == ["off", "mid", "on", "off"]
    first, last = d["bands"][0], d["bands"][-1]
    assert first["start"] == d["start"] and last["end"] == d["end"]
    assert d["bands"][1]["start"] == ts_local(2026, 7, 8, 15)
    assert d["bands"][2]["start"] == ts_local(2026, 7, 8, 19)


def test_next_rate_change():
    cfg = validate({})
    # Wed 2026-07-08 4pm (mid-peak) -> on-peak at 7pm
    cur = analytics.current_tier(cfg, ts_local(2026, 7, 8, 16, 30))
    assert cur["tier"] == "mid"
    assert cur["next"]["tier"] == "on"
    assert cur["next"]["at"] == ts_local(2026, 7, 8, 19)
    # Sat 2026-07-11 11:30pm (off-peak) -> stays off through Sunday, then off Mon until 3pm mid
    cur = analytics.current_tier(cfg, ts_local(2026, 7, 11, 23, 30))
    assert cur["tier"] == "off"
    assert cur["next"]["tier"] == "mid"
    assert cur["next"]["at"] == ts_local(2026, 7, 13, 15)
    # Non-summer 10am weekday -> on-peak at 5pm
    cur = analytics.current_tier(cfg, ts_local(2026, 1, 14, 10))
    assert cur["next"]["at"] == ts_local(2026, 1, 14, 17)


def test_bands_non_summer(client):
    d = client.get("/api/day", params={"date": "2026-01-14"}).json()
    assert [b["tier"] for b in d["bands"]] == ["off", "on", "off", "on", "off"]


def test_dst_day_has_23_hours(client):
    cfg = validate({})
    start, end = analytics.day_bounds(cfg, datetime(2026, 3, 8).date())
    assert (end - start) == 23 * 3600


def test_csv_export(client):
    put_minutes(client.store, ts_local(2026, 7, 8, 19), 60, grid=1000.0, solar=0.0)
    r = client.get("/api/export.csv", params={"start_date": "2026-07-08", "end_date": "2026-07-08"})
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("hour_utc,hour_local,tier")
    assert len(lines) == 2
    assert ",on," in lines[1]


def test_config_roundtrip_and_validation(client):
    r = client.put("/api/config", json={"config": {"labels": {"circuit:hvac": "AC"}}})
    assert r.status_code == 200
    assert client.get("/api/config").json()["config"]["labels"]["circuit:hvac"] == "AC"
    assert client.put("/api/config", json={"config": {"timezone": "Nope/Nada"}}).status_code == 422
    assert client.get("/api/day", params={"date": "garbage"}).status_code == 422
    assert client.get("/api/range", params={"start_date": "2026-07-09", "end_date": "2026-07-08"}).status_code == 422


def test_unknown_api_path_is_404_and_root_serves_page(client):
    assert client.get("/api/nope").status_code == 404
    assert client.get("/").status_code == 200


# ------------------------------------------------------------------ simulator
def test_simulator_energy_balance():
    sim = simulator.Simulator("America/Boise", seed=1)
    base = ts_local(2026, 7, 8, 0)
    for i in range(0, 24 * 60, 7):
        v = sim.step(base + i * 60, 60.0)
        circuits = sum(val for k, val in v.items() if k.startswith("circuit:"))
        derived = v["grid"] + v["solar"] - v["battery"]
        assert derived == pytest.approx(circuits, abs=1.0)
        assert v["grid"] >= -0.5
        assert 0.0 <= sim.soc <= 1.0


def test_simulator_backfill_and_report(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    n = simulator.backfill(store, "America/Boise", 3)
    assert n > 3 * 1440 * 6
    cfg = validate({})
    now = int(time.time())
    s = analytics.summary(store, cfg, now - 2 * 86400, now - 3600)
    assert s["has_data"]
    assert s["totals"]["solar_kwh"] > 0
    assert s["totals"]["import_kwh"] > 0
    # servers run ~1 kW all the time -> about 24 kWh/day
    servers = next(c for c in s["circuits"] if c["channel"] == "circuit:servers")
    assert 40 < servers["kwh"] < 50


def test_demo_mode_boots_and_serves_data(tmp_path):
    app = create_app(db_path=str(tmp_path / "d.db"), demo=True, ingest_key="", demo_days=4, live_demo=False)
    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h["demo"] is True and h["samples"] > 1000
        d = c.get("/api/day").json()
        assert d["series"]["points"]
        labels = c.get("/api/config").json()["config"]["labels"]
        assert labels["circuit:hvac"] == "Heating and cooling"
