"""
Starknet on-chain query agent using the Starknet JSON-RPC API.

Uses httpx for raw JSON-RPC calls to the Starknet node.
The HTLC contract is written in Cairo; `get_order` returns 7 Felts:
[is_fulfilled, initiator, redeemer, initiated_at, timelock, amount_low, amount_high].

Felts are hex strings (0x-prefixed).  amount = amount_high * 2^128 + amount_low.
"""
import hashlib
import json

import httpx

from config import settings
from agents.onchain.base import BaseOnChainAgent


def _rpc(method: str, params: list | dict, timeout: int = 15) -> dict:
    """Make a Starknet JSON-RPC call."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = httpx.post(settings.starknet_rpc_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return {"error": data["error"]}
        return data.get("result", {})
    except Exception as e:
        return {"error": str(e)}


def _starknet_keccak(name: str) -> str:
    """
    Compute the Starknet entry-point selector for a function name.

    sn_keccak is defined as the first 250 bits of the Keccak-256 hash of the
    ASCII function name, returned as a 0x-prefixed hex Felt.
    """
    digest = hashlib.sha3_256(name.encode("ascii")).digest()  # keccak-256
    # Starknet actually uses standard Keccak-256 (not SHA3-256).  Python's
    # hashlib may or may not expose it.  We try the proper keccak first.
    try:
        import sha3 as _sha3  # pysha3 / safe-pysha3
        k = _sha3.keccak_256(name.encode("ascii")).digest()
    except ImportError:
        try:
            k = hashlib.new("keccak_256", name.encode("ascii")).digest()
        except ValueError:
            # Last resort: use pycryptodome-style import
            from Crypto.Hash import keccak as _keccak
            k = _keccak.new(data=name.encode("ascii"), digest_bits=256).digest()

    # Mask to 250 bits (clear top 6 bits of byte 0)
    num = int.from_bytes(k, "big") & ((1 << 250) - 1)
    return hex(num)


# Pre-computed selectors for common HTLC events (sn_keccak of event name).
# These can be used as keys[0] when filtering events.
_EVENT_SELECTORS = {
    "Initiated": _starknet_keccak("Initiated"),
    "Redeemed": _starknet_keccak("Redeemed"),
    "Refunded": _starknet_keccak("Refunded"),
}

_GET_ORDER_SELECTOR = _starknet_keccak("get_order")


class StarknetOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "starknet"

    @property
    def system_prompt(self) -> str:
        return """\
You are a Starknet on-chain query agent for the Garden bridge system.

Starknet uses Cairo contracts and Felt values (0x-prefixed hex strings).

Tool usage guide:
- BALANCE check: use get_account_balance with the account address and the \
token contract address. The result is a Felt (hex). Convert to integer and \
compare against the expected amount. Start response with BALANCE_INSUFFICIENT \
or BALANCE_OK.
- HTLC redeemed check: use get_htlc_order_state with the HTLC contract \
address and order ID. The tool parses the 7-Felt response from `get_order`:
    [is_fulfilled, initiator, redeemer, initiated_at, timelock, amount_low, amount_high]
  - is_fulfilled != 0x0 -> HTLC_REDEEMED
  - is_fulfilled == 0x0 and initiated_at != 0x0 -> HTLC_PENDING
  - initiated_at == 0x0 -> not initiated
  amount = amount_high * 2^128 + amount_low (u256 split into two 128-bit Felts).
  Start response with HTLC_REDEEMED or HTLC_PENDING.
- Transaction status: use get_transaction to fetch tx details, then \
get_transaction_receipt to check execution_status. \
SUCCEEDED = success, REVERTED = failed.
- Block number: use get_block_number for the current block height.
- Event search: use get_events with contract address and optional keys filter. \
Known event selectors:
  Initiated: {initiated}
  Redeemed:  {redeemed}
  Refunded:  {refunded}

