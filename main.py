"""
Garden RCA Agent — FastAPI entrypoint.

Endpoints:
  POST /rca          — trigger full root cause analysis for an alert
  POST /study/{chain} — study a chain's repo and generate knowledge doc
  GET  /health       — health check
"""
import time
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from config import settings
from models.alert import Alert
from models.report import RCAReport
import agents.orchestrator as orchestrator
import study.study_agent as study_agent


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rca-agent")

SUPPORTED_CHAINS = {"bitcoin", "evm", "solana", "spark"}


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


@app.post("/rca", response_model=RCAReport)
async def run_rca(alert: Alert):
    """
    Receive an alert and return a full RCA report.

    The pipeline runs:
      1. Log Intelligence Agent  — queries Loki
      2. On-Chain Agent          — queries chain state
      3. Chain Specialist        — synthesizes root cause from code + logs + on-chain
      4. Orchestrator            — assembles the final report
    """
    if alert.chain not in SUPPORTED_CHAINS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported chain: {alert.chain!r}. Must be one of {sorted(SUPPORTED_CHAINS)}",
        )

    logger.info(
        "RCA triggered: order=%s chain=%s service=%s network=%s alert_type=%s",
        alert.order_id,
        alert.chain,
        alert.service,
        alert.network,
        alert.alert_type,
    )

    start = time.monotonic()
    try:
        report = orchestrator.run(alert)
        logger.info(
            "RCA complete: order=%s severity=%s confidence=%s duration=%.1fs",
            alert.order_id,
            report.severity,
            report.confidence,
            report.duration_seconds,
        )
        return report
    except Exception as exc:
        logger.exception("RCA pipeline failed for order %s", alert.order_id)
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
        output_path = study_agent.run(chain)
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
