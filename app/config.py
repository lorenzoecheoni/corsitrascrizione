from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bunny_library_id: int
    bunny_stream_api_key: str = Field(repr=False)
    bunny_cdn_hostname: str
    bunny_token_auth_key: str | None = Field(default=None, repr=False)
    openai_api_key: str = Field(repr=False)
    app_password: str = Field(repr=False)
    temp_root: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
