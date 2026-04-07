from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # Anthropic
    anthropic_api_key: str

    # Loki (primary)
    loki_url: str = "http://loki.internal:3100"
    loki_auth_token: str = ""

    # Grafana (fallback)
    grafana_url: str = ""
    grafana_api_key: str = ""

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

    # Spark repo paths (single executor service for now)
    repo_spark: str = "/opt/repos/spark-executor"

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
    branch_spark: str = "staging"

    # Bitcoin RPC
    bitcoin_rpc_url: str = ""
    bitcoin_rpc_user: str = ""
    bitcoin_rpc_pass: str = ""

    # EVM RPC
    evm_rpc_url: str = ""

    # Solana RPC
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"

    # Spark RPC
    spark_rpc_url: str = ""

    # Server
    port: int = 8000

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
            "spark": {
                "executor": self.branch_spark,
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
            "spark": {
                "executor": self.repo_spark,
            },
        }[chain]

    def repo_path(self, chain: str, component: str = "executor") -> str:
        """
        Returns the path for a specific component repo.
        Defaults to 'executor' for backward compatibility.
        """
        return self.repo_paths(chain)[component]


settings = Settings()