Always start with the required keyword (BALANCE_INSUFFICIENT, BALANCE_OK, \
HTLC_REDEEMED, HTLC_PENDING) when asked for one.\
""".format(
            initiated=_EVENT_SELECTORS["Initiated"],
            redeemed=_EVENT_SELECTORS["Redeemed"],
            refunded=_EVENT_SELECTORS["Refunded"],
        )

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_account_balance",
                "description": (
                    "Get the token balance of a Starknet account by calling balanceOf on "
                    "the token contract via starknet_call. Returns the balance as a Felt (hex). "
                    "USE THIS for balance checks (BALANCE_INSUFFICIENT / BALANCE_OK)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "account_address": {
                            "type": "string",
                            "description": "Starknet account address (0x-prefixed Felt)",
                        },
                        "token_contract_address": {
                            "type": "string",
                            "description": "Token contract address (0x-prefixed Felt)",
                        },
                    },
                    "required": ["account_address", "token_contract_address"],
                },
            },
            {
                "name": "get_transaction",
                "description": (
                    "Get a Starknet transaction by hash via starknet_getTransactionByHash. "
                    "Returns transaction type, sender, calldata, and other details. "
                    "USE THIS to inspect submitted transactions."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {
                            "type": "string",
                            "description": "Transaction hash (0x-prefixed hex)",
                        },
                    },
                    "required": ["tx_hash"],
                },
            },
            {
                "name": "get_transaction_receipt",
                "description": (
                    "Get the receipt of a Starknet transaction via starknet_getTransactionReceipt. "
                    "Check execution_status: SUCCEEDED = success, REVERTED = failed. "
                    "USE THIS to verify if a transaction succeeded or was reverted."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {
                            "type": "string",
                            "description": "Transaction hash (0x-prefixed hex)",
                        },
                    },
                    "required": ["tx_hash"],
                },
            },
            {
                "name": "get_htlc_order_state",
                "description": (
                    "Read HTLC order state from a Cairo contract via starknet_call to get_order. "
                    "Parses the 7-Felt response: "
                    "[is_fulfilled, initiator, redeemer, initiated_at, timelock, amount_low, amount_high]. "
                    "is_fulfilled != 0x0 -> HTLC_REDEEMED, "
                    "is_fulfilled == 0x0 and initiated_at != 0x0 -> HTLC_PENDING, "
                    "initiated_at == 0x0 -> not initiated."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contract_address": {
                            "type": "string",
                            "description": "HTLC contract address (0x-prefixed Felt)",
                        },
                        "order_id": {
                            "type": "string",
                            "description": "Order ID as a Felt (0x-prefixed hex)",
                        },
                    },
                    "required": ["contract_address", "order_id"],
                },
            },
            {
                "name": "get_block_number",
                "description": (
                    "Get the current Starknet block number via starknet_blockNumber. "
                    "USE THIS to check chain liveness or correlate timestamps."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_events",
                "description": (
                    "Fetch contract events via starknet_getEvents with optional keys filter. "
                    "USE THIS to search for HTLC Initiated/Redeemed/Refunded events. "
                    "Pass event selector as keys[0] to filter by event type."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contract_address": {
                            "type": "string",
                            "description": "Contract address to filter events from (0x-prefixed Felt)",
                        },
                        "from_block": {
                            "type": "integer",
                            "description": "Start block number (inclusive)",
                        },
                        "to_block": {
                            "type": "integer",
                            "description": "End block number (inclusive). Omit for latest.",
                        },
                        "keys": {
                            "type": "array",
                            "description": (
                                "Keys filter array. Each element is a list of possible values for "
                                "that key position. keys[0] is typically the event selector."
                            ),
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "chunk_size": {
                            "type": "integer",
                            "description": "Max events to return per request (default 100)",
                            "default": 100,
                        },
                    },
                    "required": ["contract_address", "from_block"],
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if not settings.starknet_rpc_url:
            return "[STARKNET_RPC_URL not configured]"

        try:
            if tool_name == "get_account_balance":
                return self._get_account_balance(tool_input)
            elif tool_name == "get_transaction":
                return self._get_transaction(tool_input)
            elif tool_name == "get_transaction_receipt":
                return self._get_transaction_receipt(tool_input)
            elif tool_name == "get_htlc_order_state":
                return self._get_htlc_order_state(tool_input)
            elif tool_name == "get_block_number":
                return self._get_block_number()
            elif tool_name == "get_events":
                return self._get_events(tool_input)
        except Exception as e:
            return f"[RPC error: {type(e).__name__}: {e}]"

        return f"[Unknown tool: {tool_name}]"

    # -- Tool implementations ------------------------------------------------

    def _get_account_balance(self, tool_input: dict) -> str:
        """Call balanceOf on a token contract for the given account."""
        balance_of_selector = _starknet_keccak("balanceOf")
        result = _rpc("starknet_call", {
            "request": {
                "contract_address": tool_input["token_contract_address"],
                "entry_point_selector": balance_of_selector,
                "calldata": [tool_input["account_address"]],
            },
            "block_id": "latest",
        })
        if isinstance(result, dict) and "error" in result:
            return f"[RPC error: {result['error']}]"
        # Result is a list of Felts.  For u256 balances: [low, high].
        if isinstance(result, list) and len(result) >= 2:
            low = int(result[0], 16)
            high = int(result[1], 16)
            balance = high * (2 ** 128) + low
            return (
                f"Balance for {tool_input['account_address']} on token "
                f"{tool_input['token_contract_address']}: "
                f"{balance} (raw u256, low={result[0]}, high={result[1]})"
            )
        elif isinstance(result, list) and len(result) == 1:
            balance = int(result[0], 16)
            return (
                f"Balance for {tool_input['account_address']} on token "
                f"{tool_input['token_contract_address']}: {balance} (Felt: {result[0]})"
            )
        return f"Unexpected balanceOf response: {json.dumps(result, default=str)}"

    def _get_transaction(self, tool_input: dict) -> str:
        """Fetch transaction details by hash."""
        result = _rpc("starknet_getTransactionByHash", {
            "transaction_hash": tool_input["tx_hash"],
        })
        if isinstance(result, dict) and "error" in result:
            return f"[RPC error: {result['error']}]"
        if not result:
            return "Transaction not found"
        slim = {
            "type": result.get("type"),
            "transaction_hash": result.get("transaction_hash"),
            "sender_address": result.get("sender_address"),
            "nonce": result.get("nonce"),
            "max_fee": result.get("max_fee"),
            "version": result.get("version"),
            "calldata_len": len(result.get("calldata", [])),
        }
        return json.dumps(slim, default=str)

    def _get_transaction_receipt(self, tool_input: dict) -> str:
        """Fetch transaction receipt and check execution status."""
        result = _rpc("starknet_getTransactionReceipt", {
            "transaction_hash": tool_input["tx_hash"],
        })
        if isinstance(result, dict) and "error" in result:
            return f"[RPC error: {result['error']}]"
        if not result:
            return "Receipt not found (transaction may not be confirmed yet)"
        execution_status = result.get("execution_status", "UNKNOWN")
        slim = {
            "transaction_hash": result.get("transaction_hash"),
            "execution_status": execution_status,
            "finality_status": result.get("finality_status"),
            "block_number": result.get("block_number"),
            "block_hash": result.get("block_hash"),
            "actual_fee": result.get("actual_fee"),
            "events_count": len(result.get("events", [])),
            "revert_reason": result.get("revert_reason"),
        }
        return json.dumps(slim, default=str)

    def _get_htlc_order_state(self, tool_input: dict) -> str:
        """Read HTLC order state via starknet_call to get_order."""
        result = _rpc("starknet_call", {
            "request": {
                "contract_address": tool_input["contract_address"],
                "entry_point_selector": _GET_ORDER_SELECTOR,
                "calldata": [tool_input["order_id"]],
            },
            "block_id": "latest",
        })
        if isinstance(result, dict) and "error" in result:
            return f"[RPC error: {result['error']}]"

        # Expected: 7 Felts
        # [is_fulfilled, initiator, redeemer, initiated_at, timelock, amount_low, amount_high]
        if not isinstance(result, list) or len(result) < 7:
            return (
                f"Unexpected get_order response (expected 7 Felts, got "
                f"{len(result) if isinstance(result, list) else type(result).__name__}): "
                f"{json.dumps(result, default=str)}"
            )

        is_fulfilled = int(result[0], 16)
        initiator = result[1]
        redeemer = result[2]
        initiated_at = int(result[3], 16)
        timelock = int(result[4], 16)
        amount_low = int(result[5], 16)
        amount_high = int(result[6], 16)
        amount = amount_high * (2 ** 128) + amount_low

        if initiated_at == 0:
            state = "not_initiated"
        elif is_fulfilled != 0:
            state = "redeemed"
        else:
            state = "pending"

        return (
            f"HTLC order state on Starknet: {state}. "
            f"is_fulfilled={result[0]}, initiator={initiator}, redeemer={redeemer}, "
            f"initiated_at={initiated_at}, timelock={timelock}, "
            f"amount={amount} (low={result[5]}, high={result[6]})."
        )

    def _get_block_number(self) -> str:
        """Get current block number."""
        result = _rpc("starknet_blockNumber", [])
        if isinstance(result, dict) and "error" in result:
            return f"[RPC error: {result['error']}]"
        return f"Current Starknet block number: {result}"

    def _get_events(self, tool_input: dict) -> str:
        """Fetch events from a contract with optional keys filter."""
        filter_obj: dict = {
            "address": tool_input["contract_address"],
            "from_block": {"block_number": tool_input["from_block"]},
            "chunk_size": tool_input.get("chunk_size", 100),
        }
        if tool_input.get("to_block") is not None:
            filter_obj["to_block"] = {"block_number": tool_input["to_block"]}
        else:
            filter_obj["to_block"] = "latest"
        if tool_input.get("keys"):
            filter_obj["keys"] = tool_input["keys"]

        result = _rpc("starknet_getEvents", {"filter": filter_obj})
        if isinstance(result, dict) and "error" in result:
            return f"[RPC error: {result['error']}]"

        events = result.get("events", []) if isinstance(result, dict) else []
        if not events:
            return "No events found for the given filter."

        slim = [
            {
                "block_number": ev.get("block_number"),
                "transaction_hash": ev.get("transaction_hash"),
                "keys": ev.get("keys", [])[:4],
                "data": ev.get("data", [])[:6],
            }
            for ev in events[:20]
        ]
        return json.dumps(slim, default=str)
