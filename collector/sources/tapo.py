"""Tapo P115 smart plugs (per-device power).

STATUS: NOT IMPLEMENTED. Tapo plugs talk through TP-Link's own protocol, which needs
your Tapo account email and password. Community libraries exist (python-kasa is the
one most often used for Tapo devices), but this has not been tested with a P115 here.

To finish: pip install the library on the Pi, log in per device in _read_one(), and
return the current power in watts.

Config example:
  {"type": "tapo", "email": "you@example.com", "password": "...",
   "devices": {"192.168.1.60": "desk", "192.168.1.61": "freezer"}}
Each device becomes channel device:<name>.

Keep the account password out of anything you share. Use a dedicated Tapo login if you can.
"""


class TapoSource:
    def __init__(self, conf):
        self.conf = conf

    def _read_one(self, host):
        raise NotImplementedError("Tapo login and power read still need to be filled in (see sources/tapo.py)")

    def read(self):
        return {f"device:{name}": self._read_one(host) for host, name in self.conf.get("devices", {}).items()}
