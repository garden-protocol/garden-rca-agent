"""
EVM on-chain query agent using web3.py.

Uses the Garden eRPC endpoint: EVM_RPC_URL should be set to
https://erpc.garden.finance/rpc — the chain name is appended per-call
(e.g. .../rpc/ethereum, .../rpc/arbitrum).

ABIs and asset metadata are fetched live from the Garden Finance API.
"""
import json
from config import settings
from agents.onchain.base import BaseOnChainAgent
from tools.garden_api import lookup_asset, get_abi

try:
    from web3 import Web3
    _W3_AVAILABLE = True
except ImportError:
    _W3_AVAILABLE = False

# Lazy per-chain web3 instances.  Keyed by chain name (e.g. "ethereum").
_W3_CACHE: dict[str, "Web3"] = {}


def _get_w3(chain: str = "ethereum") -> "Web3 | None":
    """Return (cached) Web3 instance for the given chain name."""
    if not _W3_AVAILABLE or not settings.evm_rpc_url:
        return None
    if chain not in _W3_CACHE:
        url = f"{settings.evm_rpc_url.rstrip('/')}/{chain}"
        _W3_CACHE[chain] = Web3(Web3.HTTPProvider(url))
    return _W3_CACHE[chain]


# Fallback ABIs — used if the Garden schemas API is unreachable.
# orders(bytes32) → (initiator, redeemer, initiatedAt, timelock, amount, fulfilledAt)
# Both evm:htlc and evm:htlc_erc20 share this shape.
_HTLC_ORDERS_ABI = json.dumps([{
    "name": "orders",
    "type": "function",
    "stateMutability": "view",
    "inputs": [{"name": "id", "type": "bytes32"}],
    "outputs": [
        {"name": "initiator",   "type": "address"},
        {"name": "redeemer",    "type": "address"},
        {"name": "initiatedAt", "type": "uint256"},
        {"name": "timelock",    "type": "uint256"},
        {"name": "amount",      "type": "uint256"},
        {"name": "fulfilledAt", "type": "uint256"},
    ],
}])

_ERC20_BALANCE_ABI = json.dumps([{
    "name": "balanceOf",
    "type": "function",
    "stateMutability": "view",
    "inputs":  [{"name": "account", "type": "address"}],
    "outputs": [{"name": "", "type": "uint256"}],
}])

# Chain names accepted by the eRPC endpoint
_CHAIN_DESCRIPTION = (
    "Chain name for the eRPC route. "
    "Use the Garden chain name: 'ethereum', 'arbitrum', 'base', 'citrea', "
    "'botanix', 'bnbchain', 'monad', 'hyperevm'. "
    "Extract from the question context — defaults to 'ethereum'."
)


class EVMOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "evm"

    @property
    def system_prompt(self) -> str:
        return """\
You are an EVM on-chain query agent for the Garden bridge system.
The RPC is a multi-chain eRPC endpoint — you MUST pass the correct 'chain' \
parameter to every tool call. Extract the chain name from the question context \
(e.g. 'ethereum', 'arbitrum', 'base', 'citrea').

Tool usage guide:
- BALANCE check (native EVM chains like citrea, botanix) → get_native_balance
- BALANCE check (ERC20 assets like usdt, wbtc, usdc)    → get_token_balance with asset_id
  Both tools: start response with BALANCE_INSUFFICIENT or BALANCE_OK.
- HTLC redeemed check → get_htlc_order_state. fulfilledAt != 0 → HTLC_REDEEMED. Else → HTLC_PENDING.
- Transaction status  → get_transaction, then get_transaction_receipt for revert reason.
- Nonce gap / stuck tx → get_nonce + get_pending_txs.
- Fee analysis        → get_gas_price.
- Event search        → get_logs.

Always start with the required keyword when asked for one.\
"""

    @property
    def tool_definitions(self) -> list[dict]:
        chain_param = {"chain": {"type": "string", "description": _CHAIN_DESCRIPTION}}

        return [
            {
                "name": "get_native_balance",
                "description": (
                    "Get the native token balance (ETH, cBTC, etc.) of an EVM address via eth_getBalance. "
                    "USE THIS for native-asset balance checks (BALANCE_INSUFFICIENT / BALANCE_OK). "
                    "For ERC20 tokens (USDT, WBTC, USDC) use get_token_balance."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "EVM address (0x...)"},
                        **chain_param,
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_token_balance",
                "description": (
                    "Get the ERC20 token balance of an address for a specific Garden asset. "
                    "Resolves token contract address and decimals from the Garden Finance API. "
                    "USE THIS for ERC20 balance checks. asset_id examples: 'ethereum:usdt', 'arbitrum:wbtc'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "wallet_address": {"type": "string", "description": "EVM address to check (0x...)"},
                        "asset_id":       {"type": "string", "description": "Garden asset ID, e.g. 'ethereum:usdt'"},
                        **chain_param,
                    },
                    "required": ["wallet_address", "asset_id"],
                },
            },
            {
                "name": "get_transaction",
                "description": (
                    "Get an EVM transaction by hash. blockNumber=null means pending/dropped. "
                    "USE THIS to check if an initiation tx was sent and mined."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {"type": "string", "description": "Transaction hash (0x...)"},
                        **chain_param,
                    },
                    "required": ["tx_hash"],
                },
            },
            {
                "name": "get_transaction_receipt",
                "description": (
                    "Get the receipt of a mined tx. status=1 success, status=0 reverted. "
                    "USE THIS to check success/revert reason after confirming tx is mined."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {"type": "string", "description": "Transaction hash (0x...)"},
                        **chain_param,
                    },
                    "required": ["tx_hash"],
                },
            },
            {
                "name": "get_htlc_order_state",
                "description": (
                    "Read Garden HTLC order state from on-chain contract using the official Garden ABI. "
                    "fulfilledAt != 0 → HTLC_REDEEMED. "
                    "fulfilledAt == 0 and initiatedAt != 0 → HTLC_PENDING. "
                    "initiatedAt == 0 → not initiated."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contract_address": {"type": "string", "description": "HTLC contract address (0x...)"},
                        "order_id":         {"type": "string", "description": "Order ID as hex bytes32 (0x...)"},
                        "htlc_schema": {
                            "type": "string",
                            "description": "'evm:htlc_erc20' (default, for ERC20 assets) or 'evm:htlc' (for native chains like Citrea, Botanix).",
                            "default": "evm:htlc_erc20",
                        },
                        **chain_param,
                    },
                    "required": ["contract_address", "order_id"],
                },
            },
            {
                "name": "get_logs",
                "description": (
                    "Fetch event logs from a contract. "
                    "USE THIS to search for HTLC Redeemed/Initiated events or verify on-chain activity."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contract_address": {"type": "string", "description": "Contract address (0x...)"},
                        "from_block": {"type": "integer", "description": "Start block (inclusive)"},
                        "to_block":   {"type": "integer", "description": "End block. Omit for latest."},
                        "topics": {
                            "type": "array",
                            "description": "Topic filters (hex strings). topics[0] is event signature hash.",
                            "items": {"type": "string"},
                        },
                        **chain_param,
                    },
                    "required": ["contract_address", "from_block"],
                },
            },
            {
                "name": "get_nonce",
                "description": (
                    "Get confirmed and pending nonce for an address. "
                    "Queued txs = pending - confirmed. USE THIS to diagnose nonce gaps."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "EVM address (0x...)"},
                        **chain_param,
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_pending_txs",
                "description": (
                    "Get pending transactions for an address from txpool_content. "
                    "USE THIS alongside get_nonce to find stuck transactions."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "EVM address (0x...)"},
                        **chain_param,
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_gas_price",
                "description": "Get current gas price and EIP-1559 base fee in Gwei.",
                "input_schema": {
                    "type": "object",
                    "properties": {**chain_param},
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if not _W3_AVAILABLE:
            return "[web3.py not installed]"
        if not settings.evm_rpc_url:
            return "[EVM_RPC_URL not configured]"

        chain = tool_input.get("chain", "ethereum")
        w3 = _get_w3(chain)

        try:
            if tool_name == "get_native_balance":
                addr = Web3.to_checksum_address(tool_input["address"])
                balance_wei = w3.eth.get_balance(addr)
                balance_eth = w3.from_wei(balance_wei, "ether")
                return f"Balance: {balance_wei} wei ({balance_eth:.6f} ETH) on {chain}"

            elif tool_name == "get_token_balance":
                asset = lookup_asset(tool_input["asset_id"])
                if asset is None:
                    return f"[Asset not found] '{tool_input['asset_id']}' not in Garden chain registry."
                if asset["is_native"] or not asset["token_address"]:
                    addr = Web3.to_checksum_address(tool_input["wallet_address"])
                    balance_wei = w3.eth.get_balance(addr)
                    balance_eth = w3.from_wei(balance_wei, "ether")
                    return f"Native asset on {chain}. Balance: {balance_wei} wei ({balance_eth:.6f} ETH)"
                try:
                    abi = get_abi("evm:erc20")
                except Exception:
                    abi = json.loads(_ERC20_BALANCE_ABI)
                token_contract = w3.eth.contract(
                    address=Web3.to_checksum_address(asset["token_address"]),
                    abi=abi,
                )
                raw_balance = token_contract.functions.balanceOf(
                    Web3.to_checksum_address(tool_input["wallet_address"])
                ).call()
                decimals = asset["decimals"]
                human = raw_balance / (10 ** decimals)
                return (
                    f"Token: {tool_input['asset_id']} ({asset['token_address']}) on {chain}, "
                    f"decimals: {decimals}. "
                    f"Balance: {raw_balance} raw ({human:.{decimals}f} tokens)"
                )

            elif tool_name == "get_transaction":
                tx = w3.eth.get_transaction(tool_input["tx_hash"])
                slim = {
                    "chain":        chain,
                    "hash":         tx.get("hash", b"").hex() if isinstance(tx.get("hash"), bytes) else tx.get("hash"),
                    "from":         tx.get("from"),
                    "to":           tx.get("to"),
                    "nonce":        tx.get("nonce"),
                    "value_wei":    tx.get("value"),
                    "gas":          tx.get("gas"),
                    "gasPrice":     tx.get("gasPrice"),
                    "maxFeePerGas": tx.get("maxFeePerGas"),
                    "blockNumber":  tx.get("blockNumber"),
                }
                return json.dumps(slim, default=str)

            elif tool_name == "get_transaction_receipt":
                receipt = w3.eth.get_transaction_receipt(tool_input["tx_hash"])
                if receipt is None:
                    return "Transaction not yet mined (receipt is null)"
                slim = {
                    "chain":             chain,
                    "status":            receipt.get("status"),
                    "blockNumber":       receipt.get("blockNumber"),
                    "gasUsed":           receipt.get("gasUsed"),
                    "effectiveGasPrice": receipt.get("effectiveGasPrice"),
                    "logs_count":        len(receipt.get("logs", [])),
                }
                return json.dumps(slim, default=str)

            elif tool_name == "get_htlc_order_state":
                schema_name = tool_input.get("htlc_schema", "evm:htlc_erc20")
                try:
                    abi = get_abi(schema_name)
                except Exception:
                    abi = json.loads(_HTLC_ORDERS_ABI)
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(tool_input["contract_address"]),
                    abi=abi,
                )
                order_id = tool_input["order_id"]
                if isinstance(order_id, str):
                    order_id = bytes.fromhex(order_id.removeprefix("0x").zfill(64))
                result = contract.functions.orders(order_id).call()
                # (initiator, redeemer, initiatedAt, timelock, amount, fulfilledAt)
                initiated_at = result[2]
                fulfilled_at = result[5]
                if initiated_at == 0:
                    state = "not_initiated"
                elif fulfilled_at != 0:
                    state = "redeemed"
                else:
                    state = "pending"
                return (
                    f"HTLC order state on {chain}: {state}. "
                    f"initiatedAt={initiated_at}, fulfilledAt={fulfilled_at}, "
                    f"amount={result[4]}, timelock={result[3]}."
                )

            elif tool_name == "get_logs":
                filter_params = {
                    "address":   Web3.to_checksum_address(tool_input["contract_address"]),
                    "fromBlock": tool_input["from_block"],
                    "toBlock":   tool_input.get("to_block", "latest"),
                }
                if tool_input.get("topics"):
                    filter_params["topics"] = tool_input["topics"]
                logs = w3.eth.get_logs(filter_params)
                if not logs:
                    return f"No logs found on {chain} for the given filter."
                slim = [
                    {
                        "blockNumber": log.get("blockNumber"),
                        "txHash":      log.get("transactionHash", b"").hex()
                                       if isinstance(log.get("transactionHash"), bytes)
                                       else log.get("transactionHash"),
                        "topics":      [t.hex() if isinstance(t, bytes) else t for t in log.get("topics", [])],
                        "data":        log.get("data", "")[:64],
                    }
                    for log in logs[:20]
                ]
                return json.dumps(slim, default=str)

            elif tool_name == "get_nonce":
                addr = Web3.to_checksum_address(tool_input["address"])
                confirmed = w3.eth.get_transaction_count(addr, "latest")
                pending   = w3.eth.get_transaction_count(addr, "pending")
                return (
                    f"Chain: {chain}. Confirmed nonce: {confirmed}, pending nonce: {pending}. "
                    f"Queued txs in pool: {pending - confirmed}."
                )

            elif tool_name == "get_pending_txs":
                txpool = w3.manager.request_blocking("txpool_content", [])
                pending = txpool.get("pending", {}).get(tool_input["address"].lower(), {})
                if not pending:
                    return f"No pending transactions on {chain} for this address."
                slim = {
                    nonce: {"hash": tx.get("hash"), "gasPrice": tx.get("gasPrice"), "nonce": tx.get("nonce")}
                    for nonce, tx in list(pending.items())[:10]
                }
                return json.dumps(slim, default=str)

            elif tool_name == "get_gas_price":
                gas_price = w3.eth.gas_price
                block = w3.eth.get_block("latest")
                base_fee = block.get("baseFeePerGas", "N/A")
                return (
                    f"Chain: {chain}. "
                    f"Gas price: {w3.from_wei(gas_price, 'gwei')} Gwei | "
                    f"Base fee: {w3.from_wei(base_fee, 'gwei') if base_fee != 'N/A' else 'N/A'} Gwei"
                )

        except Exception as e:
            return f"[RPC error on {chain}: {type(e).__name__}: {e}]"

        return f"[Unknown tool: {tool_name}]"
