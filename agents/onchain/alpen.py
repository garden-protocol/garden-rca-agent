"""Alpen (Bitcoin L2) on-chain query agent using Electrs-compatible REST API."""
import json
import httpx
from config import settings
from agents.onchain.base import BaseOnChainAgent


def _electrs_get(path: str, timeout: int = 15):
    """Make an HTTP GET request to the Alpen Electrs REST API."""
    url = f"{settings.alpen_electrs_url.rstrip('/')}{path}"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        # Some endpoints return plain text (e.g. /blocks/tip/height)
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            return resp.json()
        # Try JSON parse first, fall back to raw text
        try:
            return resp.json()
        except Exception:
            return resp.text
    except Exception as e:
        return {"error": str(e)}


class AlpenOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "alpen"

    @property
    def system_prompt(self) -> str:
        return """\
You are an on-chain query agent for the Alpen network (a Bitcoin L2) in the Garden bridge system.

Alpen is a UTXO-based Bitcoin L2 that uses the same Taproot/Tapscript HTLC structure as Bitcoin \
mainnet. It uses btcsuite/btcd address types and an Electrs-compatible indexer for queries.

Tool usage guide:
- BALANCE check -> use get_address_balance. Start response with BALANCE_INSUFFICIENT or BALANCE_OK.
- HTLC redeemed check -> use get_address_utxos on the HTLC address.
  Empty UTXOs = HTLC was spent (HTLC_REDEEMED).
  UTXOs present = still locked (HTLC_PENDING).
  Optionally call get_transaction on the funding or spending tx to inspect witness data.
- Transaction details -> use get_transaction to inspect inputs, outputs, and witness data.
- Current block height -> use get_tip_block_height (useful for timelock / CSV eligibility checks).
- Fee estimates -> use get_fee_estimates for current fee rate information.

Taproot HTLC witness analysis (same as Bitcoin):
- Witness with 4 elements = redeem spend (element[1] is the preimage/secret).
- Witness with 3 elements = refund spend (CSV timelock path).
- Witness with 2 elements = multisig/keypath spend.

Always start with the required keyword (BALANCE_INSUFFICIENT, BALANCE_OK, HTLC_REDEEMED, \
HTLC_PENDING) when the question asks for one. Be precise and factual.\
"""

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_address_balance",
                "description": (
                    "Get the total balance of an Alpen address in satoshis by summing all UTXOs. "
                    "Queries GET /address/{address}/utxo from the Electrs API and sums values. "
                    "USE THIS for relayer/executor balance checks (BALANCE_INSUFFICIENT / BALANCE_OK)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "description": "Alpen address (btcsuite/btcd format, e.g. P2TR/P2WPKH)",
                        },
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_address_utxos",
                "description": (
                    "Get all unspent transaction outputs (UTXOs) for an Alpen address. "
                    "Queries GET /address/{address}/utxo from the Electrs API. "
                    "USE THIS to check if a Taproot HTLC address has been spent: "
                    "if the list is empty -> HTLC was spent (HTLC_REDEEMED); "
                    "if UTXOs are present -> HTLC is still locked (HTLC_PENDING)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "description": "Alpen address to query UTXOs for",
                        },
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_transaction",
                "description": (
                    "Get full details of a transaction by txid via GET /tx/{txid}. "
                    "Returns inputs (with witness data), outputs, confirmation status, and more. "
                    "Witness with 4 elements = redeem spend (element[1] is the preimage/secret). "
                    "Witness with 3 elements = refund spend (CSV timelock path). "
                    "USE THIS to check tx confirmation status and to identify HTLC spend type."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "txid": {
                            "type": "string",
                            "description": "Transaction ID (64 hex chars)",
                        },
                    },
                    "required": ["txid"],
                },
            },
            {
                "name": "get_tip_block_height",
                "description": (
                    "Get the current tip block height via GET /blocks/tip/height. "
                    "USE THIS to check whether a CSV timelock has elapsed "
                    "(compare current height to the initiation block + timelock blocks) "
                    "or to assess how many confirmations a tx has relative to current tip."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "get_fee_estimates",
                "description": (
                    "Get current fee rate estimates via GET /fee-estimates. "
                    "Returns a map of confirmation-target (number of blocks) to fee rate (sat/vB). "
                    "USE THIS to assess network congestion and whether a tx fee was adequate."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_address_balance":
            address = tool_input["address"]
            result = _electrs_get(f"/address/{address}/utxo")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            if not isinstance(result, list):
                return f"[Unexpected response] {result}"
            total_sats = sum(utxo.get("value", 0) for utxo in result)
            return (
                f"Balance: {total_sats} satoshis, confirmed UTXOs: {len(result)}"
            )

        elif tool_name == "get_address_utxos":
            address = tool_input["address"]
            result = _electrs_get(f"/address/{address}/utxo")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            if not isinstance(result, list):
                return f"[Unexpected response] {result}"
            if not result:
                return "No UTXOs found at address — HTLC has been spent."
            total_sats = sum(utxo.get("value", 0) for utxo in result)
            slim = [
                {
                    "txid": u.get("txid"),
                    "vout": u.get("vout"),
                    "value_sats": u.get("value", 0),
                    "status": "confirmed" if u.get("status", {}).get("confirmed") else "unconfirmed",
                }
                for u in result
            ]
            return f"{len(slim)} UTXO(s) found, total {total_sats} sats. UTXOs: {slim}"

        elif tool_name == "get_transaction":
            txid = tool_input["txid"]
            result = _electrs_get(f"/tx/{txid}")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            if not isinstance(result, dict):
                return f"[Unexpected response] {result}"
            # Slim down to the fields useful for HTLC analysis
            confirmed = result.get("status", {}).get("confirmed", False)
            block_height = result.get("status", {}).get("block_height")
            slim = {
                "txid": result.get("txid"),
                "confirmed": confirmed,
                "block_height": block_height,
                "fee": result.get("fee"),
                "size": result.get("size"),
                "weight": result.get("weight"),
                "vin": [
                    {
                        "txid": v.get("txid"),
                        "vout": v.get("vout"),
                        "witness": v.get("witness", []),
                        "prevout": {
                            "value_sats": v.get("prevout", {}).get("value", 0),
                            "scriptpubkey_address": v.get("prevout", {}).get("scriptpubkey_address"),
                        } if v.get("prevout") else None,
                    }
                    for v in result.get("vin", [])
                ],
                "vout": [
                    {
                        "value_sats": v.get("value", 0),
                        "scriptpubkey_address": v.get("scriptpubkey_address"),
                    }
                    for v in result.get("vout", [])
                ],
            }
            return json.dumps(slim)

        elif tool_name == "get_tip_block_height":
            result = _electrs_get("/blocks/tip/height")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            return f"Current tip block height: {result}"

        elif tool_name == "get_fee_estimates":
            result = _electrs_get("/fee-estimates")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            return f"Fee estimates (confirmation target -> sat/vB): {result}"

        return f"[Unknown tool: {tool_name}]"
