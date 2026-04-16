"""Litecoin on-chain query agent using Electrs-compatible REST API."""
import httpx
from config import settings
from agents.onchain.base import BaseOnChainAgent


def _electrs_get(path: str, timeout: int = 15):
    """Make a GET request to the Litecoin Electrs REST API."""
    url = f"{settings.litecoin_electrs_url}{path}"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            return resp.json()
        # Some endpoints (e.g. /blocks/tip/height) return plain text
        return resp.text
    except Exception as e:
        return {"error": str(e)}


class LitecoinOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "litecoin"

    @property
    def system_prompt(self) -> str:
        return """\
You are a Litecoin on-chain query agent for the Garden bridge system.

Tool usage guide:
- BALANCE check → use get_address_balance. Sum all UTXOs in litoshis (1 LTC = 100,000,000 litoshis). \
Start response with BALANCE_INSUFFICIENT or BALANCE_OK.
- HTLC redeemed check → use get_address_utxos on the HTLC address. \
No UTXOs (empty list) = HTLC was spent (HTLC_REDEEMED). \
UTXOs present = still locked (HTLC_PENDING). \
Optionally call get_transaction on the spending tx to confirm spend type via witness analysis.
- Transaction status → use get_transaction (check the "status" field for confirmation info).
- Fee congestion → use get_fee_estimates for current fee rate estimates.
- Current block height → use get_tip_block_height.

Litecoin HTLC details (Taproot/Tapscript — same as Bitcoin):
- Litecoin HTLC addresses use Bech32m format: ltc1p...
- The HTLC Taproot script tree has three leaf scripts:
  1. Redeem leaf: OP_SHA256 <secret_hash> OP_EQUALVERIFY <redeemer_pubkey> OP_CHECKSIG
     Witness: [signature, secret/preimage, redeem_script, control_block]
     → 4 witness elements. Element[1] is the secret/preimage.
  2. Refund leaf: <timelock> OP_CHECKSEQUENCEVERIFY OP_DROP <initiator_pubkey> OP_CHECKSIG
     Witness: [signature, refund_script, control_block]
     → 3 witness elements. CSV timelock path.
  3. MultiSig leaf: <pubkey1> OP_CHECKSIGVERIFY <pubkey2> OP_CHECKSIG (2-of-2 instant refund / SACP)
     Witness: [sig2, sig1, multisig_script, control_block]
     → 4 witness elements, but NO secret/preimage — both are signatures.

UTXO-based HTLC detection pattern:
- Address has UTXOs → funds still locked → HTLC_PENDING
- Address has no UTXOs → funds have been spent → HTLC_REDEEMED (or refunded — check spending tx witness)

Always start with the required keyword (BALANCE_INSUFFICIENT, BALANCE_OK, HTLC_REDEEMED, \
HTLC_PENDING) when the question asks for one. Be precise and factual.\
"""

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_address_balance",
                "description": (
                    "Get the total confirmed LTC balance of a Litecoin address in litoshis "
                    "(1 LTC = 100,000,000 litoshis). Fetches all UTXOs via Electrs and sums their values. "
                    "USE THIS for relayer/executor balance checks (BALANCE_INSUFFICIENT / BALANCE_OK)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "description": "Litecoin address (e.g. ltc1p... for Taproot/Bech32m)",
                        },
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_address_utxos",
                "description": (
                    "Get all unspent transaction outputs (UTXOs) for a Litecoin address via Electrs. "
                    "USE THIS to check if a Taproot HTLC address has been spent: "
                    "if the list is empty → HTLC was spent (redeemed or refunded); "
                    "if UTXOs are present → HTLC is still locked and pending."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "description": "Litecoin address to check for UTXOs",
                        },
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_transaction",
                "description": (
                    "Get full details of a Litecoin transaction by txid via Electrs. "
                    "Returns status (confirmed/unconfirmed), inputs with witness data, and outputs. "
                    "Witness with 4 elements where element[1] is a preimage = redeem spend. "
                    "Witness with 3 elements = refund spend (CSV timelock path). "
                    "Witness with 4 elements where both element[0] and element[1] are signatures = "
                    "instant refund (multisig/SACP). "
                    "USE THIS to check tx confirmation status and to identify HTLC spend type."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "txid": {
                            "type": "string",
                            "description": "Litecoin transaction ID (64 hex chars)",
                        },
                    },
                    "required": ["txid"],
                },
            },
            {
                "name": "get_tip_block_height",
                "description": (
                    "Get the current Litecoin block height via Electrs. "
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
                    "Get current Litecoin network fee rate estimates (in sat/vbyte) "
                    "for various confirmation targets via Electrs. "
                    "USE THIS to assess network congestion and whether a tx fee was too low."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_address_balance":
            result = _electrs_get(f"/address/{tool_input['address']}/utxo")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            if not isinstance(result, list):
                return f"[Unexpected response] {result}"
            total_litoshis = sum(u.get("value", 0) for u in result)
            total_ltc = total_litoshis / 1e8
            return (
                f"Balance: {total_litoshis} litoshis ({total_ltc:.8f} LTC), "
                f"confirmed UTXOs: {len(result)}"
            )

        elif tool_name == "get_address_utxos":
            result = _electrs_get(f"/address/{tool_input['address']}/utxo")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            if not isinstance(result, list):
                return f"[Unexpected response] {result}"
            if not result:
                return "No UTXOs found at address — HTLC has been spent."
            total_litoshis = sum(u.get("value", 0) for u in result)
            slim = [
                {
                    "txid": u.get("txid"),
                    "vout": u.get("vout"),
                    "value_litoshis": u.get("value"),
                    "status": "confirmed" if u.get("status", {}).get("confirmed") else "unconfirmed",
                }
                for u in result
            ]
            return f"{len(slim)} UTXO(s) found, total {total_litoshis} litoshis. UTXOs: {slim}"

        elif tool_name == "get_transaction":
            result = _electrs_get(f"/tx/{tool_input['txid']}")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            if not isinstance(result, dict):
                return f"[Unexpected response] {result}"
            status = result.get("status", {})
            slim = {
                "txid": result.get("txid"),
                "confirmed": status.get("confirmed", False),
                "block_height": status.get("block_height"),
                "block_time": status.get("block_time"),
                "size": result.get("size"),
                "weight": result.get("weight"),
                "fee": result.get("fee"),
                "vin": [
                    {
                        "txid": v.get("txid"),
                        "vout": v.get("vout"),
                        "witness": v.get("witness", []),
                        "prevout": {
                            "value_litoshis": v.get("prevout", {}).get("value"),
                            "address": v.get("prevout", {}).get("scriptpubkey_address"),
                        } if v.get("prevout") else None,
                    }
                    for v in result.get("vin", [])
                ],
                "vout": [
                    {
                        "value_litoshis": v.get("value"),
                        "address": v.get("scriptpubkey_address"),
                    }
                    for v in result.get("vout", [])
                ],
            }
            return str(slim)

        elif tool_name == "get_tip_block_height":
            result = _electrs_get("/blocks/tip/height")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            return f"Current block height: {result}"

        elif tool_name == "get_fee_estimates":
            result = _electrs_get("/fee-estimates")
            if isinstance(result, dict) and "error" in result:
                return f"[Electrs error] {result['error']}"
            if not isinstance(result, dict):
                return f"[Unexpected response] {result}"
            # Pick representative targets: 1-block (fast), 6-block (normal), 144-block (slow)
            fast = result.get("1", "unavailable")
            normal = result.get("6", "unavailable")
            slow = result.get("144", result.get("504", "unavailable"))
            return (
                f"Fee estimates (sat/vbyte): "
                f"fast(1-block)={fast}, "
                f"normal(6-block)={normal}, "
                f"slow(144-block)={slow}"
            )

        return f"[Unknown tool: {tool_name}]"
