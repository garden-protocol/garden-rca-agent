"""
Garden RCA Slack Bot.

Slash commands:
  /investigate <order_id> [--investigate]
      Enqueues an investigation on the RCA agent. Results are posted to the
      configured Slack webhook channel by the agent itself — the bot only
      acknowledges the enqueue.

  /explore <question>
      Codebase Q&A; result posted back to the channel.

Runs in Socket Mode (no public URL required). Requires:
  SLACK_BOT_TOKEN   — xoxb-... (Bot User OAuth Token)
  SLACK_APP_TOKEN   — xapp-... (App-Level Token with connections:write scope)
  RCA_AGENT_URL     — Base URL of the RCA agent, e.g. https://rca.garden.finance
  SERVER_SECRET     — Same secret used by the RCA agent endpoint

Slack app setup:
  1. Create app at https://api.slack.com/apps
  2. Enable Socket Mode → generate App-Level Token with connections:write
  3. Add Bot Token Scopes: commands, chat:write
  4. Create slash commands /investigate and /explore (any Request URL — unused in Socket Mode)
  5. Install to workspace → copy Bot User OAuth Token
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rca-slack-bot")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
RCA_AGENT_URL   = os.getenv("RCA_AGENT_URL", "http://localhost:8080").rstrip("/")
SERVER_SECRET   = os.getenv("SERVER_SECRET", "")

app = AsyncApp(token=SLACK_BOT_TOKEN)


def _truncate(text: str, limit: int = 2900) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _lenient_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return _json.loads(resp.text, strict=False)


def _parse_investigate_args(text: str) -> tuple[str, bool]:
    """
    Parse command text into (order_id, force_investigate).

    Accepted formats:
      <order_id>
      <order_id> --investigate
      <order_id> investigate:true
    """
    parts = text.strip().split()
    if not parts:
        return "", False
    order_id = parts[0]
    force = any(p in ("--investigate", "investigate:true", "--force") for p in parts[1:])
    return order_id, force


# ── /investigate ──────────────────────────────────────────────────────────────

@app.command("/investigate")
async def handle_investigate(ack, respond, command):
    """Enqueue a Garden order investigation and acknowledge."""
    await ack()

    text = (command.get("text") or "").strip()
    order_id, force = _parse_investigate_args(text)

    if not order_id:
        await respond(
            text="Usage: `/investigate <order_id> [--investigate]`",
            response_type="ephemeral",
        )
        return

    logger.info("Enqueuing investigation order=%s force=%s", order_id, force)

    url = f"{RCA_AGENT_URL}/investigate/{SERVER_SECRET}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(url, json={"order_id": order_id, "investigate": force})
            resp.raise_for_status()
            enqueue = _lenient_json(resp)
    except httpx.HTTPStatusError as exc:
        await respond(
            text=f":x: RCA agent returned `{exc.response.status_code}`: {exc.response.text[:400]}",
            response_type="ephemeral",
        )
        return
    except Exception as exc:
        logger.exception("Enqueue failed for order=%s", order_id)
        await respond(
            text=f":x: Failed to enqueue investigation: {exc}",
            response_type="ephemeral",
        )
        return

    job_id = enqueue.get("job_id", "?")
    await respond(
        text=f":mag: Investigation enqueued for `{order_id}` (job `{job_id}`). "
             f"Result will post to the configured webhook channel.",
        response_type="ephemeral",
    )


# ── /explore ──────────────────────────────────────────────────────────────────

def _build_explore_blocks(data: dict) -> list[dict[str, Any]]:
    answer    = data.get("answer", "No answer returned.")
    repo_name = data.get("repo_name", "unknown")
    branch    = data.get("branch", "?")
    duration  = data.get("duration_seconds", "?")
    ai_cost   = data.get("ai_cost")

    header = f":books: *Explore — {repo_name or 'cross-repo'}*"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(answer)}},
    ]

    meta_fields: list[dict] = []
    if repo_name:
        meta_fields.append({"type": "mrkdwn", "text": f"*Repo*\n`{repo_name}`"})
        meta_fields.append({"type": "mrkdwn", "text": f"*Branch*\n`{branch}`"})
    meta_fields.append({"type": "mrkdwn", "text": f"*Duration*\n{duration}s"})
    if ai_cost:
        cost  = ai_cost.get("cost_usd", 0.0)
        model = ai_cost.get("model", "?")
        meta_fields.append({"type": "mrkdwn", "text": f"*AI Cost*\n${cost:.4f} ({model})"})

    if meta_fields:
        blocks.append({"type": "section", "fields": meta_fields[:10]})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Garden RCA Agent — Explore"}],
    })
    return blocks


@app.command("/explore")
async def handle_explore(ack, respond, command):
    """Answer a codebase question and post the result back to the channel."""
    await ack()

    question = (command.get("text") or "").strip()
    if not question:
        await respond(
            text="Usage: `/explore <question>`  e.g. `/explore What is the default price protection in cobi-v2?`",
            response_type="ephemeral",
        )
        return

    await respond(
        text=f":hourglass: Exploring: _{_truncate(question, 200)}_  — this may take a minute…",
        response_type="in_channel",
    )

    url = f"{RCA_AGENT_URL}/explore/{SERVER_SECRET}"
    logger.info("Explore question: %s via %s", question[:120], url)

    try:
        async with httpx.AsyncClient(timeout=300.0) as http:
            resp = await http.post(url, json={"question": question})
            resp.raise_for_status()
            data = _lenient_json(resp)
    except httpx.HTTPStatusError as exc:
        await respond(
            text=f":x: RCA agent returned `{exc.response.status_code}`: {exc.response.text[:400]}",
        )
        return
    except Exception as exc:
        logger.exception("Explore request failed")
        await respond(text=f":x: Failed to reach RCA agent: {exc}")
        return

    blocks = _build_explore_blocks(data)

    # If the answer is very long, also attach it as a snippet
    answer = data.get("answer", "")
    if len(answer) > 2900:
        await respond(
            blocks=blocks,
            text=f"Explore result for: {question[:100]}",
        )
        # Post the full answer as a code snippet via files.upload fallback
        # (respond() doesn't support file upload; log a warning instead)
        logger.info(
            "Explore answer for order=%s was %d chars — truncated in Slack; full answer logged",
            question[:60],
            len(answer),
        )
    else:
        await respond(
            blocks=blocks,
            text=f"Explore result for: {question[:100]}",
        )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is not set")
    if not SLACK_APP_TOKEN:
        raise RuntimeError("SLACK_APP_TOKEN is not set")
    if not SERVER_SECRET:
        raise RuntimeError("SERVER_SECRET is not set")

    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    logger.info("Starting Garden RCA Slack bot (Socket Mode)…")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
