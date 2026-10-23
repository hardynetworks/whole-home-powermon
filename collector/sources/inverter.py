"""Anenji ANJ-4.2KW-LV-W hybrid inverter over its RS232 port.

STATUS: NOT IMPLEMENTED. The manual does not describe the serial protocol or pin-out.

What is known: the unit has one RJ45 jack that the Wi-Fi module also uses, so the
serial cable and the Wi-Fi module cannot be connected at the same time. SolarAssistant
lists this family as the "Sumry" model, which suggests a Voltronic-like command set,
but that is a lead, not a fact for your exact unit.

To finish:
  1. Ask the seller for the "communication protocol" PDF for the ANJ-4.2KW-LV-W.
  2. Confirm the RJ45 pin-out, baud rate and the command that returns live status.
  3. Fill in _query() and parse() below. pip install pyserial on the Pi.
  4. Test with the Wi-Fi module unplugged. Never hot-plug the cable while the inverter is on.

Channels to return:
  solar    total PV watts
  battery  watts, positive while charging, negative while discharging
  load     optional; used only for cross-checking (the server derives load itself)
Do NOT return 'grid' from here if the Refoss already measures the utility meter.
"""


class InverterSource:
    def __init__(self, conf):
        self.conf = conf   # e.g. {"port": "/dev/ttyUSB0", "baud": 2400}

    def _query(self):
        raise NotImplementedError("Inverter protocol still needs to be filled in (see sources/inverter.py)")

    def read(self):
        return self.parse(self._query())

    def parse(self, raw):
        raise NotImplementedError
