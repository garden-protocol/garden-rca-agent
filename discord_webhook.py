"""
Discord webhook delivery for investigation results.

Builds Discord-compatible embed payloads (dicts) from InvestigateResponse
and POSTs them to the configured webhook URL. Used by the /investigate
pipeline to push results directly to a Discord channel without a bot.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings
from models.investigate import InvestigateResponse


logger = logging.getLogger("rca-agent.webhook")

# Severity -> embed colour (Discord uses decimal RGB ints)
_SEVERITY_COLOUR = {
    "critical": 0xED4245,   # red
    "high":     0xFAA61A,   # orange
    "medium":   0xFEE75C,   # yellow
    "low":      0x57F287,   # green
}
_EARLY_RETURN_COLOUR = 0x5865F2  # blurple
_DEFAULT_COLOUR      = 0x99AAB5  # light grey

# SwapState -> human label
_STATE_LABEL = {
    "DestInitPending":      "Dest Init Pending",
    "UserRedeemPending":    "User Redeem Pending",
    "SolverRedeemPending":  "Solver Redeem Pending",
    "UserNotInited":        "User Not Inited",
    "Refunded":             "Refunded",
    "Unknown":              "Unknown",
}


def _truncate(text: str, limit: int = 1000) -> str:
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
    return f"**${total:.4f}**" + (f"  ({breakdown})" if breakdown else "")


def _build_early_return_embed(data: dict) -> dict:
    state = _STATE_LABEL.get(data.get("state", ""), data.get("state", "?"))
    src = data.get("source_chain", "?")
    dst = data.get("destination_chain", "?")
    reason = data.get("reason") or "No reason provided."

    return {
        "title": _truncate(f"Investigation — {state}", 256),
        "description": _truncate(reason, 2000),
        "color": _EARLY_RETURN_COLOUR,
        "fields": [
            {"name": "Order ID", "value": f"`{data.get('order_id', '?')}`", "inline": False},
            {"name": "Route",    "value": f"{src} → {dst}", "inline": True},
            {"name": "Duration", "value": f"{data.get('duration_seconds', '?')}s", "inline": True},
        ],
        "footer": {"text": "Garden RCA Agent  •  no AI cost (early return)"},
    }


def _build_rca_embed(data: dict) -> dict:
    report = data.get("rca_report") or {}
    state = _STATE_LABEL.get(data.get("state", ""), data.get("state", "?"))
    src = data.get("source_chain", "?")
    dst = data.get("destination_chain", "?")

    severity      = report.get("severity", "medium")
    confidence    = report.get("confidence", "low")
    root_cause    = report.get("root_cause", "Unknown")
    actions       = report.get("remediation_actions", []) or []
    components    = report.get("affected_components", []) or []
    investigation = report.get("investigation_summary", "")
    timeline      = report.get("timeline", []) or []
    ruled_out     = report.get("hypotheses_ruled_out", []) or []
    next_action   = report.get("next_action", "")
    links         = report.get("links", []) or []
    evidence      = report.get("key_log_evidence", []) or []

    fields: list[dict[str, Any]] = [
        {"name": "Order ID", "value": f"`{data.get('order_id', '?')}`", "inline": False},
        {"name": "Route",    "value": f"{src} → {dst}", "inline": True},
        {"name": "Duration", "value": f"{data.get('duration_seconds', '?')}s", "inline": True},
        {"name": "AI Cost",  "value": _format_cost(data.get("ai_cost")), "inline": True},
    ]

    if next_action:
        fields.append({
            "name": "▶ What to do now",
            "value": _truncate(next_action, 1024),
            "inline": False,
        })

    if investigation:
        fields.append({
            "name": "Investigation Summary",
            "value": _truncate(investigation, 1024),
            "inline": False,
        })

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
            fields.append({
                "name": "Timeline",
                "value": _truncate("\n".join(lines), 1024),
                "inline": False,
            })

    if actions:
        fields.append({
            "name": "Remediation Actions",
            "value": _truncate(
                "\n".join(f"{i+1}. {a}" for i, a in enumerate(actions[:5])),
                1024,
            ),
            "inline": False,
        })

    if ruled_out:
        fields.append({
            "name": "Ruled Out",
            "value": _truncate("\n".join(f"• {h}" for h in ruled_out[:3]), 1024),
            "inline": False,
        })

    if evidence:
        evidence_lines: list[str] = []
        for ev in evidence[:5]:
            if not isinstance(ev, dict):
                continue
            line = ev.get("line", "")
            sig = ev.get("significance", "")
            if not line:
                continue
            display_line = line[:120] + "..." if len(line) > 120 else line
            entry = f"`{display_line}`"
            if sig:
                entry += f"\n  _{sig}_"
            evidence_lines.append(entry)
        if evidence_lines:
            fields.append({
                "name": "Key Evidence",
                "value": _truncate("\n".join(evidence_lines), 1024),
                "inline": False,
            })

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
            rendered.append(f"• [{c}]({url})" if url else f"• {c}")
        fields.append({
            "name": "Affected Components",
            "value": _truncate("\n".join(rendered), 512),
            "inline": False,
        })

    tx_links = [l for l in links if isinstance(l, dict) and l.get("kind") == "tx"]
    order_link = next(
        (l for l in links if isinstance(l, dict) and l.get("kind") == "order"),
        None,
    )
    link_lines: list[str] = []
    if order_link:
        link_lines.append(f"[{order_link.get('label', 'order')}]({order_link.get('url', '')})")
    for l in tx_links[:6]:
        link_lines.append(f"[{l.get('label', 'tx')}]({l.get('url', '')})")
    if link_lines:
        fields.append({
            "name": "Links",
            "value": _truncate("  •  ".join(link_lines), 1024),
            "inline": False,
        })

    title = f"RCA — {state}  |  severity: {severity.upper()}  ·  confidence: {confidence.upper()}"
    return {
        "title": _truncate(title, 256),
        "description": _truncate(root_cause, 2000),
        "color": _SEVERITY_COLOUR.get(severity, _DEFAULT_COLOUR),
        "fields": fields,
        "footer": {"text": "Garden RCA Agent"},
    }


def build_embed(response: InvestigateResponse) -> dict:
    """Build a Discord embed dict from an InvestigateResponse."""
    data = response.model_dump(mode="json")
    if data.get("early_return"):
        return _build_early_return_embed(data)
    return _build_rca_embed(data)


async def post_investigation(response: InvestigateResponse) -> None:
    """
    POST the investigation result to the configured Discord webhook.

    No-op when `discord_webhook_url` is not set. Network / HTTP errors are
    logged but never raised — webhook delivery must not fail the job.
    """
    url = settings.discord_webhook_url
    if not url:
        return

    embed = build_embed(response)
    payload = {"embeds": [embed]}

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        logger.info("Webhook posted for order=%s", response.order_id)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Webhook rejected for order=%s: %s %s",
            response.order_id,
            exc.response.status_code,
            exc.response.text[:300],
        )
    except Exception as exc:
        logger.warning("Webhook POST failed for order=%s: %s", response.order_id, exc)
