from datetime import datetime
from typing import Literal
from pydantic import BaseModel


class Alert(BaseModel):
    order_id: str
    alert_type: str  # e.g. "deadline_approaching", "missed_init", "stuck_order"
    chain: Literal["bitcoin", "evm", "solana", "spark"]
    service: Literal["executor", "watcher", "relayer"]
    network: Literal["mainnet", "testnet"]
    message: str
    timestamp: datetime
    deadline: datetime | None = None
    metadata: dict = {}
