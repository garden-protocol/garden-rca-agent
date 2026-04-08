from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # Anthropic
    anthropic_api_key: str

    # Server auth — required; set SERVER_SECRET in .env
    server_secret: str

    # Loki (primary)
    loki_url: str = "http://loki.internal:3100"
    loki_auth_token: str = ""

    # Grafana (fallback)
    grafana_url: str = ""
    grafana_api_key: str = ""

    # Orders API (used by investigate pipeline)
    order_api_base_url: str = "https://api.garden.finance"
    order_api_timeout_seconds: float = 10.0

    # Bitcoin repo paths
    # Bitcoin has two watchers: cobi (order event watcher) and zmq (mempool/block watcher)
    repo_bitcoin_executor: str = "/opt/repos/bitcoin-executor"
    repo_bitcoin_watcher_cobi: str = "/opt/repos/bitcoin-watcher-cobi"
    repo_bitcoin_watcher_zmq: str = "/opt/repos/bitcoin-watcher-zmq"
    repo_bitcoin_relayer: str = "/opt/repos/bitcoin-relayer"
    # Note: Bitcoin HTLC is a static Tapscript (no repo) — stored at knowledge/bitcoin_htlc.rs

    # EVM repo paths
    repo_evm_executor: str = "/opt/repos/evm-executor"
    repo_evm_watcher: str = "/opt/repos/evm-watcher"
    repo_evm_relayer: str = "/opt/repos/evm-relayer"
    repo_evm_htlc: str = "/opt/repos/evm-htlc"

    # Solana repo paths
    repo_solana_executor: str = "/opt/repos/solana-executor"
    repo_solana_watcher: str = "/opt/repos/solana-watcher"
    repo_solana_relayer: str = "/opt/repos/solana-relayer"
    repo_solana_native_swaps: str = "/opt/repos/solana-native-swaps"
    repo_solana_spl_swaps: str = "/opt/repos/solana-spl-swaps"

    # Branch overrides per component (default: "staging")
    # Only set these when a component uses a different branch than staging
    branch_bitcoin_executor: str = "staging"
    branch_bitcoin_watcher_cobi: str = "staging"
    branch_bitcoin_watcher_zmq: str = "feat/rollout2"
    branch_bitcoin_relayer: str = "staging"
    branch_evm_executor: str = "staging"
    branch_evm_watcher: str = "staging"
    branch_evm_relayer: str = "staging"
    branch_evm_htlc: str = "staging"
    branch_solana_executor: str = "staging"
    branch_solana_watcher: str = "staging"
    branch_solana_relayer: str = "staging"
    branch_solana_native_swaps: str = "dev"
    branch_solana_spl_swaps: str = "dev"

    # Bitcoin RPC
    bitcoin_rpc_url: str = ""
    bitcoin_rpc_user: str = ""
    bitcoin_rpc_pass: str = ""

    # EVM RPC
    evm_rpc_url: str = ""

    # Solana RPC
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"

    # Server
    port: int = 8000

    # ── Investigation pipeline ────────────────────────────────────────────────

    # Max % deviation between source filled_amount and expected amount before skipping
    filled_amount_tolerance_pct: float = 5.0

    # Max % deviation between source and destination token values (price * amount) before skipping
    # If input_token_price * source_amount vs output_token_price * destination_amount diverges
    # beyond this threshold, the solver likely rejected the swap due to price fluctuation.
    price_deviation_tolerance_pct: float = 1.0

    # URL that returns all solvers' available liquidity (no auth, returns JSON list/map)
    liquidity_url: str = ""

    # Per-chain relayer wallet addresses (used in UserRedeemPending balance check)
    relayer_address_bitcoin: str = ""
    relayer_address_evm: str = ""
    relayer_address_solana: str = ""

    # Per-chain executor wallet addresses (used in SolverRedeemPending gas check)
    executor_address_bitcoin: str = ""
    executor_address_evm: str = ""
    executor_address_solana: str = ""

    # Minimum native balances before flagging as insufficient
    min_evm_gas_balance: int = 10_000_000_000_000_000    # 0.01 ETH in wei
    min_solana_gas_balance: int = 10_000_000              # 0.01 SOL in lamports
    min_bitcoin_gas_balance: int = 10_000                 # 10k satoshis

    def relayer_address(self, chain: str) -> str:
        """Return the configured relayer address for a given internal chain name."""
        return {
            "bitcoin": self.relayer_address_bitcoin,
            "evm": self.relayer_address_evm,
            "solana": self.relayer_address_solana,
        }.get(chain, "")

    def executor_address(self, chain: str) -> str:
        """Return the configured executor address for a given internal chain name."""
        return {
            "bitcoin": self.executor_address_bitcoin,
            "evm": self.executor_address_evm,
            "solana": self.executor_address_solana,
        }.get(chain, "")

    def min_gas_balance(self, chain: str) -> int:
        """Return the minimum acceptable native balance (chain-native units) for a chain."""
        return {
            "bitcoin": self.min_bitcoin_gas_balance,
            "evm": self.min_evm_gas_balance,
            "solana": self.min_solana_gas_balance,
        }.get(chain, 0)

    def repo_branches(self, chain: str) -> dict[str, str]:
        """
        Returns the branch to study for each component repo.
        Matches keys returned by repo_paths().
        """
        return {
            "bitcoin": {
                "executor": self.branch_bitcoin_executor,
                "watcher_cobi": self.branch_bitcoin_watcher_cobi,
                "watcher_zmq": self.branch_bitcoin_watcher_zmq,
                "relayer": self.branch_bitcoin_relayer,
            },
            "evm": {
                "executor": self.branch_evm_executor,
                "watcher": self.branch_evm_watcher,
                "relayer": self.branch_evm_relayer,
                "htlc": self.branch_evm_htlc,
            },
            "solana": {
                "executor": self.branch_solana_executor,
                "watcher": self.branch_solana_watcher,
                "relayer": self.branch_solana_relayer,
                "native_swaps": self.branch_solana_native_swaps,
                "spl_swaps": self.branch_solana_spl_swaps,
            },
        }[chain]

    def repo_paths(self, chain: str) -> dict[str, str]:
        """
        Returns all component repos for a chain as {component_name: path}.
        Used by study agent and specialist tools to navigate multiple repos.
        """
        return {
            "bitcoin": {
                "executor": self.repo_bitcoin_executor,
                "watcher_cobi": self.repo_bitcoin_watcher_cobi,
                "watcher_zmq": self.repo_bitcoin_watcher_zmq,
                "relayer": self.repo_bitcoin_relayer,
            },
            "evm": {
                "executor": self.repo_evm_executor,
                "watcher": self.repo_evm_watcher,
                "relayer": self.repo_evm_relayer,
                "htlc": self.repo_evm_htlc,
            },
            "solana": {
                "executor": self.repo_solana_executor,
                "watcher": self.repo_solana_watcher,
                "relayer": self.repo_solana_relayer,
                "native_swaps": self.repo_solana_native_swaps,
                "spl_swaps": self.repo_solana_spl_swaps,
            },
        }[chain]

    def repo_path(self, chain: str, component: str = "executor") -> str:
        """
        Returns the path for a specific component repo.
        Defaults to 'executor' for backward compatibility.
        """
        return self.repo_paths(chain)[component]


settings = Settings()
