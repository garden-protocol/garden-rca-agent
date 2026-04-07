from datetime import datetime
from typing import Literal
from pydantic import BaseModel


class RCAReport(BaseModel):
    order_id: str
    chain: str
    service: str
    network: str
    root_cause: str
    affected_components: list[str]  # e.g. ["executor/init.go:L142"]
    log_evidence: list[str]         # key log lines
    onchain_evidence: dict | None = None
    suggested_actions: list[str]
    severity: Literal["critical", "high", "medium", "low"]
    confidence: Literal["high", "medium", "low"]
    raw_analysis: str               # full markdown report from specialist
    generated_at: datetime
    duration_seconds: float
