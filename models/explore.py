"""
Models for the /explore endpoint — codebase Q&A.
"""
from datetime import datetime
from pydantic import BaseModel

from models.investigate import AgentTokenUsage


class ExploreRequest(BaseModel):
    """
    Natural language question about the codebase.
    Examples:
      - "What is the default price protection in cobi-v2?"
      - "How does the evm-executor handle nonce management?"
      - "Where is the HTLC timeout configured in solana-native-swaps?"
    """
    question: str


class ExploreResponse(BaseModel):
    answer: str
    repo_name: str | None = None      # resolved repo (None if unresolved)
    branch: str | None = None
    ai_cost: AgentTokenUsage | None = None
    generated_at: datetime
    duration_seconds: float
