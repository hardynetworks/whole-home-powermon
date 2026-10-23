"""Sources turn a device into {channel: watts}. Add a new one by writing a class
with read() and registering it here."""
from importlib import import_module

REGISTRY = {
    "mock": ("sources.mock", "MockSource"),
    "refoss": ("sources.refoss", "RefossSource"),
    "tapo": ("sources.tapo", "TapoSource"),
    "inverter": ("sources.inverter", "InverterSource"),
}


def build_source(conf):
    kind = conf["type"]
    if kind not in REGISTRY:
        raise SystemExit(f"collector: unknown source type '{kind}'. Choose from {', '.join(REGISTRY)}")
    module, cls = REGISTRY[kind]
    return getattr(import_module(module), cls)(conf)
