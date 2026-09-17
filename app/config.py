from collections.abc import Sequence
import re
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_material_allowed_hosts(value: str | Sequence[str]) -> tuple[str, ...]:
    """Normalize exact DNS hostnames; never interpret URLs, ports or wildcards."""
    entries = value.split(",") if isinstance(value, str) else value
    hosts = []
    for entry in entries:
        host = entry.strip().lower()
        if not host:
            continue
        if len(host) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", host
        ):
            raise ValueError("Host materiali non valido")
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


class Settings(BaseSettings):
    bunny_library_id: int
    bunny_stream_api_key: str = Field(repr=False)
    bunny_cdn_hostname: str
    bunny_token_auth_key: str | None = Field(default=None, repr=False)
    openai_api_key: str = Field(repr=False)
    assemblyai_api_key: str | None = Field(default=None, repr=False)
    assemblyai_region: Literal["eu", "global"] = "eu"
    app_password: str = Field(repr=False)
    database_path: str = "bunny-video-report.sqlite3"
    google_sheet_id: str = "1yw2K-1cH5goQER1tP3KItvluXMv7N_H5RoratcWZoZo"
    google_service_account_json: str | None = Field(default=None, repr=False)
    inventory_match_threshold: float = Field(default=.88, ge=0, le=1, allow_inf_nan=False)
    inventory_match_margin: float = Field(default=.08, ge=0, le=1, allow_inf_nan=False)
    temp_root: str | None = None
    media_runtime_seconds: float = Field(default=21600, gt=0, allow_inf_nan=False)
    media_inactivity_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    media_max_workspace_bytes: int = Field(default=2_000_000_000, gt=0)
    material_allowed_hosts: str = "www.assoholding.it"
    material_library_dir: str | None = None
    bunny_storage_zone: str | None = None
    bunny_storage_api_key: str | None = Field(default=None, repr=False)
    bunny_storage_pull_hostname: str | None = None
    bunny_storage_region: str = "de"

    @field_validator("bunny_storage_pull_hostname")
    @classmethod
    def validate_storage_pull_hostname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        hosts = parse_material_allowed_hosts([value])
        if len(hosts) != 1:
            raise ValueError("Hostname CDN della pull zone non valido")
        return hosts[0]

    @field_validator("bunny_storage_region")
    @classmethod
    def validate_storage_region(cls, value: str) -> str:
        if value not in {"de", "ny", "la", "sg", "syd", "uk", "se", "br", "jh"}:
            raise ValueError("Regione Bunny Storage non valida")
        return value

    @field_validator("material_allowed_hosts")
    @classmethod
    def validate_material_hosts(cls, value: str) -> str:
        return ",".join(parse_material_allowed_hosts(value))

    @property
    def parsed_material_allowed_hosts(self) -> tuple[str, ...]:
        return parse_material_allowed_hosts(self.material_allowed_hosts)

    @field_validator(
        "assemblyai_api_key", "google_service_account_json",
        "bunny_storage_zone", "bunny_storage_api_key", mode="before",
    )
    @classmethod
    def blank_optional_secret_disables_integration(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
