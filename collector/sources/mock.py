"""Fake readings so you can test the whole chain (collector -> server -> dashboard)
without any hardware. Produces grid, solar and a few circuits."""
import math
import random
import time


class MockSource:
    def __init__(self, conf):
        self.rng = random.Random(1)

    def read(self):
        t = time.time()
        hour = (t % 86400) / 3600.0 - 7          # rough local hour, good enough for a test
        sun = max(0.0, math.sin(math.pi * (hour - 6) / 13)) if 6 <= hour <= 19 else 0.0
        solar = 2400 * sun
        servers = 1000 + self.rng.gauss(0, 15)
        other = 300 + self.rng.gauss(0, 40)
        hvac = 1800 if int(t // 900) % 3 == 0 else 80
        load = servers + other + hvac
        return {
            "grid": load - solar, "solar": solar,
            "circuit:servers": servers, "circuit:hvac": hvac, "circuit:other": other,
        }
