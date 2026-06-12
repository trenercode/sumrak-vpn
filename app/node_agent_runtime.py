import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

VERSION = "0.2.0"
XRAY_IMAGE = os.getenv("XRAY_IMAGE", "ghcr.io/xtls/xray-core:26.6.1")
PANEL_URL = os.environ["PANEL_URL"].rstrip("/")
AGENT_TOKEN = os.environ["AGENT_TOKEN"]
CONFIG_PATH = Path(os.getenv("XRAY_CONFIG_PATH", "/data/config.json"))
HOST_NODE_DIR = Path(os.getenv("HOST_NODE_DIR", "/opt/sumrak-node"))
XRAY_CONTAINER_NAME = os.getenv("XRAY_CONTAINER_NAME", "sumrak-node-xray")
INTERVAL = int(os.getenv("SYNC_INTERVAL", "30"))


def log(message: str) -> None:
    print(f"[sumrak-node-agent] {message}", flush=True)


def api(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{PANEL_URL}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": f"sumrak-node-agent/{VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def render(desired: dict) -> Path:
    config = json.loads(CONFIG_PATH.read_text())
    inbound = next(item for item in config["inbounds"] if item.get("tag") == "vless-reality")
    inbound["port"] = desired["public_port"]
    clients = [dict(client) for client in desired["clients"]]
    is_xhttp = desired["transport"] == "xhttp"
    for client in clients:
        if is_xhttp:
            client.pop("flow", None)
        else:
            client["flow"] = "xtls-rprx-vision"
    inbound.setdefault("settings", {})["clients"] = clients
    inbound["settings"]["decryption"] = (
        desired["vless_decryption"] if desired.get("pq_enabled") else "none"
    )
    inbound["sniffing"] = {
        "enabled": not desired.get("pq_enabled", False),
        "destOverride": ["http", "tls", "quic"],
    }
    stream = inbound.setdefault("streamSettings", {})
    stream["network"] = "xhttp" if is_xhttp else "raw"
    stream["security"] = "reality"
    if is_xhttp:
        xhttp_settings = {
            "host": "",
            "path": desired["xhttp_path"] or "/",
            "mode": desired["xhttp_mode"] or "auto",
        }
        if desired.get("pq_enabled"):
            xhttp_settings.update(
                {
                    "xPaddingBytes": "100-1000",
                    "scMaxEachPostBytes": "1000000",
                    "scMaxBufferedPosts": 30,
                    "scStreamUpServerSecs": "20-80",
                }
            )
        stream["xhttpSettings"] = xhttp_settings
    else:
        stream.pop("xhttpSettings", None)
    reality = stream.setdefault("realitySettings", {})
    reality["target"] = desired["reality_target"]
    reality["serverNames"] = [desired["reality_server_name"]]
    reality["shortIds"] = [desired["reality_short_id"]]
    if desired.get("pq_enabled"):
        reality["mldsa65Seed"] = desired["reality_mldsa65_seed"]
    else:
        reality.pop("mldsa65Seed", None)
    outbounds = [
        outbound
        for outbound in config.get("outbounds", [])
        if outbound.get("tag") != "blocked"
    ]
    if not any(outbound.get("tag") == "direct" for outbound in outbounds):
        outbounds.append({"tag": "direct", "protocol": "freedom"})
    outbounds.append({"tag": "blocked", "protocol": "blackhole"})
    config["outbounds"] = outbounds
    candidate = CONFIG_PATH.with_name("config.candidate.json")
    candidate.write_text(json.dumps(config, ensure_ascii=True, indent=2) + "\n")
    json.loads(candidate.read_text())
    return candidate


def apply(candidate: Path) -> None:
    host_candidate = HOST_NODE_DIR / candidate.name
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{host_candidate}:/etc/xray/config.json:ro",
            XRAY_IMAGE,
            "run",
            "-test",
            "-config",
            "/etc/xray/config.json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    backup = CONFIG_PATH.with_name("config.json.backup")
    shutil.copy2(CONFIG_PATH, backup)
    shutil.copy2(candidate, CONFIG_PATH)
    try:
        subprocess.run(
            ["docker", "restart", XRAY_CONTAINER_NAME],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        shutil.copy2(backup, CONFIG_PATH)
        subprocess.run(["docker", "restart", XRAY_CONTAINER_NAME])
        raise
    candidate.unlink(missing_ok=True)


def reality_public_key() -> str:
    config = json.loads(CONFIG_PATH.read_text())
    inbound = next(item for item in config["inbounds"] if item.get("tag") == "vless-reality")
    private_key = inbound["streamSettings"]["realitySettings"]["privateKey"]
    result = subprocess.run(
        ["docker", "exec", XRAY_CONTAINER_NAME, "xray", "x25519", "-i", private_key],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in f"{result.stdout}\n{result.stderr}".splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().lower() in {"public key", "password (publickey)"}:
            return value.strip()
    raise RuntimeError("Could not derive REALITY public key")


def report(error: str | None, clients_count: int) -> None:
    try:
        try:
            public_key = reality_public_key()
        except Exception:
            public_key = None
        api(
            "/api/node/report",
            "POST",
            {
                "node_version": VERSION,
                "last_error": error,
                "clients_count": clients_count,
                "reality_public_key": public_key,
            },
        )
    except Exception as report_error:
        log(f"report failed: {type(report_error).__name__}")


def main() -> None:
    while True:
        clients_count = 0
        try:
            desired = api("/api/node/sync")
            clients_count = len(desired["clients"])
            candidate = render(desired)
            if candidate.read_bytes() != CONFIG_PATH.read_bytes():
                apply(candidate)
            else:
                candidate.unlink(missing_ok=True)
            report(None, clients_count)
            log(f"sync completed: clients={clients_count}")
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            log(message[:2000])
            report(message[:2000], clients_count)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
