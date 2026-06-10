"""
Known-issue knowledge-base models.

After the orchestrator diagnoses an order, the diagnosis is distilled into a
`KnownIssue` record and persisted (see tools/known_issues.py). When a new order
comes in, its structural `KnownIssueFingerprint` is matched against the stored
records so a recurring, already-understood failure can be returned instantly
(`KnownIssueMatch`) instead of re-running the expensive LLM pipeline.
"""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


# How confident we are in a diagnosis, as a float in [0, 1].
# The specialist emits a "high"/"medium"/"low" label; map it onto this scale.
CONFIDENCE_LABEL_TO_FLOAT: dict[str, float] = {"high": 0.9, "medium": 0.6, "low": 0.3}


class KnownIssue(BaseModel):
    """A single distilled, persisted diagnosis."""

    issue_id: str                       # stable hash of the structural+cause fingerprint
    first_seen: datetime
    last_seen: datetime
    occurrence_count: int = 1

    # ── Classification (the searchable fingerprint, denormalized for storage) ──
    state: str = ""                     # SwapState.value at diagnosis time
    service: str = ""                   # "executor" | "relayer" | "watcher"
    source_chain: str = ""              # internal chain name, e.g. "evm"
    destination_chain: str = ""
    source_asset: str = ""              # API asset string, e.g. "ethereum:USDC"
    destination_asset: str = ""
    tags: list[str] = Field(default_factory=list)
    error_signature: str = ""           # normalized probable-cause text (addrs/nums masked)

    # ── Findings ───────────────────────────────────────────────────────────────
    probable_cause: str = ""            # one-line/short root cause
    findings: str = ""                  # fuller RCA detail (raw_analysis or reason)
    remediation: list[str] = Field(default_factory=list)
    severity: str = ""                  # "critical"|"high"|"medium"|"low" (from RCA)
    confidence: float = 0.0             # [0, 1]
    confidence_label: str = ""          # "high"|"medium"|"low"

    # ── Provenance / control ────────────────────────────────────────────────────
    origin: Literal["deterministic", "llm"] = "llm"
    # Deterministic early-returns depend on live state (liquidity, balances,
    # confirmations) and must be re-checked each time, so they are NOT used to
    # short-circuit. Only LLM-derived systemic findings are short-circuitable.
    short_circuitable: bool = True

    # ── Free-form context ────────────────────────────────────────────────────────
    metadata: dict = Field(default_factory=dict)


class KnownIssueFingerprint(BaseModel):
    """The structural signature used to match a (possibly new) order against the KB."""

    state: str = ""
    service: str = ""
    source_chain: str = ""
    destination_chain: str = ""
    source_asset: str = ""
    destination_asset: str = ""
    tags: list[str] = Field(default_factory=list)
    error_signature: str = ""           # usually empty at query time (cause unknown yet)


class KnownIssueMatch(BaseModel):
    """A KB hit: the stored issue plus why/how strongly it matched the query."""

    issue: KnownIssue
    score: float                        # [0, 1] weighted similarity
    matched_on: list[str] = Field(default_factory=list)  # e.g. ["chain-pair", "state"]
