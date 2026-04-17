"""
Garden RCA Discord Bot.

Slash commands:
  /investigate <order_id> — enqueues an investigation on the RCA agent.
                            Results are posted to the configured Discord
                            webhook channel by the agent itself — the bot
                            only acknowledges the enqueue.
  /explore <question>     — codebase Q&A; result posted as an embed reply.

Required env vars:
  DISCORD_BOT_TOKEN  — Discord bot token
  RCA_AGENT_URL      — Base URL of the RCA agent, e.g. https://rca.garden.finance
  SERVER_SECRET      — Same secret used by the RCA agent endpoint
"""
import json as _json
import logging
import os

import discord
import httpx
from discord import app_commands
from dotenv import load_dotenv


def _lenient_json(resp: httpx.Response) -> dict:
    """
    Parse a JSON response with strict=False so that raw control characters
    (e.g. unescaped newlines inside string values, which some server-side
    JSON encoders emit even though RFC 8259 forbids them) don't blow up the
    bot. Prefer `resp.json()` where available; fall back to `json.loads`
    with strict=False on failure.
    """
    try:
        return resp.json()
    except Exception:
        return _json.loads(resp.text, strict=False)

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


def _truncate(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


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
@app_commands.describe(
    order_id="Order ID or full Garden Finance URL",
    investigate="Run full LLM analysis even for refunded/early-return orders (default: False)",
)
async def investigate(interaction: discord.Interaction, order_id: str, investigate: bool = False):
    """
    Enqueue an investigation and acknowledge. The RCA agent posts the final
    result to the configured Discord webhook — the bot does not poll.
    """
    await interaction.response.defer(thinking=True, ephemeral=True)
    logger.info("Enqueuing investigation order=%s (investigate=%s)", order_id, investigate)

    url = f"{RCA_AGENT_URL}/investigate/{SERVER_SECRET}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(url, json={"order_id": order_id, "investigate": investigate})
            resp.raise_for_status()
            enqueue = _lenient_json(resp)
    except httpx.HTTPStatusError as exc:
        await interaction.followup.send(
            f"RCA agent returned `{exc.response.status_code}`: {exc.response.text[:500]}",
            ephemeral=True,
        )
        return
    except Exception as exc:
        logger.exception("Enqueue failed")
        await interaction.followup.send(
            f"Failed to enqueue investigation: {exc}",
            ephemeral=True,
        )
        return

    job_id = enqueue.get("job_id", "?")
    await interaction.followup.send(
        f"Investigation enqueued (`{job_id}`). Result will post to the configured webhook channel.",
        ephemeral=True,
    )


def _build_explore_embed(data: dict) -> discord.Embed:
    answer    = data.get("answer", "No answer returned.")
    repo_name = data.get("repo_name", "unknown")
    branch    = data.get("branch", "?")
    ai_cost   = data.get("ai_cost")
    duration  = data.get("duration_seconds", "?")

    embed = discord.Embed(
        title=f"Explore — {repo_name or 'cross-repo'}",
        description=_truncate(answer, 4000),
        colour=discord.Colour.blue(),
    )
    if repo_name:
        embed.add_field(name="Repo",     value=f"`{repo_name}`",  inline=True)
        embed.add_field(name="Branch",   value=f"`{branch}`",     inline=True)
    embed.add_field(name="Duration", value=f"{duration}s", inline=True)

    if ai_cost:
        cost = ai_cost.get("cost_usd", 0.0)
        model = ai_cost.get("model", "?")
        embed.add_field(name="AI Cost", value=f"${cost:.4f} ({model})", inline=True)

    embed.set_footer(text="Garden RCA Agent — Explore")
    return embed


@client.tree.command(name="explore", description="Ask a question about the codebase")
@app_commands.describe(
    question="Natural language question, e.g. 'What is the default price protection in cobi-v2?'",
)
async def explore(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)

    url = f"{RCA_AGENT_URL}/explore/{SERVER_SECRET}"
    logger.info("Explore question: %s via %s", question[:120], url)

    try:
        async with httpx.AsyncClient(timeout=300.0) as http:
            resp = await http.post(url, json={"question": question})
            resp.raise_for_status()
            data = _lenient_json(resp)
    except httpx.HTTPStatusError as exc:
        await interaction.followup.send(
            f"RCA agent returned `{exc.response.status_code}`: {exc.response.text[:500]}"
        )
        return
    except Exception as exc:
        logger.exception("Explore request failed")
        await interaction.followup.send(f"Failed to reach RCA agent: {exc}")
        return

    embed = _build_explore_embed(data)

    # If answer is too long for embed description (>4096), send as file attachment too
    answer = data.get("answer", "")
    if len(answer) > 4000:
        import io
        file = discord.File(io.BytesIO(answer.encode()), filename="explore_answer.md")
        await interaction.followup.send(embed=embed, file=file)
    else:
        await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")
    if not SERVER_SECRET:
        raise RuntimeError("SERVER_SECRET is not set")
    client.run(DISCORD_BOT_TOKEN)
