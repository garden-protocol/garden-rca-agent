"""
Garden RCA Agent — FastAPI entrypoint.

Endpoints:
  POST /investigate/{server_secret} — order-state-aware investigation (auth required)
  POST /explore/{server_secret}     — codebase Q&A (auth required)
  POST /study/{chain}               — study a chain's repo and generate knowledge doc
  GET  /health                      — health check
"""
import asyncio
import json as _json
import time
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from config import settings
from models.investigate import InvestigateRequest, InvestigateResponse
from models.explore import ExploreRequest, ExploreResponse
from models.investigate import AgentTokenUsage
import agents.orchestrator as orchestrator
import agents.explore_agent as explore_agent
import study.study_agent as study_agent
import jobs as job_store
from jobs import JobStatus
import discord_webhook


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rca-agent")

SUPPORTED_CHAINS = {"bitcoin", "evm", "solana", "tron", "starknet", "litecoin", "alpen"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Garden RCA Agent starting on port %d", settings.port)
    yield
    logger.info("Garden RCA Agent shutting down")


app = FastAPI(
    title="Garden RCA Agent",
    description="AI-powered Root Cause Analysis for Garden cross-chain bridge alerts.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok", "chains": list(SUPPORTED_CHAINS)}


async def _run_investigation(job_id: str, order_id: str, investigate: bool):
    """Background task — runs orchestrator.investigate and records the result."""
    await job_store.set_running(job_id)
    try:
        result = await asyncio.to_thread(orchestrator.investigate, order_id, investigate)
        await job_store.set_done(job_id, result)
        logger.info("Job %s: done order=%s", job_id, order_id)
        await discord_webhook.post_investigation(result)
    except Exception as exc:
        logger.exception("Job %s failed for order %s", job_id, order_id)
        await job_store.set_failed(job_id, f"{type(exc).__name__}: {exc}")


@app.post("/investigate/{server_secret}", status_code=202)
async def investigate_order(server_secret: str, req: InvestigateRequest):
    """
    Enqueue an investigation job. Returns 202 with a job_id; poll
    GET /jobs/{server_secret}/{job_id} for status and result.

    Made async in Batch D — some investigations exceed Cloudflare's
    100s proxy timeout; a queued job pattern is safer.

    States detected:
      - DestInitPending      — source inited, destination not yet inited
      - UserRedeemPending    — destination inited but not redeemed
      - SolverRedeemPending  — destination redeemed, source not yet redeemed

    Early returns (no LLM cost):
      - Order blacklisted
      - Filled amount outside tolerance
      - Insufficient solver liquidity
      - Source initiate past deadline
      - No user init found
      - Relayer/executor balance too low
      - HTLC already redeemed on-chain (watcher out of sync)
    """
    if server_secret != settings.server_secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    job = await job_store.create()
    logger.info("Investigate enqueued: order=%s job_id=%s", req.order_id, job.id)
    asyncio.create_task(_run_investigation(job.id, req.order_id, req.investigate))
    return {
        "job_id": job.id,
        "status": job.status.value,
        "poll_url": f"/jobs/{server_secret}/{job.id}",
    }


@app.get("/jobs/{server_secret}/{job_id}")
async def get_job(server_secret: str, job_id: str):
    """Poll a job's status. Returns 404 if unknown or expired."""
    if server_secret != settings.server_secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    job = await job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")

    payload: dict = {
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if job.status == JobStatus.DONE and job.result is not None:
        payload["result"] = job.result.model_dump(mode="json")
    elif job.status == JobStatus.FAILED:
        payload["error"] = job.error

    # Force stdlib json so control chars in strings (e.g. newlines inside
    # RCA raw_analysis) are always escaped as \\n. Some deployments pull in
    # a non-stdlib JSON encoder (orjson / ujson) via transitive deps that
    # leave raw 0x0a bytes inside string values — invalid per RFC 8259 and
    # rejected by strict clients (httpx.Response.json(), stdlib json.loads).
    body = _json.dumps(payload, ensure_ascii=False, default=str)
    return Response(content=body, media_type="application/json")


@app.post("/explore/{server_secret}")
async def explore_codebase(server_secret: str, req: ExploreRequest):
    """
    Codebase Q&A endpoint (auth required via path secret).

    Accepts a natural language question about any repo in the Gitea org.
    The agent resolves which repo to explore from the question text
    (or uses keyword/knowledge-doc matching if no repo is named explicitly),
    then searches and reads source code to answer.
    """
    if server_secret != settings.server_secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    logger.info("Explore request: %s", req.question[:120])
    start = time.monotonic()
    try:
        result = await asyncio.to_thread(explore_agent.run, req.question)
        duration = round(time.monotonic() - start, 1)

        usage = result.get("usage") or {}
        ai_cost = AgentTokenUsage(
            model=usage.get("model", ""),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
        ) if usage else None

        from datetime import datetime
        response = ExploreResponse(
            answer=result["answer"],
            repo_name=result.get("repo_name"),
            branch=result.get("branch"),
            ai_cost=ai_cost,
            generated_at=datetime.utcnow(),
            duration_seconds=duration,
        )
        logger.info(
            "Explore complete: repo=%s duration=%.1fs cost=$%.4f",
            response.repo_name or "unresolved",
            duration,
            ai_cost.cost_usd if ai_cost else 0.0,
        )
        # Force stdlib JSON encoding (see /jobs handler for the full reason).
        body = _json.dumps(response.model_dump(mode="json"), ensure_ascii=False, default=str)
        return Response(content=body, media_type="application/json")
    except Exception as exc:
        logger.exception("Explore failed for question: %s", req.question[:120])
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/study/{chain}")
async def study_chain(chain: str):
    """
    Trigger the study agent to read the chain's repo and regenerate knowledge/{chain}.md.
    Run this whenever the codebase changes significantly.
    Commit the resulting knowledge doc to git so the whole team benefits.
    """
    if chain not in SUPPORTED_CHAINS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown chain: {chain!r}. Supported: {sorted(SUPPORTED_CHAINS)}",
        )

    logger.info("Study mode triggered for chain: %s", chain)
    start = time.monotonic()
    try:
        output_path = await asyncio.to_thread(study_agent.run, chain)
        duration = round(time.monotonic() - start, 1)
        logger.info("Study complete for %s -> %s (%.1fs)", chain, output_path, duration)
        return {
            "status": "ok",
            "chain": chain,
            "knowledge_file": output_path,
            "duration_seconds": duration,
            "next_step": f"git add {output_path} && git commit -m 'chore: update {chain} knowledge doc'",
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Study failed for chain %s", chain)
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=False)
