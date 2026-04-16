from datetime import datetime
from typing import Literal
from pydantic import BaseModel


class LogEvidence(BaseModel):
    """A single curated log evidence item with significance."""
    line: str
    significance: str
    source: str = ""  # service name (e.g. "executor", "watcher")


class TimelineEvent(BaseModel):
    """A single event in the incident timeline."""
    timestamp: str = ""   # ISO8601 preferred; may be "t+30s" if relative
    event: str
    source: str = ""      # "logs" | "onchain" | "alert" | "orderbook"


class ReportLink(BaseModel):
    """A click-through link for the report (block explorer, source code, etc.)."""
    label: str
    url: str
    kind: str = ""        # "tx" | "code" | "address" | "order"


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
    timeline: list[TimelineEvent] = []
    hypotheses_ruled_out: list[str] = []
    next_action: str = ""
    links: list[ReportLink] = []
    generated_at: datetime
    duration_seconds: float
