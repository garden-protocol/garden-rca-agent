from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Anthropic
    anthropic_api_key: str

    # Loki (primary)
    loki_url: str = "http://loki.internal:3100"
    loki_auth_token: str = ""

    # Grafana (fallback)
    grafana_url: str = ""
    grafana_api_key: str = ""

    # Repo paths
    repo_bitcoin: str = "/opt/repos/bitcoin-executor"
    repo_evm: str = "/opt/repos/evm-executor"
    repo_solana: str = "/opt/repos/solana-executor"
    repo_spark: str = "/opt/repos/spark-executor"

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

    def repo_path(self, chain: str) -> str:
        return {
            "bitcoin": self.repo_bitcoin,
            "evm": self.repo_evm,
            "solana": self.repo_solana,
            "spark": self.repo_spark,
        }[chain]


settings = Settings()
