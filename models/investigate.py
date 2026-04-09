"""
Models for the /investigate endpoint — order-state-aware RCA entry point.
"""
from datetime import datetime
from enum import Enum
from pydantic import BaseModel

from models.report import RCAReport


class SwapState(str, Enum):
    USER_NOT_INITED = "UserNotInited"
    DEST_INIT_PENDING = "DestInitPending"
    USER_REDEEM_PENDING = "UserRedeemPending"
    SOLVER_REDEEM_PENDING = "SolverRedeemPending"
    UNKNOWN = "Unknown"


class AgentTokenUsage(BaseModel):
    """Token usage and cost for a single agent."""
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


class AICost(BaseModel):
    """Aggregated token usage and cost across all agents that ran."""
    log_agent: AgentTokenUsage | None = None
    onchain_agent: AgentTokenUsage | None = None
    specialist: AgentTokenUsage | None = None
    total_cost_usd: float = 0.0


class InvestigateRequest(BaseModel):
    """
    Accepts either a raw order ID or a full Garden Finance URL.
    Examples:
      - "7ac2352264079d8579993da6b1788038f9078dfdca16e79f14b7298cfb2afc78"
      - "https://api.garden.finance/v2/orders/7ac235..."
    """
    order_id: str


class InvestigateResponse(BaseModel):
    order_id: str
    state: SwapState
    source_chain: str = ""
    destination_chain: str = ""
    early_return: bool
    reason: str | None = None       # set when early_return is True
    rca_report: RCAReport | None = None  # set when early_return is False and LLM pipeline ran
    ai_cost: AICost | None = None   # token usage and cost for all LLM calls
    generated_at: datetime
    duration_seconds: float
