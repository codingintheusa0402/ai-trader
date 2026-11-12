"""Command line for a running engine (it talks to http://127.0.0.1:$AITRADER_PORT).

    ./aitrader status                     traders, mode, money, holdings
    ./aitrader live 대길 [--budget 150]    switch a trader from dry-run to REAL money (all safety checks apply)
    ./aitrader dry 대길                     back to dry-run (only when it holds no real shares)
    ./aitrader pause|resume|stop 대길       pause buying / resume / sell everything and stop
    ./aitrader update 대길 risk=3 budget=800 horizon=1d ...
    ./aitrader arm                         type-to-confirm arming of real trading (asks you to type REAL MONEY)
    ./aitrader disarm                      no new real orders
    ./aitrader kill [--sell]               kill switch: cancel open orders (and sell everything with --sell)
"""
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import dotenv_values

from trader import config as C


def base():
    port = os.environ.get("AITRADER_PORT") or dotenv_values(C.ROOT / ".env").get("AITRADER_PORT") or "8764"
    try:
        port = (C.STATE_DIR / "app" / "port").read_text().strip() or port
    except OSError:
        pass
    return f"http://127.0.0.1:{port}"


def call(path, body=None):
    req = urllib.request.Request(base() + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", "x-aitrader": "1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("detail")
        except Exception:
            msg = e.reason
        sys.exit(f"refused: {msg}")
    except urllib.error.URLError:
        sys.exit(f"engine not running at {base()} (start it with ./run.sh)")


def plan_of(ov, who):
    for p in ov["plans"]:
        if p["mode"] == "real" and who in (p["player"], p["pid"], p["id"]) and p["status"] not in ("finished", "stopped"):
            return p
    sys.exit(f"no running plan for {who}")


def status():
    ov = call("/api/overview")
    r = ov["real"]
    print(f"engine ok: {ov['engine_ok']}   US market open: {ov['open'].get('US')}   "
          f"real trading: {'ARMED' if r['armed'] else 'not armed'}{' (KILLED)' if r['killed'] else ''}, "
          f".env unlock: {'yes' if r['env_unlocked'] else 'no'}")
    for p in ov["plans"]:
        if p["mode"] != "real":
            continue
        mode = "DRY-RUN" if p["dry_run"] else "REAL MONEY"
        hold = ", ".join(f"{x['symbol']}×{x['qty']:g}" for x in p["positions"]) or "-"
        print(f"  {p['player']:<3} [{mode}] {p['status']:<8} budget {p['budget']:,.2f} {p['ccy']}  "
              f"value {p['net']:,.2f} ({p['ret'] * 100:+.2f}%)  risk {p['risk']} {p['horizon']}  holding {hold}")


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__); return
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        return status()
    if cmd == "arm":
        phrase = input("Real money will be used. Type REAL MONEY to arm: ")
        print(call("/api/real/arm", {"phrase": phrase})); return
    if cmd == "disarm":
        print(call("/api/real/disarm", {})); return
    if cmd == "kill":
        print(call("/api/real/kill", {"flatten": "--sell" in rest})); return
    if not rest:
        sys.exit(f"which trader? e.g. ./aitrader {cmd} 대길")
    p = plan_of(call("/api/overview"), rest[0])
    if cmd in ("pause", "resume", "stop"):
        call(f"/api/plan/{p['id']}/{cmd}", {}); return status()
    if cmd in ("live", "dry"):
        body = {"dry_run": cmd == "dry"}
        if "--budget" in rest:
            body["budget"] = float(rest[rest.index("--budget") + 1])
        if cmd == "live":
            ans = input(f"{p['player']} will trade REAL money (budget {body.get('budget', p['budget']):,.2f} {p['ccy']}). "
                        f"Type {p['player']} to confirm: ")
            if ans.strip() != p["player"]:
                sys.exit("cancelled")
        call(f"/api/plan/{p['id']}/update", body); return status()
    if cmd == "update":
        body = {}
        for kv in rest[1:]:
            k, _, v = kv.partition("=")
            body[k] = (v.lower() == "true") if v.lower() in ("true", "false") else (
                float(v) if k in ("budget", "min_pct", "max_pct") else int(v) if k == "risk" else v)
        call(f"/api/plan/{p['id']}/update", body); return status()
    sys.exit(f"unknown command {cmd!r}; ./aitrader help")


if __name__ == "__main__":
    main(sys.argv[1:])
