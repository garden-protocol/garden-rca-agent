"""
Models for the /investigate endpoint — order-state-aware RCA entry point.
"""
from datetime import datetime
from enum import Enum
from pydantic import BaseModel

from models.report import RCAReport


class SwapState(str, Enum):
    DEST_INIT_PENDING = "DestInitPending"
    USER_REDEEM_PENDING = "UserRedeemPending"
    SOLVER_REDEEM_PENDING = "SolverRedeemPending"
    UNKNOWN = "Unknown"


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
    generated_at: datetime
    duration_seconds: float
