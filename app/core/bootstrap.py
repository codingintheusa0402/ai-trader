"""First start: create the dry-run plans listed in traders.toml. Never creates a real-money plan."""
import tomllib

from trader import config as C
from .brain import PERSONAS
from . import profiles, universe

FILE = C.ROOT / "traders.toml"
REAL_TRADERS = ("vector", "blaze")


def load() -> dict:
    if not FILE.exists():
        return {}
    with open(FILE, "rb") as f:
        return tomllib.load(f)


def ensure_plans(engine):
    try:
        cfg = load()
    except Exception as e:
        print(f"traders.toml: can't read it ({e}); no plans created", flush=True)
        return
    for pid, t in cfg.items():
        if pid not in REAL_TRADERS or not t.get("enabled", True):
            continue
        if any(p.pid == pid and p.mode == "real" and not p.league for p in list(engine.plans.values())):
            continue   # it has (or had) a plan: what happens to it from here is your call, not this file's
        name = PERSONAS[pid]["name"]
        try:
            risk, horizon = int(t.get("risk", 3)), str(t.get("horizon", "1d"))
            uni, style = str(t.get("universe", "large")), str(t.get("style", "normal"))
            if risk not in profiles.RISK or horizon not in profiles.HORIZON or uni not in universe.MODES:
                raise ValueError("risk must be 1-5, horizon one of " + " ".join(profiles.HORIZON)
                                 + ", universe one of " + " ".join(universe.MODES))
            p = engine.start_plan("real", pid, str(t.get("market", "US")), risk, horizon, float(t.get("budget", 1000)),
                                  t.get("min_pct"), t.get("max_pct"), dry_run=True, uni=uni,
                                  inverse=bool(t.get("inverse", True)), style=style, monthly=bool(t.get("monthly", False)))
            p.last_tick = 0
            print(f"traders.toml: started {name} in DRY-RUN ({p.budget:,.0f} pretend {p.market})", flush=True)
        except Exception as e:
            print(f"traders.toml: {name} not started: {e}", flush=True)
