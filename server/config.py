from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    auth_token: str = "changeme"
    host: str = "0.0.0.0"
    port: int = 8000
    default_working_dir: str = "."
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8000"]
    db_path: str = "octopus.db"
    # User-uploaded attachments live here, one subdir per session_id.
    # `~` is expanded at use time (not config load time) so tests that
    # override $HOME via monkeypatch see the override.
    attachments_dir: str = "~/.octopus/attachments"
    # Per-session spill directory for prompts too large to deliver as
    # positional argv (the kernel's MAX_ARG_STRLEN ceiling is ~128 KB).
    # When a prompt exceeds the threshold, Octopus writes it to a file
    # under this root and sends the backend a small pointer message
    # instructing the model to Read the file. See server/large_prompts.py.
    large_prompts_dir: str = "~/.octopus/large-prompts"

    # Dev mode (enables uvicorn reload)
    debug: bool = False

    # Cloudflare Tunnel (opt-in)
    enable_tunnel: bool = False

    # Bridge configuration (opt-in)
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: list[str] = []
    telegram_api_base_url: str = "https://api.telegram.org"

    # If an AskUserQuestion goes unanswered for this long, the server
    # synthesizes an "act autonomously" reply so the session doesn't
    # wedge forever (matters most for bridge/scheduled-task sessions
    # where no human will ever see the prompt). 0 disables auto-answer.
    ask_user_question_timeout_seconds: int = 1800

    model_config = {"env_prefix": "OCTOPUS_", "env_file": ".env"}


settings = Settings()
