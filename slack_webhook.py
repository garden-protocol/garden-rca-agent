"""
Slack webhook delivery for investigation results.

Builds Slack Block Kit payloads from InvestigateResponse and POSTs them
to the configured Incoming Webhook URL. Mirrors discord_webhook.py in
structure and call convention.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings
from models.investigate import InvestigateResponse


logger = logging.getLogger("rca-agent.slack")

_SEVERITY_COLOUR = {
    "critical": "#ED4245",
    "high":     "#FAA61A",
    "medium":   "#FEE75C",
    "low":      "#57F287",
}
_EARLY_RETURN_COLOUR = "#5865F2"
_DEFAULT_COLOUR      = "#99AAB5"

_STATE_LABEL = {
    "DestInitPending":      "Dest Init Pending",
    "UserRedeemPending":    "User Redeem Pending",
    "SolverRedeemPending":  "Solver Redeem Pending",
    "UserNotInited":        "User Not Inited",
    "Refunded":             "Refunded",
    "Unknown":              "Unknown",
}


def _truncate(text: str, limit: int = 2000) -> str:
    if not isinstance(text, str):
        text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _format_cost(ai_cost: dict | None) -> str:
    if not ai_cost:
        return "—"
    total = ai_cost.get("total_cost_usd", 0.0)
    parts = []
    for key, label in (
        ("log_agent", "log"),
        ("onchain_agent", "on-chain"),
        ("specialist", "specialist"),
    ):
        agent = ai_cost.get(key)
        cost = agent["cost_usd"] if agent else 0.0
        parts.append(f"{label} ${cost:.4f}")
    breakdown = "  |  ".join(parts)
    return f"*${total:.4f}*" + (f"  ({breakdown})" if breakdown else "")


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(text, 3000)}}


def _fields_block(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Section block with up to 10 mrkdwn fields (Slack max is 10)."""
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*{k}*\n{v}"}
            for k, v in pairs[:10]
        ],
    }


def _divider() -> dict[str, Any]:
    return {"type": "divider"}


def _build_early_return_blocks(data: dict) -> tuple[str, list[dict]]:
    state = _STATE_LABEL.get(data.get("state", ""), data.get("state", "?"))
    src   = data.get("source_chain", "?")
    dst   = data.get("destination_chain", "?")
    reason = data.get("reason") or "No reason provided."
    order_id = data.get("order_id", "?")
    duration = data.get("duration_seconds", "?")

    blocks: list[dict] = [
        _text_block(f":mag: *Investigation — {state}*\n{reason}"),
        _divider(),
        _fields_block([
            ("Order ID",  f"`{order_id}`"),
            ("Route",     f"{src} → {dst}"),
            ("Duration",  f"{duration}s"),
            ("AI Cost",   "—  _(early return)_"),
        ]),
        _text_block("_Garden RCA Agent  •  no AI cost (early return)_"),
    ]
    return _EARLY_RETURN_COLOUR, blocks


