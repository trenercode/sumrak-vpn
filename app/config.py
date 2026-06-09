from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./vpn.db"
    bot_token: str = ""
    admin_username: str = "admin"
    admin_password: str = "change-me-now"
    trial_days: int = 3
    max_devices: int = 10

    vpn_backend: str = "mock"
    wg_interface: str = "wg0"
    wg_server_public_key: str = ""
    wg_endpoint: str = "vpn.example.com:51820"
    wg_network: str = "10.66.0.0/24"
    wg_server_address: str = "10.66.0.1"
    wg_dns: str = "1.1.1.1,1.0.0.1"
    wg_persistent_keepalive: int = 25


@lru_cache
def get_settings() -> Settings:
    return Settings()

