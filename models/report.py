from datetime import datetime
from typing import Literal
from pydantic import BaseModel


class LogEvidence(BaseModel):
    """A single curated log evidence item with significance."""
    line: str
    significance: str
    source: str = ""  # service name (e.g. "executor", "watcher")


class RCAReport(BaseModel):
    order_id: str
    chain: str
    service: str
    network: str
    root_cause: str
    affected_components: list[str]          # e.g. ["executor/init.go:L142"]
    investigation_summary: str = ""         # what the bot checked and found
    key_log_evidence: list[LogEvidence] = []  # LLM-curated evidence with significance
    onchain_evidence: dict | None = None
    remediation_actions: list[str]          # only human-actionable remediation steps
    severity: Literal["critical", "high", "medium", "low"]
    confidence: Literal["high", "medium", "low"]
    raw_analysis: str                       # full markdown report from specialist
    generated_at: datetime
    duration_seconds: float
