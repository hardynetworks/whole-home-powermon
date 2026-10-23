"""Home Energy Dashboard - API and web app.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8080
Env:  DB_PATH, INGEST_KEY, DEMO_MODE, DEMO_DAYS, TIMEZONE
"""
from __future__ import annotations

import asyncio
import csv
import json
import hmac
import io
import math
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, simulator
from .config import DEFAULT_CONFIG, ConfigError, validate
from .db import Store

STATIC = Path(__file__).parent / "static"
CHANNEL_RE = re.compile(r"^(grid|solar|battery|(circuit|device):[A-Za-z0-9_.\-]{1,40})$")
MAX_RANGE_DAYS = 400


class Sample(BaseModel):
    channel: str
    watts: float
    ts: int | None = None


class IngestBody(BaseModel):
    ts: int | None = None
    readings: dict[str, float] | None = None
    samples: list[Sample] | None = None


class ModeBody(BaseModel):
    demo: bool


class ConfigBody(BaseModel):
    config: dict = Field(default_factory=dict)


def create_app(db_path: str | None = None, demo: bool | None = None, ingest_key: str | None = None,
               demo_days: int | None = None, live_demo: bool = True) -> FastAPI:
    explicit_db = db_path or os.getenv("DB_PATH")
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    mode_file = data_dir / "mode.json"
    if demo is None:
        demo = os.getenv("DEMO_MODE", "false").lower() in ("1", "true", "yes")
        if not explicit_db:                      # a choice made in the app wins over the env default
            try:
                demo = bool(json.loads(mode_file.read_text())["demo"])
            except Exception:
                pass
    ingest_key = os.getenv("INGEST_KEY", "") if ingest_key is None else ingest_key
    demo_days = int(os.getenv("DEMO_DAYS", "45")) if demo_days is None else demo_days
    default_tz = os.getenv("TIMEZONE", DEFAULT_CONFIG["timezone"])

    def path_for(is_demo: bool) -> str:
        return explicit_db or str(data_dir / ("demo.db" if is_demo else "energy.db"))

    class Holder:
        pass

    holder = Holder()
    holder.demo = demo
    holder.store = Store(path_for(demo))
    holder.task = None
    holder.switching = asyncio.Lock()

    class StoreRef:
        """Always points at the current store, so a mode switch needs no other changes."""
        def __getattr__(self, name):
            return getattr(holder.store, name)

    store = StoreRef()

    def load_config() -> dict:
        saved = store.get_setting("config")
        base = dict(DEFAULT_CONFIG, timezone=default_tz)
        try:
            return validate({**base, **(saved or {})}) if saved else validate(base)
        except ConfigError:
            return validate(base)

    async def start_demo():
        """Seed the demo database if empty and start the live simulator."""
        st = holder.store
        cfg = load_config()
        if st.count() == 0:
            await asyncio.to_thread(simulator.backfill, st, cfg["timezone"], demo_days)
            labels = dict(cfg["labels"])
            labels.update(simulator.DEMO_LABELS)
            st.set_setting("config", {**(st.get_setting("config") or {}), "labels": labels})
        if live_demo:
            holder.task = asyncio.create_task(simulator.live_loop(st, cfg["timezone"]))

    async def stop_demo():
        if holder.task:
            holder.task.cancel()
            holder.task = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if holder.demo:
            await start_demo()
        yield
        await stop_demo()

    app = FastAPI(title="Home Energy Dashboard", lifespan=lifespan)
    app.state.store = store

    def parse_day(value: str | None, cfg: dict) -> date:
        if not value:
            return datetime.now(analytics.local_tz(cfg)).date()
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise HTTPException(422, "Dates must look like 2026-10-22")

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if not ingest_key:
            raise HTTPException(403, "Ingest is off. Set INGEST_KEY on the server to turn it on.")
        if not x_api_key or not hmac.compare_digest(x_api_key, ingest_key):
            raise HTTPException(401, "Missing or wrong X-API-Key")

    # ---------------------------------------------------------------- health
    @app.get("/api/health")
    def health():
        return {"ok": True, "demo": holder.demo, "mode_locked": bool(explicit_db), "samples": store.count(), "ingest_enabled": bool(ingest_key)}

    # ---------------------------------------------------------------- ingest
    @app.post("/api/ingest", dependencies=[Depends(require_key)])
    def ingest(body: IngestBody):
        now = int(time.time())
        items: list[tuple[int, str, float]] = []
        for ch, w in (body.readings or {}).items():
            items.append((body.ts or now, ch, w))
        for s in body.samples or []:
            items.append((s.ts or body.ts or now, s.channel, s.watts))
        if not items:
            raise HTTPException(422, "Send readings or samples")
        if len(items) > 5000:
            raise HTTPException(413, "Send at most 5000 samples per request")
        for ts, ch, w in items:
            if not CHANNEL_RE.match(ch):
                raise HTTPException(422, f"Bad channel name: {ch}")
            if not math.isfinite(w) or abs(w) > 1_000_000:
                raise HTTPException(422, f"Bad watts value for {ch}")
            if ts > now + 300 or ts < now - 400 * 86400:
                raise HTTPException(422, f"Timestamp out of range for {ch}")
        return {"stored": store.ingest(items)}

    # ---------------------------------------------------------------- mode
    @app.post("/api/mode")
    async def set_mode(body: ModeBody):
        if explicit_db:
            raise HTTPException(409, "Mode is fixed because DB_PATH is set on the server.")
        async with holder.switching:
            if body.demo != holder.demo:
                await stop_demo()
                holder.store = Store(path_for(body.demo))
                holder.demo = body.demo
                data_dir.mkdir(parents=True, exist_ok=True)
                mode_file.write_text(json.dumps({"demo": body.demo}))
                if body.demo:
                    await start_demo()
        return {"demo": holder.demo}

    # ---------------------------------------------------------------- config
    @app.get("/api/config")
    def get_config():
        return {"config": load_config(), "defaults": DEFAULT_CONFIG, "demo": holder.demo}

    @app.put("/api/config")
    def put_config(body: ConfigBody):
        try:
            cleaned = validate({**load_config(), **body.config})
        except ConfigError as exc:
            raise HTTPException(422, str(exc))
        store.set_setting("config", cleaned)
        return {"config": cleaned}

    # ---------------------------------------------------------------- live
    @app.get("/api/live")
    def live():
        cfg = load_config()
        now = int(time.time())
        fresh = {ch: w for ch, (ts, w) in store.latest.items() if now - ts <= 180}
        last = max((ts for ts, _ in store.latest.values()), default=None)
        grid, solar, batt = fresh.get("grid"), fresh.get("solar"), fresh.get("battery")
        load = None
        if grid is not None:
            load = max(grid + (solar or 0.0) - (batt or 0.0), 0.0)
        today = datetime.fromtimestamp(now, analytics.local_tz(cfg)).date()
        start, end = analytics.day_bounds(cfg, today)
        sm = analytics.summary(store, cfg, start, end)
        return {
            "now": now, "last_update": last, "online": grid is not None,
            "grid": grid, "solar": solar, "battery": batt, "load": load,
            "circuits": [
                {"channel": ch, "label": cfg["labels"].get(ch) or analytics._default_label(ch), "watts": w}
                for ch, w in sorted(fresh.items()) if ch.startswith(("circuit:", "device:"))
            ],
            "tier": analytics.current_tier(cfg, now),
            "today": {"totals": sm["totals"], "by_tier": sm["by_tier"]},
        }

    # ---------------------------------------------------------------- day
    @app.get("/api/day")
    def day(date_: str | None = Query(default=None, alias="date")):
        cfg = load_config()
        d = parse_day(date_, cfg)
        start, end = analytics.day_bounds(cfg, d)
        return {
            "date": d.isoformat(), "start": start, "end": end,
            "series": analytics.series(store, cfg, start, end, 5),
            "bands": analytics.bands(cfg, d),
            "summary": analytics.summary(store, cfg, start, end),
            "plan": analytics.active_plan(cfg)["name"],
        }

    # ---------------------------------------------------------------- range
    def range_bounds(cfg: dict, start_date: str, end_date: str) -> tuple[int, int]:
        a, b = parse_day(start_date, cfg), parse_day(end_date, cfg)
        if b < a:
            raise HTTPException(422, "End date is before start date")
        if (b - a).days + 1 > MAX_RANGE_DAYS:
            raise HTTPException(422, f"Pick at most {MAX_RANGE_DAYS} days")
        return analytics.day_bounds(cfg, a)[0], analytics.day_bounds(cfg, b)[1]

    @app.get("/api/range")
    def range_report(start_date: str, end_date: str):
        cfg = load_config()
        start, end = range_bounds(cfg, start_date, end_date)
        return {
            "start_date": start_date, "end_date": end_date,
            "summary": analytics.summary(store, cfg, start, end),
            "compare": analytics.compare(store, cfg, start, end),
        }

    @app.get("/api/export.csv")
    def export_csv(start_date: str, end_date: str):
        cfg = load_config()
        start, end = range_bounds(cfg, start_date, end_date)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["hour_utc", "hour_local", "tier", "rate_per_kwh", "load_kwh", "solar_kwh",
                         "grid_import_kwh", "grid_export_kwh", "import_cost"])
        writer.writerows(analytics.hourly_csv_rows(store, cfg, start, end))
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="energy_{start_date}_{end_date}.csv"'})

    # ---------------------------------------------------------------- devices
    @app.get("/api/channels")
    def channels():
        cfg = load_config()
        now = int(time.time())
        out = []
        for c in store.channels():
            c["label"] = cfg["labels"].get(c["channel"]) or analytics._default_label(c["channel"])
            c["online"] = now - c["last"] <= 180
            out.append(c)
        return {"channels": out}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
