from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bunny_library_id: int
    bunny_stream_api_key: str = Field(repr=False)
    bunny_cdn_hostname: str
    bunny_token_auth_key: str | None = Field(default=None, repr=False)
    openai_api_key: str = Field(repr=False)
    assemblyai_api_key: str | None = Field(default=None, repr=False)
    assemblyai_region: Literal["eu", "global"] = "eu"
    app_password: str = Field(repr=False)
    temp_root: str | None = None
    media_runtime_seconds: float = Field(default=21600, gt=0, allow_inf_nan=False)
    media_inactivity_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    media_max_workspace_bytes: int = Field(default=2_000_000_000, gt=0)

    @field_validator("assemblyai_api_key", mode="before")
    @classmethod
    def blank_assemblyai_key_disables_fast_path(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
