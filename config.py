from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # LLM Provider — "anthropic" or "openai"
    llm_provider: str = "anthropic"

    # Anthropic
    anthropic_api_key: str = ""

    # OpenAI
    openai_api_key: str = ""

    # Model names per tier (defaults set per provider)
    # Specialist: deep reasoning agent (expensive, most capable)
    specialist_model: str = ""
    # Fast: log agent + on-chain agent (cheap, fast)
    fast_model: str = ""
    # Study: offline knowledge generation agent
    study_model: str = ""

    def get_specialist_model(self) -> str:
        if self.specialist_model:
            return self.specialist_model
        if self.llm_provider == "openai":
            return "gpt-4o"
        return "claude-opus-4-6"

    def get_fast_model(self) -> str:
        if self.fast_model:
            return self.fast_model
        if self.llm_provider == "openai":
            return "gpt-4o-mini"
        return "claude-haiku-4-5-20251001"

    def get_study_model(self) -> str:
        if self.study_model:
            return self.study_model
        if self.llm_provider == "openai":
            return "gpt-4o"
        return "claude-opus-4-6"

    # Server auth — required; set SERVER_SECRET in .env
    server_secret: str

    # Loki (primary — infrastructure logs: relayers, watchers, orderbook, etc.)
    loki_url: str = "http://loki.internal:3100"
    loki_auth_token: str = ""

    # Loki solver (executor logs — separate instance)
    loki_solver_url: str = ""
    loki_solver_auth_token: str = ""

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

    # Shared library paths (used by multiple chains)
    repo_blockchain: str = "/opt/repos/blockchain"
    repo_garden_rs: str = "/opt/repos/garden-rs"

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

    # Shared library branches
    branch_blockchain: str = "main"
    branch_garden_rs: str = "main"

    # Bitcoin RPC
    bitcoin_rpc_url: str = ""
    bitcoin_rpc_user: str = ""
    bitcoin_rpc_pass: str = ""

    # EVM RPC
    evm_rpc_url: str = ""

    # Solana RPC
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"

    # Gitea (code access in prod — replaces mounted repos)
    gitea_url: str = ""              # e.g. https://version.btcfi.wtf
    gitea_token: str = ""            # API token with read access
    gitea_org: str = "hashiraio"     # Gitea org/owner

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
                "blockchain_lib": self.branch_blockchain,
                "garden_rs": self.branch_garden_rs,
            },
            "evm": {
                "executor": self.branch_evm_executor,
                "watcher": self.branch_evm_watcher,
                "relayer": self.branch_evm_relayer,
                "htlc": self.branch_evm_htlc,
                "blockchain_lib": self.branch_blockchain,
                "garden_rs": self.branch_garden_rs,
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
        Includes shared libraries (blockchain, garden-rs) for chains that use them.
        """
        return {
            "bitcoin": {
                "executor": self.repo_bitcoin_executor,
                "watcher_cobi": self.repo_bitcoin_watcher_cobi,
                "watcher_zmq": self.repo_bitcoin_watcher_zmq,
                "relayer": self.repo_bitcoin_relayer,
                "blockchain_lib": self.repo_blockchain,
                "garden_rs": self.repo_garden_rs,
            },
            "evm": {
                "executor": self.repo_evm_executor,
                "watcher": self.repo_evm_watcher,
                "relayer": self.repo_evm_relayer,
                "htlc": self.repo_evm_htlc,
                "blockchain_lib": self.repo_blockchain,
                "garden_rs": self.repo_garden_rs,
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

    def gitea_repos(self, chain: str) -> dict[str, tuple[str, str]]:
        """
        Returns Gitea repo name and branch for each component of a chain.
        Format: {component_name: (gitea_repo_name, branch)}
        """
        return {
            "bitcoin": {
                "executor": ("cobi-v2", self.branch_bitcoin_executor),
                "watcher_cobi": ("bit-ponder", self.branch_bitcoin_watcher_cobi),
                "watcher_zmq": ("bitcoin-watcher", self.branch_bitcoin_watcher_zmq),
                "relayer": ("btc-relayer", self.branch_bitcoin_relayer),
                "blockchain_lib": ("blockchain", self.branch_blockchain),
                "garden_rs": ("garden-rs", self.branch_garden_rs),
            },
            "evm": {
                "executor": ("evm-executor", self.branch_evm_executor),
                "watcher": ("garden-evm-watcher", self.branch_evm_watcher),
                "relayer": ("evm-swapper-relay", self.branch_evm_relayer),
                "htlc": ("evm-htlc", self.branch_evm_htlc),
                "blockchain_lib": ("blockchain", self.branch_blockchain),
                "garden_rs": ("garden-rs", self.branch_garden_rs),
            },
            "solana": {
                "executor": ("solana-executor", self.branch_solana_executor),
                "watcher": ("solana-watcher", self.branch_solana_watcher),
                "relayer": ("solana-relayer", self.branch_solana_relayer),
                "native_swaps": ("solana-native-swaps", self.branch_solana_native_swaps),
                "spl_swaps": ("solana-spl-swaps", self.branch_solana_spl_swaps),
            },
        }.get(chain, {})

    def gitea_solver_repos(self) -> dict[str, tuple[str, str]]:
        """Gitea repos for the solver ecosystem (chain-agnostic)."""
        return {
            "solver_engine": ("solver-engine", "staging"),
            "solver_comms": ("solver-comms", "staging"),
            "solver_agg": ("solver-agg-v2", "staging"),
            "solver_daemon": ("solver", "stage"),
        }


settings = Settings()
