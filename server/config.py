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
    # Per-credential CODEX_HOME root (codex-backend.md §7, option B). Each
    # in-app Codex login gets `<codex_home_dir>/<credential_id>/`, which holds
    # the `auth.json` Codex writes + manages. `~` expanded at use time.
    codex_home_dir: str = "~/.octopus/codex"

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

    # Connectors (connectors.md §7). The public base URL is what connector
    # OAuth redirect URIs are built against; behind a tunnel it must be set
    # to the stable public host. Unset → computed as http://127.0.0.1:{port}.
    # Per-kind client credentials gate availability in the catalog: a kind is
    # only installable once both its id and secret are present.
    public_base_url: str | None = None
    gmail_oauth_client_id: str | None = None
    gmail_oauth_client_secret: str | None = None
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None

    model_config = {"env_prefix": "OCTOPUS_", "env_file": ".env"}

    @property
    def resolved_public_base_url(self) -> str:
        """Base URL for OAuth redirect URIs — explicit config or localhost."""
        return self.public_base_url or f"http://127.0.0.1:{self.port}"


settings = Settings()
