"""
Pydantic models for the Garden Finance Order API response.
Endpoint: GET {order_api_base_url}/orders/id/{order_id}
"""
from datetime import datetime
from typing import Any
from pydantic import BaseModel, field_validator


class BitcoinTimestamps(BaseModel):
    initiate_detected_timestamp: datetime | None = None
    redeem_detected_timestamp: datetime | None = None


class AdditionalData(BaseModel):
    strategy_id: str = ""
    bitcoin_optional_recipient: str = ""
    input_token_price: float | None = None
    output_token_price: float | None = None
    sig: str = ""
    deadline: int | None = None                    # UNIX timestamp
    src_init_detection_deadline: int | None = None  # UNIX timestamp
    instant_refund_tx_bytes: str = ""
    is_blacklisted: bool = False
    integrator: str = ""
    version: str = ""
    bitcoin: BitcoinTimestamps | None = None

    model_config = {"extra": "allow"}


class SwapData(BaseModel):
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    swap_id: str
    chain: str                          # "bitcoin", "ethereum", etc.
    asset: str
    htlc_address: str
    token_address: str = ""
    initiator: str                      # solver address (executor side)
    redeemer: str                       # user address (relayer side)
    timelock: int
    filled_amount: str                  # string integer
    amount: str                         # string integer (expected)
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
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    source_swap: SwapData
    destination_swap: SwapData
    create_order: CreateOrder


class OrderApiResponse(BaseModel):
    status: str
    result: OrderResult
