"""
Garden RCA Discord Bot.

Slash command: /investigate <order_id>
  - Calls POST /investigate/{server_secret} on the RCA agent
  - Posts a formatted embed with the result

Required env vars:
  DISCORD_BOT_TOKEN  — Discord bot token
  RCA_AGENT_URL      — Base URL of the RCA agent, e.g. https://rca.garden.finance
  SERVER_SECRET      — Same secret used by the RCA agent endpoint
"""
import logging
import os

import discord
import httpx
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rca-bot")

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
RCA_AGENT_URL     = os.getenv("RCA_AGENT_URL", "http://localhost:8080").rstrip("/")
SERVER_SECRET     = os.getenv("SERVER_SECRET", "")
DISCORD_GUILD_ID  = os.getenv("DISCORD_GUILD_ID", "")

# Severity → Discord colour
_SEVERITY_COLOUR = {
    "critical": discord.Colour.red(),
    "high":     discord.Colour.orange(),
    "medium":   discord.Colour.yellow(),
    "low":      discord.Colour.green(),
}

# SwapState → human label
_STATE_LABEL = {
    "DestInitPending":      "Dest Init Pending",
    "UserRedeemPending":    "User Redeem Pending",
    "SolverRedeemPending":  "Solver Redeem Pending",
    "UserNotInited":        "User Not Inited",
    "Unknown":              "Unknown",
}


def _truncate(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _format_cost(ai_cost: dict | None) -> str:
    """Return a compact cost string for the embed footer / field."""
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
        if agent:
            parts.append(f"{label} ${agent['cost_usd']:.4f}")
    breakdown = "  |  ".join(parts) if parts else ""
    return f"**${total:.4f}**" + (f"  ({breakdown})" if breakdown else "")


def _build_early_return_embed(data: dict) -> discord.Embed:
    state = _STATE_LABEL.get(data.get("state", ""), data.get("state", "?"))
    src   = data.get("source_chain", "?")
    dst   = data.get("destination_chain", "?")
    reason = data.get("reason", "No reason provided.")

    embed = discord.Embed(
        title=f"🔍 Investigation — {state}",
        description=_truncate(reason, 2000),
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="Order ID", value=f"`{data.get('order_id', '?')}`", inline=False)
    embed.add_field(name="Route",    value=f"{src} → {dst}",               inline=True)
    embed.add_field(name="Duration", value=f"{data.get('duration_seconds', '?')}s", inline=True)
    embed.set_footer(text="Garden RCA Agent  •  no AI cost (early return)")
    return embed


def _build_rca_embed(data: dict) -> discord.Embed:
    report  = data.get("rca_report") or {}
    state   = _STATE_LABEL.get(data.get("state", ""), data.get("state", "?"))
    src     = data.get("source_chain", "?")
    dst     = data.get("destination_chain", "?")

    severity   = report.get("severity", "medium")
    confidence = report.get("confidence", "low")
    root_cause = report.get("root_cause", "Unknown")
    actions    = report.get("suggested_actions", [])
    components = report.get("affected_components", [])

    colour = _SEVERITY_COLOUR.get(severity, discord.Colour.light_grey())

    embed = discord.Embed(
        title=f"🔍 RCA — {state}  |  {severity.upper()} / {confidence} confidence",
        description=_truncate(root_cause, 2000),
        colour=colour,
    )
    embed.add_field(name="Order ID", value=f"`{data.get('order_id', '?')}`", inline=False)
    embed.add_field(name="Route",    value=f"{src} → {dst}", inline=True)
    embed.add_field(name="Duration", value=f"{data.get('duration_seconds', '?')}s", inline=True)
    embed.add_field(name="AI Cost",  value=_format_cost(data.get("ai_cost")),        inline=True)

    if components:
        embed.add_field(
            name="Affected Components",
            value=_truncate("\n".join(f"• {c}" for c in components), 512),
            inline=False,
        )

    if actions:
        embed.add_field(
            name="Suggested Actions",
            value=_truncate("\n".join(f"{i+1}. {a}" for i, a in enumerate(actions)), 1024),
            inline=False,
        )

    log_lines = report.get("log_evidence", [])
    if log_lines:
        embed.add_field(
            name="Log Evidence",
            value=_truncate("\n".join(f"`{l}`" for l in log_lines[:5]), 1024),
            inline=False,
        )

    embed.set_footer(text="Garden RCA Agent")
    return embed


class RCABot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %s (instant).", DISCORD_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally (may take up to 1 hour).")

    async def on_ready(self):
        logger.info("Logged in as %s (id=%s)", self.user, self.user.id)


client = RCABot()


@client.tree.command(name="investigate", description="Run RCA on a Garden order ID")
@app_commands.describe(order_id="Order ID or full Garden Finance URL")
async def investigate(interaction: discord.Interaction, order_id: str):
    # Acknowledge immediately — investigation can take 30-60s
    await interaction.response.defer(thinking=True)

    url = f"{RCA_AGENT_URL}/investigate/{SERVER_SECRET}"
    logger.info("Investigating order %s via %s", order_id, url)

    try:
        async with httpx.AsyncClient(timeout=120.0) as http:
            resp = await http.post(url, json={"order_id": order_id})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        await interaction.followup.send(
            f"❌ RCA agent returned `{exc.response.status_code}`: {exc.response.text[:500]}"
        )
        return
    except Exception as exc:
        logger.exception("RCA request failed")
        await interaction.followup.send(f"❌ Failed to reach RCA agent: {exc}")
        return

    if data.get("early_return"):
        embed = _build_early_return_embed(data)
    else:
        embed = _build_rca_embed(data)

    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")
    if not SERVER_SECRET:
        raise RuntimeError("SERVER_SECRET is not set")
    client.run(DISCORD_BOT_TOKEN)
