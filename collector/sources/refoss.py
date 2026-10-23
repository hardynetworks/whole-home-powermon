"""Refoss EM16P (whole-home and circuit monitor).

STATUS: NOT IMPLEMENTED. Written before the hardware arrived, on purpose, so no
guessed API details end up in your setup.

What is known (from Refoss's published material): the EM16P has a local HTTP
JSON-RPC 2.0 interface, MQTT support and a local web page. What is NOT verified:
the exact method names, response fields and whether the local API needs a token.

To finish this file once the unit is on your network:
  1. Read https://docs.refoss.net/open-api (and the EM16P local API page).
  2. Fetch one raw reading with curl and save it, so you can see the real field names.
  3. Fill in _fetch() and the mapping below.

Config example (collector.json):
  {"type": "refoss", "host": "192.168.1.50",
   "grid": {"mains": ["ch1", "ch2"], "sign": 1},
   "circuits": {"ch3": "hvac", "ch4": "water_heater"}}

Channel rules the dashboard expects:
  grid     net watts at the meter, positive when importing. Sum the two legs of a 240 V
           main. If current clamps read backwards, flip "sign" to -1.
  circuit:<name>  one per monitored breaker.
"""


class RefossSource:
    def __init__(self, conf):
        self.conf = conf

    def _fetch(self):
        # TODO: HTTP request to http://<host>/... using the method documented by Refoss.
        raise NotImplementedError("Refoss local API calls still need to be filled in (see sources/refoss.py)")

    def read(self):
        raw = self._fetch()
        # TODO: turn `raw` into watts per channel using self.conf["grid"] and self.conf["circuits"].
        return raw