def _build_rca_blocks(data: dict) -> tuple[str, list[dict]]:
    report     = data.get("rca_report") or {}
    state      = _STATE_LABEL.get(data.get("state", ""), data.get("state", "?"))
    src        = data.get("source_chain", "?")
    dst        = data.get("destination_chain", "?")
    order_id   = data.get("order_id", "?")
    duration   = data.get("duration_seconds", "?")

    severity   = report.get("severity", "medium")
    confidence = report.get("confidence", "low")
    root_cause = report.get("root_cause", "Unknown")
    actions    = report.get("remediation_actions", []) or []
    components = report.get("affected_components", []) or []
    investigation = report.get("investigation_summary", "")
    timeline   = report.get("timeline", []) or []
    ruled_out  = report.get("hypotheses_ruled_out", []) or []
    next_action = report.get("next_action", "")
    links      = report.get("links", []) or []
    evidence   = report.get("key_log_evidence", []) or []

    severity_icon = {"critical": ":red_circle:", "high": ":orange_circle:",
                     "medium": ":yellow_circle:", "low": ":large_green_circle:"}.get(severity, ":white_circle:")

    blocks: list[dict] = [
        _text_block(
            f"{severity_icon} *RCA — {state}*  |  severity: `{severity.upper()}`  ·  confidence: `{confidence.upper()}`\n"
            f"{_truncate(root_cause, 500)}"
        ),
        _divider(),
        _fields_block([
            ("Order ID",  f"`{order_id}`"),
            ("Route",     f"{src} → {dst}"),
            ("Duration",  f"{duration}s"),
            ("AI Cost",   _format_cost(data.get("ai_cost"))),
        ]),
    ]

    if next_action:
        blocks.append(_text_block(f"*▶ What to do now*\n{_truncate(next_action, 1000)}"))

    if investigation:
        blocks.append(_text_block(f"*Investigation Summary*\n{_truncate(investigation, 1000)}"))

    if timeline:
        lines: list[str] = []
        for entry in timeline[:5]:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp", "")
            ev = entry.get("event", "")
            if not ev:
                continue
            lines.append(f"`{ts}` — {ev}" if ts else f"• {ev}")
        if lines:
            blocks.append(_text_block(f"*Timeline*\n" + _truncate("\n".join(lines), 1000)))

    if actions:
        blocks.append(_text_block(
            "*Remediation Actions*\n" +
            _truncate("\n".join(f"{i+1}. {a}" for i, a in enumerate(actions[:5])), 1000)
        ))

    if ruled_out:
        blocks.append(_text_block(
            "*Ruled Out*\n" +
            _truncate("\n".join(f"• {h}" for h in ruled_out[:3]), 1000)
        ))

    if evidence:
        ev_lines: list[str] = []
        for ev in evidence[:5]:
            if not isinstance(ev, dict):
                continue
            line = ev.get("line", "")
            sig  = ev.get("significance", "")
            if not line:
                continue
            display = line[:120] + "..." if len(line) > 120 else line
            entry = f"`{display}`"
            if sig:
                entry += f"\n  _{sig}_"
            ev_lines.append(entry)
        if ev_lines:
            blocks.append(_text_block("*Key Evidence*\n" + _truncate("\n".join(ev_lines), 1000)))

    if components:
        code_links = {
            l.get("label", ""): l.get("url", "")
            for l in links if isinstance(l, dict) and l.get("kind") == "code"
        }
        rendered: list[str] = []
        for c in components:
            url = code_links.get(c)
            if not url:
                for label, u in code_links.items():
                    if c in label or label in c:
                        url = u
                        break
            rendered.append(f"• <{url}|{c}>" if url else f"• {c}")
        blocks.append(_text_block("*Affected Components*\n" + _truncate("\n".join(rendered), 500)))

    tx_links    = [l for l in links if isinstance(l, dict) and l.get("kind") == "tx"]
    order_link  = next((l for l in links if isinstance(l, dict) and l.get("kind") == "order"), None)
    link_parts: list[str] = []
    if order_link:
        link_parts.append(f"<{order_link.get('url', '')}|{order_link.get('label', 'order')}>")
    for l in tx_links[:6]:
        link_parts.append(f"<{l.get('url', '')}|{l.get('label', 'tx')}>")
    if link_parts:
        blocks.append(_text_block("*Links*\n" + "  •  ".join(link_parts)))

    blocks.append(_text_block("_Garden RCA Agent_"))

    colour = _SEVERITY_COLOUR.get(severity, _DEFAULT_COLOUR)
    return colour, blocks


def build_payload(response: InvestigateResponse) -> dict[str, Any]:
    """Build a Slack Incoming Webhook payload from an InvestigateResponse."""
    data = response.model_dump(mode="json")
    if data.get("early_return"):
        colour, blocks = _build_early_return_blocks(data)
    else:
        colour, blocks = _build_rca_blocks(data)

    # Slack attachments carry the sidebar colour; blocks live inside the attachment.
    return {
        "attachments": [
            {
                "color":  colour,
                "blocks": blocks,
            }
        ]
    }


async def post_investigation(response: InvestigateResponse) -> None:
    """
    POST the investigation result to the configured Slack webhook.

    No-op when `slack_webhook_url` is not set. Network / HTTP errors are
    logged but never raised — webhook delivery must not fail the job.
    """
    url = settings.slack_webhook_url
    if not url:
        return

    payload = build_payload(response)

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        logger.info("Slack webhook posted for order=%s", response.order_id)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Slack webhook rejected for order=%s: %s %s",
            response.order_id,
            exc.response.status_code,
            exc.response.text[:300],
        )
    except Exception as exc:
        logger.warning("Slack webhook POST failed for order=%s: %s", response.order_id, exc)
