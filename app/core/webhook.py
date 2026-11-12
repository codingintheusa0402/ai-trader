"""Where alerts and the daily report go: ALERT_WEBHOOK_URL in .env (optional).

Any webhook that accepts a JSON body with "text" (Slack, Google Chat, Mattermost) or "content" (Discord) works;
both keys are sent. Leave it empty and the engine only prints alerts to its log.
"""
import os

from dotenv import dotenv_values

from trader import config as C


def url() -> str:
    return (os.environ.get("ALERT_WEBHOOK_URL") or dotenv_values(C.ROOT / ".env").get("ALERT_WEBHOOK_URL") or "").strip()
