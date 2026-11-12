"""Broker adapters. Pick one with BROKER in .env:

    BROKER=toss   토스증권 Open API        TESTED with real money (the default)
    BROKER=kis    한국투자증권 KIS Open API  NOT TESTED: written from the public API docs, never run against a real account

Each adapter implements trader/brokers/base.py. To add another broker, copy kis.py, convert its API into the shapes
documented in base.py, and register it in ADAPTERS below.
"""
import os

from dotenv import dotenv_values

from trader import config as C

ADAPTERS = {
    "toss": ("trader.toss", "TossClient"),
    "kis": ("trader.brokers.kis", "KisClient"),
}


def name() -> str:
    return (os.environ.get("BROKER") or dotenv_values(C.ROOT / ".env").get("BROKER") or "toss").strip().lower()


def make_client():
    """The configured broker client. Raises if the broker is unknown or its keys are missing."""
    import importlib
    n = name()
    if n not in ADAPTERS:
        raise ValueError(f"unknown BROKER={n!r} (choose: {', '.join(ADAPTERS)})")
    mod, cls = ADAPTERS[n]
    return getattr(importlib.import_module(mod), cls)()
