# Whole-home power monitor

A self-hosted dashboard for whole-home power, solar and battery, with reports and cost breakdowns
using Idaho Power Time-of-Use rates (editable). No Home Assistant or cloud account needed.

- **Overview**: live power, current rate tier and next change, a day chart shaded by rate tier, cost by tier and by circuit
- **Reports**: week, month or custom ranges, cost per day by tier, bill estimate, plan comparison (TOU vs Standard), CSV export
- **Rates**: edit tier rates, fixed charge, riders and export credit
- **Devices**: channels, friendly names, and how to send readings

## Try it (demo data)

```bash
cp .env.example .env
docker compose up -d --build
```

Open http://localhost:8855 (change `PORT` in `.env` to use another port). Demo mode fills 45 days of simulated data (kept in a separate `demo.db`).

## Use real data

1. In `.env` set `INGEST_KEY=<long random string>` (`openssl rand -hex 24`), then `docker compose up -d --build`.
2. In the app, click the **Demo data** badge (or Devices, Data source) to switch to real data. The choice is remembered.
   Setting `DEMO_MODE=false` in `.env` before the first start does the same.
3. Set up the collector on the Raspberry Pi (below).
4. On the **Rates** page, enter your fixed monthly charge and riders from your bill.

## Collector (Raspberry Pi 2B)

```bash
cd collector
cp collector.example.json collector.json     # set "server" and "api_key"
python3 collector.py -c collector.json --check   # read once, print, send nothing
python3 collector.py -c collector.json
```

`interval_seconds` is 1 in the example, which gives the per-second Live graph. Raise it if a device can't be polled that often. Standard library only, so it runs on the Pi 2B's stock Python. Readings are held in memory and resent if the server is down.

| Source | Status |
|---|---|
| `mock` | Works. Fake data for testing the whole chain. |
| `refoss` | **Not finished.** Refoss EM16P local API details must be confirmed against Refoss's docs once the unit is on your network. |
| `tapo` | **Not finished.** Needs a library and your Tapo login. |
| `inverter` | **Not finished.** The Anenji manual does not give the RS232 protocol. Ask the seller for the protocol PDF. The Wi-Fi module shares the one RJ45 port, so it must be unplugged while the serial cable is used. |

The unfinished sources stop with a clear message rather than sending guessed values. Each file lists exactly what is needed.

## Ingest API

`POST /api/ingest` with header `X-API-Key`. Body: `{"readings": {"grid": 1250, "solar": 800}}` or
`{"samples": [{"channel": "grid", "watts": 1250, "ts": 1760000000}]}`.

| Channel | Meaning |
|---|---|
| `grid` | Net watts at the meter (+ import, − export). Required for costs. |
| `solar` | Solar production |
| `battery` | + charging, − discharging |
| `circuit:<name>` / `device:<name>` | Circuits and plugs |

Home load is derived as `grid + solar − battery`.

## Security

The dashboard has **no sign-in**, and the Rates page can change settings. Keep it on your home network and do not
forward a port to it. Only ingest is protected, by `INGEST_KEY`. If you want remote access, put it behind a VPN
or an authenticating reverse proxy.

## Development

```bash
pip install -r requirements.txt pytest httpx
python3 -m pytest -q tests
DEMO_MODE=true uvicorn app.main:create_app --factory --port 8080
```

Rates are stored in dollars per kWh and shown in cents. Defaults come from Idaho Power's Time-of-Use page
(Mon to Sat; Sundays and listed holidays are off-peak). Verify them against your own bill.
