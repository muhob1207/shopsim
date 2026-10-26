"""Service registry: service name -> module exposing ``create(ctx) -> ServiceModule``."""
from __future__ import annotations

import importlib

_MODULES = {
    "gateway": "shopsim.services.gateway",
    "products": "shopsim.services.products",
    "orders": "shopsim.services.orders",
    "inventory": "shopsim.services.inventory",
    "payments": "shopsim.services.payments",
    "notifications": "shopsim.services.notifications",
    "bank-sim": "shopsim.services.bank_sim",
}


def load(name: str):
    try:
        return importlib.import_module(_MODULES[name])
    except KeyError:
        raise SystemExit(f"unknown service '{name}'; known: {', '.join(_MODULES)}") from None
