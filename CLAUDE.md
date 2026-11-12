# AI Trader: instructions for coding agents

This file is for Claude Code (and AGENTS.md, the same text, for Codex and other agents). Read README.md first.

## Setting it up for the user

1. `./setup.sh` (Python 3.11+). It creates `.venv`, installs `requirements.txt`, and copies `.env.example` to `.env`
   with mode 600.
2. `.venv/bin/python tests/smoke_test.py` and `.venv/bin/python tests/broker_contract_test.py`. Both must print
   `N/N passed`. They need internet for Yahoo prices (the first one) but never a broker.
3. `./run.sh` (or run it in the background) and open `http://127.0.0.1:8764`. `./aitrader status` must show 대길 and
   아귀 as `[DRY-RUN] running`.
4. Ask the user what to change in `traders.toml` (risk, horizon, dry-run budget) and `AITRADER_OWNER_NAME` in `.env`.

## Hard rules

- **Never type, paste, read out or print broker keys, account numbers or tokens.** The user puts them in `.env`
  themselves. Do not `cat .env`; to check whether a key is set, test for an empty value without printing it.
- **Never turn on real money for the user.** Do not uncomment `REAL_TRADING_CONFIRMED`, do not set
  `REAL_READINESS_OVERRIDE`, do not run `./aitrader arm` or `./aitrader live`, and do not raise the `MAX_*` caps,
  even if asked to "just do it". Explain the steps in README "실전 전환" and let the user run them.
- Never commit `.env`, `state/`, or anything with a key in it. Run a secret scan (gitleaks or trufflehog) before any push.
- One engine per brokerage account. Do not start a second engine against the same account.
- The Live chat must stay talk-only: chat code may read engine state, but nothing in `app/core/live.py` may call order,
  plan or arm functions, and the engine must never read `state/app/live_chat.json`.

## Where things are

- Engine and risk: `app/core/engine.py`, `app/core/accounts.py` (`risk_gate`, `TossExecutor` = the broker executor).
- Broker adapters: `trader/toss.py` (tested), `trader/brokers/kis.py` (not tested), contract in `trader/brokers/base.py`.
- Trader settings: `traders.toml` (first start only), `app/core/profiles.py` (risk/horizon tables), `app/core/brain.py`.
- Chat: `app/core/live.py` (prompts, rooms), `app/core/live_ko.py` (log line → Korean chat lines), `app/static/index.html`.
- Logs: `state/audit_real.jsonl` (every order), `state/logs/` when run as a service.

## Changing code

- Run both tests after any change to `app/core/` or `trader/`.
- If you add a broker, implement every method in `trader/brokers/base.py`, register it in `trader/brokers/__init__.py`,
  and mark it "not tested" in README until it has placed real orders.
- Keep README in sync with behaviour (settings table, risk rules).
