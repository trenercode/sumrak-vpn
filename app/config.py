from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./vpn.db"
    bot_token: str = ""
    bot_username: str = ""
    support_telegram_url: str = ""
    panel_public_url: str = "http://127.0.0.1:8000"
    admin_username: str = "admin"
    admin_password: str = "change-me-now"
    trial_days: int = 3
    max_devices: int = 10
    broadcast_delay_seconds: float = 0.08

    vpn_backend: str = "mock"
    xray_api_address: str = "127.0.0.1:10085"
    xray_inbound_tag: str = "vless-reality"
    xray_config_path: str = "/data/xray/config.json"
    xray_container_name: str = "vpn-xray"
    docker_socket_path: str = "/var/run/docker.sock"
    xray_public_host: str = ""
    xray_public_port: int = 8443
    xray_reality_server_name: str = "www.microsoft.com"
    xray_reality_public_key: str = ""
    xray_reality_short_id: str = ""
    xray_fingerprint: str = "chrome"
    xray_flow: str = "xtls-rprx-vision"
    default_server_name: str = "Sumrak VPN"
    default_server_country_code: str = "RU"
    default_server_country_name: str = "Default"
    default_server_city: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
