from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    auth_token: str = "changeme"
    host: str = "0.0.0.0"
    port: int = 8000
    default_working_dir: str = "."
    cors_origins: list[str] = ["http://localhost:5173"]

    model_config = {"env_prefix": "OCTOPUS_", "env_file": ".env"}


settings = Settings()
