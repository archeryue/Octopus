from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    auth_token: str = "changeme"
    host: str = "0.0.0.0"
    port: int = 8000
    default_working_dir: str = "."
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8000"]
    db_path: str = "octopus.db"

    # Dev mode (enables uvicorn reload)
    debug: bool = False

    # Cloudflare Tunnel (opt-in)
    enable_tunnel: bool = False

    # Bridge configuration (opt-in)
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: list[str] = []
    telegram_api_base_url: str = "https://api.telegram.org"

    model_config = {"env_prefix": "OCTOPUS_", "env_file": ".env"}


settings = Settings()
