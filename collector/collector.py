#!/usr/bin/env python3
"""Collector for the Home Energy Dashboard. Runs on the Raspberry Pi.

Reads every configured source on an interval, then posts the readings to the
dashboard server. If the server is unreachable the readings are held in memory
and sent later, so short outages leave no gaps. Standard library only.

    python3 collector.py -c collector.json            # run forever
    python3 collector.py -c collector.json --check    # read once, print, do not send
"""
import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

from sources import build_source

MAX_BUFFER = 20000      # readings kept while the server is down
BATCH = 1000            # readings per POST (server allows 5000)
log = logging.getLogger("collector")


def load_config(path):
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("interval_seconds", 10)
    cfg["api_key"] = os.environ.get("INGEST_KEY") or cfg.get("api_key", "")
    if not cfg.get("sources"):
        sys.exit("collector: no sources configured")
    return cfg


def post(server, key, samples):
    body = json.dumps({"samples": [{"ts": t, "channel": c, "watts": w} for t, c, w in samples]}).encode()
    req = urllib.request.Request(
        server.rstrip("/") + "/api/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


class Backoff:
    """Skip a failing source for a growing number of seconds (max 5 minutes)."""
    def __init__(self):
        self.fails = 0
        self.until = 0.0

    def ok(self):
        self.fails, self.until = 0, 0.0

    def fail(self):
        self.fails += 1
        self.until = time.time() + min(300, 5 * 2 ** min(self.fails, 6))

    def waiting(self):
        return time.time() < self.until


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="collector.json")
    ap.add_argument("--check", action="store_true", help="read each source once, print, and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    sources = [(s.get("name") or s["type"], build_source(s), Backoff()) for s in cfg["sources"]]
    buffer = []

    while True:
        started = time.time()
        now = int(started)
        for name, src, backoff in sources:
            if backoff.waiting() and not args.check:
                continue
            try:
                for channel, watts in src.read().items():
                    buffer.append((now, channel, round(float(watts), 1)))
                backoff.ok()
            except NotImplementedError as exc:
                sys.exit(f"collector: source '{name}' is not finished yet: {exc}")
            except Exception as exc:  # one broken device must not stop the others
                backoff.fail()
                log.warning("source %s failed (%s: %s); retrying in %ds", name, type(exc).__name__, exc,
                            max(0, int(backoff.until - time.time())))

        if args.check:
            for t, c, w in buffer:
                print(f"{c:28s} {w:10.1f} W")
            return

        buffer = buffer[-MAX_BUFFER:]
        while buffer:
            chunk = buffer[:BATCH]
            try:
                post(cfg["server"], cfg["api_key"], chunk)
                del buffer[:len(chunk)]
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:200]
                if exc.code in (400, 413, 422):
                    log.error("server rejected %d readings (%s), dropping them: %s", len(chunk), exc.code, detail)
                    del buffer[:len(chunk)]
                else:
                    log.error("server said %s: %s", exc.code, detail)
                    break
            except Exception as exc:
                log.warning("cannot reach server (%s); holding %d readings", exc, len(buffer))
                break
        time.sleep(max(0.5, cfg["interval_seconds"] - (time.time() - started)))


if __name__ == "__main__":
    main()
