"""
Pydantic models for the Garden Finance Order API response.
Endpoint: GET {order_api_base_url}/orders/id/{order_id}
"""
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field, field_validator


class BitcoinTimestamps(BaseModel):
    initiate_detected_timestamp: datetime | None = None
    redeem_detected_timestamp: datetime | None = None


class AdditionalData(BaseModel):
    strategy_id: str = ""
    bitcoin_optional_recipient: str = ""
    sig: str = ""
    deadline: int | None = None                    # UNIX timestamp
    src_init_detection_deadline: int | None = None  # UNIX timestamp
    instant_refund_tx_bytes: str = ""
    is_blacklisted: bool | None = False
    integrator: str = ""
    version: str = ""
    bitcoin: BitcoinTimestamps | None = None

    model_config = {"extra": "allow"}


class SwapData(BaseModel):
    created_at: datetime
    updated_at: datetime | None = None   # absent in new flattened schema
    deleted_at: datetime | None = None
    swap_id: str
    chain: str                          # "bitcoin", "ethereum", etc.
    asset: str
    htlc_address: str | None = None
    token_address: str | None = None
    initiator: str                      # solver address (executor side)
    redeemer: str                       # user address (relayer side)
    timelock: int
    filled_amount: str                  # string integer
    amount: str                         # string integer (current/expected)
    original_amount: str = ""           # new schema: originally quoted amount
    asset_price: float | None = None    # new schema: USD price at order time
    secret_hash: str
    secret: str = ""
    initiate_tx_hash: str = ""
    redeem_tx_hash: str = ""
    refund_tx_hash: str = ""
    initiate_block_number: str = "0"
    redeem_block_number: str = "0"
    refund_block_number: str = "0"
    required_confirmations: int = 0
    current_confirmations: int = 0
    initiate_timestamp: datetime | None = None
    redeem_timestamp: datetime | None = None
    refund_timestamp: datetime | None = None

    @field_validator(
        "secret", "initiate_tx_hash", "redeem_tx_hash", "refund_tx_hash",
        "initiate_block_number", "redeem_block_number", "refund_block_number",
        "htlc_address", "token_address", "original_amount",
        mode="before",
    )
    @classmethod
    def _none_to_empty(cls, v):
        """New API returns null for not-yet-set tx/block fields; treat as empty."""
        return "" if v is None else v

    @property
    def is_initiated(self) -> bool:
        return bool(self.initiate_tx_hash)

    @property
    def is_redeemed(self) -> bool:
        return bool(self.redeem_tx_hash)

    @property
    def is_refunded(self) -> bool:
        return bool(self.refund_tx_hash)

    @property
    def filled_amount_int(self) -> int:
        try:
            return int(self.filled_amount)
        except (ValueError, TypeError):
            return 0

    @property
    def amount_int(self) -> int:
        try:
            return int(self.amount)
        except (ValueError, TypeError):
            return 0


class CreateOrder(BaseModel):
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    create_id: str
    block_number: str = "0"
    source_chain: str                   # "bitcoin", "ethereum", etc.
    destination_chain: str
    source_asset: str
    destination_asset: str
    initiator_source_address: str = ""
    initiator_destination_address: str = ""
    source_amount: str
    destination_amount: str
    fee: str = ""
    nonce: str = ""
    min_destination_confirmations: int = 0
    timelock: int = 0
    secret_hash: str
    user_id: str = ""
    affiliate_fees: list[Any] = []
    solver_id: str = ""
    additional_data: AdditionalData = AdditionalData()

    @property
    def source_amount_int(self) -> int:
        try:
            return int(self.source_amount)
        except (ValueError, TypeError):
            return 0

    @property
    def destination_amount_int(self) -> int:
        try:
            return int(self.destination_amount)
        except (ValueError, TypeError):
            return 0


class OrderResult(BaseModel):
    """
    Supports both the legacy nested schema (result.create_order.*) and the new
    flattened schema (order_id/status/solver_id/deadline at result level, no
    create_order sub-object). `create_order` is exposed as a compatibility shim
    so downstream code (orchestrator) doesn't care which shape came back.
    """
    created_at: datetime
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
    source_swap: SwapData
    destination_swap: SwapData

    # New flattened top-level fields (absent in legacy schema → defaults)
    order_id: str = ""
    nonce: str = ""
    deadline: int | None = None
    version: str = ""
    status: str = ""
    solver_id: str = ""
    integrator: str = ""
    affiliate_fees: list[Any] = []
    # Present on /solver-orders (and enriched onto /v2/orders/{id}); absent → defaults
    is_blacklisted: bool = False
    strategy_id: str = ""
    user_id: str = ""
    fee: str = ""

    # Legacy nested object (absent in new schema → None)
    create_order_raw: CreateOrder | None = Field(default=None, alias="create_order")

    model_config = {"populate_by_name": True, "extra": "allow"}

    @property
    def create_order(self) -> CreateOrder:
        """Return the legacy CreateOrder, synthesizing it from flattened fields if needed."""
        if self.create_order_raw is not None:
            return self.create_order_raw
        src, dst = self.source_swap, self.destination_swap
        return CreateOrder(
            created_at=self.created_at,
            updated_at=self.updated_at or self.created_at,
            create_id=self.order_id,
            source_chain=src.chain,
            destination_chain=dst.chain,
            source_asset=src.asset,
            destination_asset=dst.asset,
            source_amount=src.original_amount or src.amount,
            destination_amount=dst.original_amount or dst.amount,
            nonce=self.nonce,
            timelock=src.timelock,
            secret_hash=src.secret_hash,
            user_id=self.user_id,
            fee=self.fee,
            solver_id=self.solver_id,
            affiliate_fees=self.affiliate_fees,
            additional_data=AdditionalData(
                strategy_id=self.strategy_id,
                deadline=self.deadline,
                is_blacklisted=self.is_blacklisted,
                integrator=self.integrator,
                version=self.version,
            ),
        )


class OrderApiResponse(BaseModel):
    status: str
    result: OrderResult
