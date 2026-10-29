from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./relayforge.db"
    secret_key: str = "change-me"
    token_minutes: int = Field(60, ge=1)

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    llm_timeout_s: float = Field(20.0, gt=0)

    max_refund: int = Field(50000, gt=0)
    step_max_attempts: int = Field(4, ge=1)
    retry_base_delay_s: float = Field(0.5, ge=0)
    retry_max_delay_s: float = Field(30.0, ge=0)

    # a worker owns a workflow for this long, and renews the lease while it works
    lease_seconds: int = Field(60, ge=5)
    sweeper_enabled: bool = True
    sweep_interval_s: float = Field(30.0, gt=0)

    login_max_failures: int = Field(5, ge=1)
    login_window_s: int = Field(900, ge=1)

    # a public demo server: ready-made accounts, and nobody can create new companies
    demo_mode: bool = False
    signup_enabled: bool = True

    # only for demos: makes the fake payment provider fail now and then
    gateway_failure_rate: float = Field(0.0, ge=0, le=1)

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, v: str) -> str:
        # people usually paste postgres:// or postgresql://, SQLAlchemy would then look for psycopg2
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @field_validator("openai_api_key")
    @classmethod
    def _blank_key_is_none(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


settings = Settings()


def check_secret_key() -> None:
    if settings.secret_key == "change-me" or len(settings.secret_key) < 32:
        raise RuntimeError("SECRET_KEY must be set to a random string of at least 32 characters "
                           "(for example: python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
