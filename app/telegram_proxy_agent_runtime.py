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
VERSION = "1.0.1"


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
    tag = config.get("sponsor_tag", "")
    command = json.dumps(["run", config["secret"]] + ([tag] if tag else []), separators=(",", ":"))
    return f"""services:
  proxy:
    image: nineseconds/mtg:1
    container_name: sumrak-telegram-proxy
    restart: unless-stopped
    command: {command}
    environment:
      MTG_BIND: 0.0.0.0:3128
      MTG_STATS_BIND: 0.0.0.0:3129
    ports:
      - "{config['public_port']}:3128"
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
    try:
        with urllib.request.urlopen("http://proxy:3129/metrics", timeout=5) as response:
            text = response.read().decode()
    except Exception:
        return None, None
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or " " not in line:
            continue
        name, value = line.rsplit(" ", 1)
        try:
            values[name.split("{", 1)[0]] = float(value)
        except ValueError:
            continue
    active = values.get("mtg_connections")
    traffic = sum(value for name, value in values.items() if "traffic" in name)
    return int(active) if active is not None else None, int(traffic) if traffic else None


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
