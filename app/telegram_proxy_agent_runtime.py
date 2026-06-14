import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

PANEL_URL = os.environ.get("PANEL_URL", "http://127.0.0.1:8000").rstrip("/")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
DATA_DIR = Path("/data")
VERSION = "1.2.0"


def api(path, method="GET", payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        PANEL_URL + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def render_compose(config):
    secret = config["secret"].removeprefix("dd")
    environment = [f"SECRET={secret}"]
    if config.get("sponsor_tag"):
        environment.append(f"TAG={config['sponsor_tag']}")
    environment_yaml = "\n".join(
        f'      {item.split("=", 1)[0]}: "{item.split("=", 1)[1]}"' for item in environment
    )
    return f"""name: sumrak-telegram-proxy

services:
  proxy:
    image: telegrammessenger/proxy:latest
    container_name: sumrak-telegram-proxy
    restart: unless-stopped
    environment:
{environment_yaml}
    ports:
      - "{config['public_port']}:443"
  agent:
    build:
      context: .
      dockerfile: Dockerfile.agent
    container_name: sumrak-telegram-proxy-agent
    restart: unless-stopped
    environment:
      PANEL_URL: "{PANEL_URL}"
      AGENT_TOKEN: "{AGENT_TOKEN}"
    volumes:
      - ./:/data
      - /var/run/docker.sock:/var/run/docker.sock
"""


def run_command(args, check=True):
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part.strip())
        raise RuntimeError(f"{' '.join(args)} failed ({result.returncode}): {output}")
    return result


def sync():
    config = api("/api/telegram-proxy/sync", "POST")
    compose = render_compose(config)
    digest = hashlib.sha256(compose.encode()).hexdigest()
    current = DATA_DIR / "compose.yaml"
    if not config["enabled"]:
        run_command(["docker", "stop", "sumrak-telegram-proxy"], check=False)
        return digest
    if not current.exists() or current.read_text() != compose:
        candidate = DATA_DIR / "compose.candidate.yaml"
        candidate.write_text(compose)
        run_command(["docker", "compose", "-f", str(candidate), "config"])
        current.write_text(compose)
        candidate.unlink(missing_ok=True)
        run_command(["docker", "compose", "-f", str(current), "up", "-d", "proxy"])
    return digest


def metrics():
    return None, None


def main():
    while True:
        error = None
        status = "online"
        digest = None
        try:
            digest = sync()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            status = "error"
        try:
            active_connections, traffic_bytes = metrics()
            api(
                "/api/telegram-proxy/heartbeat",
                "POST",
                {
                    "status": status,
                    "version": VERSION,
                    "active_connections": active_connections,
                    "traffic_bytes": traffic_bytes,
                    "config_hash": digest,
                    "error": error,
                },
            )
        except Exception as exc:
            print(f"heartbeat failed: {exc}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
