"""Runs the small LLM used for news scoring, the nightly reflection and the Live chat.

Default: Claude Code's CLI (`claude -p --model haiku`). Any CLI that reads a prompt on stdin and prints the answer
works: set AITRADER_LLM_CMD in .env, e.g. AITRADER_LLM_CMD="codex exec -". Set AITRADER_LLM_CMD=off to turn the
LLM off; the engine then scores news with its keyword list and the Live chat answers with a short fallback line.
"""
import os
import shlex
import subprocess

from dotenv import dotenv_values

from trader import config as C


def _cmd():
    raw = (os.environ.get("AITRADER_LLM_CMD") or dotenv_values(C.ROOT / ".env").get("AITRADER_LLM_CMD")
           or "claude -p --model haiku").strip()
    return None if raw.lower() in ("off", "none", "0") else shlex.split(raw)


def haiku(prompt: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """Same shape as subprocess.run(..., capture_output=True, text=True); raises TimeoutExpired."""
    cmd = _cmd()
    if not cmd:
        return subprocess.CompletedProcess(["off"], 1, "", "LLM turned off (AITRADER_LLM_CMD=off)")
    try:
        return subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")
