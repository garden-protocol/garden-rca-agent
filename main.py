"""
Garden RCA Agent — FastAPI entrypoint.

Endpoints:
  POST /investigate/{server_secret} — order-state-aware investigation (auth required)
  POST /study/{chain}               — study a chain's repo and generate knowledge doc
  GET  /health                      — health check
"""
import asyncio
import time
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from config import settings
from models.investigate import InvestigateRequest, InvestigateResponse
import agents.orchestrator as orchestrator
import study.study_agent as study_agent


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rca-agent")

SUPPORTED_CHAINS = {"bitcoin", "evm", "solana"}


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


@app.post("/investigate/{server_secret}", response_model=InvestigateResponse)
async def investigate_order(server_secret: str, req: InvestigateRequest):
    """
    Order-state-aware investigation endpoint (auth required via path secret).

    Accepts a raw order ID or a full Garden Finance URL.
    Automatically classifies the swap state, runs cheap deterministic checks first,
    and escalates to the full LLM pipeline only when needed.

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

    logger.info("Investigate request: order=%s", req.order_id)
    try:
        response = await asyncio.to_thread(orchestrator.investigate, req.order_id, req.investigate)
        logger.info(
            "Investigate complete: order=%s state=%s early_return=%s duration=%.1fs",
            response.order_id,
            response.state.value,
            response.early_return,
            response.duration_seconds,
        )
        return response
    except Exception as exc:
        logger.exception("Investigation failed for order %s", req.order_id)
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
