import asyncio
import fcntl
import json
import os
import secrets
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from app.config import Settings


@dataclass(frozen=True)
class PeerProfile:
    credential: str
    client_email: str
    uri: str


@dataclass(frozen=True)
class PeerStats:
    last_activity_at: datetime | None
    transfer_rx: int
    transfer_tx: int


class VpnBackend:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def create_peer(self, client_email: str, label: str) -> PeerProfile:
        raise NotImplementedError

    async def revoke_peer(self, client_email: str) -> None:
        raise NotImplementedError

    async def activate_peer(self, credential: str, client_email: str) -> None:
        raise NotImplementedError

    async def peer_stats(self) -> dict[str, PeerStats]:
        raise NotImplementedError

    def render_uri(self, credential: str, label: str) -> str:
        s = self.settings
        query = urlencode(
            {
                "encryption": "none",
                "flow": "xtls-rprx-vision",
                "security": "reality",
                "sni": s.xray_reality_server_name,
                "fp": s.xray_fingerprint,
                "pbk": s.xray_reality_public_key,
                "sid": s.xray_reality_short_id,
                "type": "tcp",
                "headerType": "none",
            }
        )
        return (
            f"vless://{credential}@{s.xray_public_host}:{s.xray_public_port}"
            f"?{query}#{quote(label)}"
        )


class MockVpnBackend(VpnBackend):
    async def create_peer(self, client_email: str, label: str) -> PeerProfile:
        credential = str(uuid.uuid4())
        return PeerProfile(credential, client_email, self.render_uri(credential, label))

    async def revoke_peer(self, client_email: str) -> None:
        return None

    async def activate_peer(self, credential: str, client_email: str) -> None:
        return None

    async def peer_stats(self) -> dict[str, PeerStats]:
        return {}


class XrayBackend(VpnBackend):
    @staticmethod
    def _decode_docker_response(response: bytes) -> bytes:
        headers, separator, body = response.partition(b"\r\n\r\n")
        if not separator or b"transfer-encoding: chunked" not in headers.lower():
            return body if separator else response

        decoded = bytearray()
        while body:
            size_line, separator, body = body.partition(b"\r\n")
            if not separator:
                raise RuntimeError("Invalid chunked response from Docker API")
            size = int(size_line.split(b";", 1)[0], 16)
            if size == 0:
                break
            decoded.extend(body[:size])
            body = body[size + 2 :]
        return bytes(decoded)

    def _mutate_clients(
        self, *, credential: str | None = None, client_email: str, remove: bool = False
    ) -> bool:
        config_path = Path(self.settings.xray_config_path)
        lock_path = config_path.with_suffix(f"{config_path.suffix}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            config = json.loads(config_path.read_text())
            inbound = next(
                (
                    item
                    for item in config.get("inbounds", [])
                    if item.get("tag") == self.settings.xray_inbound_tag
                ),
                None,
            )
            if inbound is None:
                raise RuntimeError(
                    f"Xray inbound {self.settings.xray_inbound_tag!r} not found"
                )

            clients = inbound.setdefault("settings", {}).setdefault("clients", [])
            existing = next(
                (item for item in clients if item.get("email") == client_email), None
            )
            if remove:
                if existing is None:
                    return False
                clients.remove(existing)
            else:
                if credential is None:
                    raise ValueError("credential is required when adding an Xray client")
                desired = {"id": credential, "email": client_email}
                if self.settings.xray_flow:
                    desired["flow"] = self.settings.xray_flow
                if existing == desired:
                    return False
                if existing is None:
                    clients.append(desired)
                else:
                    existing.clear()
                    existing.update(desired)

            temporary_path = config_path.with_suffix(f"{config_path.suffix}.tmp")
            with temporary_path.open("w") as output:
                json.dump(config, output, ensure_ascii=True, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary_path, stat.S_IMODE(config_path.stat().st_mode))
            os.replace(temporary_path, config_path)
            return True

    async def _docker_request(
        self, method: str, path: str, payload: dict | None = None
    ) -> tuple[bytes, bytes]:
        reader, writer = await asyncio.open_unix_connection(self.settings.docker_socket_path)
        body = json.dumps(payload).encode() if payload is not None else b""
        writer.write(
            (
                f"{method} {path} HTTP/1.1\r\n"
                "Host: docker\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        status_line = await reader.readline()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return status_line, self._decode_docker_response(response)

    async def _restart_xray(self) -> None:
        path = f"/containers/{quote(self.settings.xray_container_name, safe='')}/restart?t=10"
        status_line, response = await self._docker_request("POST", path)
        if b" 204 " not in status_line:
            raise RuntimeError(
                f"Failed to restart Xray container: {status_line.decode().strip()} "
                f"{response.decode(errors='replace').strip()}"
            )

    async def _test_xray_config(self, container_path: str) -> None:
        container = quote(self.settings.xray_container_name, safe="")
        status_line, response = await self._docker_request(
            "POST",
            f"/containers/{container}/exec",
            {
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": True,
                "Cmd": ["xray", "run", "-test", "-config", container_path],
            },
        )
        if b" 201 " not in status_line:
            raise RuntimeError(f"Could not create Xray config test: {response.decode(errors='replace')}")
        exec_id = json.loads(response)["Id"]
        status_line, output = await self._docker_request(
            "POST", f"/exec/{quote(exec_id, safe='')}/start", {"Detach": False, "Tty": True}
        )
        if b" 200 " not in status_line:
            raise RuntimeError(f"Could not run Xray config test: {output.decode(errors='replace')}")
        status_line, inspection = await self._docker_request(
            "GET", f"/exec/{quote(exec_id, safe='')}/json"
        )
        if b" 200 " not in status_line:
            raise RuntimeError(
                f"Could not inspect Xray config test: {inspection.decode(errors='replace')}"
            )
        returncode = json.loads(inspection)["ExitCode"]
        output_text = output.decode(errors="replace").strip()
        if returncode != 0:
            raise RuntimeError(f"Xray rejected config (returncode={returncode}): {output_text}")

    async def _ensure_xray_running(self) -> None:
        container = quote(self.settings.xray_container_name, safe="")
        status_line, response = await self._docker_request("GET", f"/containers/{container}/json")
        if b" 200 " not in status_line or not json.loads(response).get("State", {}).get("Running"):
            raise RuntimeError("Xray container did not start after config update")

    def _build_server_config(self, server) -> tuple[Path, Path]:
        config_path = Path(self.settings.xray_config_path)
        config = json.loads(config_path.read_text())
        self.render_server_config(config, server)

        candidate_path = config_path.with_name(f"{config_path.stem}.candidate{config_path.suffix}")
        with candidate_path.open("w") as output:
            json.dump(config, output, ensure_ascii=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(candidate_path, stat.S_IMODE(config_path.stat().st_mode))
        json.loads(candidate_path.read_text())
        container_config = Path(self.settings.xray_container_config_path)
        return candidate_path, container_config.with_name(candidate_path.name)

    def render_server_config(self, config: dict, server, clients: list[dict] | None = None) -> dict:
        inbound = next(
            (
                item
                for item in config.get("inbounds", [])
                if item.get("tag") == self.settings.xray_inbound_tag
            ),
            None,
        )
        if inbound is None:
            raise RuntimeError(f"Xray inbound {self.settings.xray_inbound_tag!r} not found")

        is_xhttp = server.transport == "xhttp"
        rendered_clients = (
            [dict(client) for client in clients]
            if clients is not None
            else inbound.setdefault("settings", {}).setdefault("clients", [])
        )
        for client in rendered_clients:
            if is_xhttp:
                client.pop("flow", None)
            else:
                client["flow"] = server.flow or "xtls-rprx-vision"
        inbound.setdefault("settings", {})["clients"] = rendered_clients
        inbound["settings"]["decryption"] = (
            server.vless_decryption if getattr(server, "pq_enabled", False) else "none"
        )

        inbound["port"] = server.public_port
        inbound["sniffing"] = {
            "enabled": not getattr(server, "pq_enabled", False),
            "destOverride": ["http", "tls", "quic"],
        }
        stream = inbound.setdefault("streamSettings", {})
        stream["network"] = "xhttp" if is_xhttp else "raw"
        stream["security"] = "reality"
        if is_xhttp:
            xhttp_settings = {
                "host": "",
                "path": server.xhttp_path or "/",
                "mode": server.xhttp_mode or "auto",
            }
            if getattr(server, "pq_enabled", False):
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
        reality["target"] = server.reality_target
        reality["serverNames"] = [server.reality_server_name]
        reality["shortIds"] = [server.reality_short_id]
        if getattr(server, "pq_enabled", False):
            reality["mldsa65Seed"] = server.reality_mldsa65_seed
        else:
            reality.pop("mldsa65Seed", None)
        return config

    async def apply_server_config(self, server) -> None:
        candidate_path, container_candidate = await asyncio.to_thread(
            self._build_server_config, server
        )
        config_path = Path(self.settings.xray_config_path)
        backup_path = config_path.with_suffix(f"{config_path.suffix}.backup")
        try:
            await self._test_xray_config(str(container_candidate))
        except Exception as error:
            raise RuntimeError(
                f"{error}; candidate kept for diagnostics at {candidate_path}"
            ) from error
        await asyncio.to_thread(shutil.copy2, config_path, backup_path)
        await asyncio.to_thread(shutil.copy2, candidate_path, config_path)
        try:
            await self._restart_xray()
            await self._ensure_xray_running()
        except Exception as error:
            await asyncio.to_thread(shutil.copy2, backup_path, config_path)
            await self._restart_xray()
            raise RuntimeError(
                f"{error}; candidate kept for diagnostics at {candidate_path}"
            ) from error
        candidate_path.unlink(missing_ok=True)

    async def _grpc(self, method: str, payload: dict) -> dict:
        process = await asyncio.create_subprocess_exec(
            "grpcurl",
            "-plaintext",
            "-d",
            json.dumps(payload),
            self.settings.xray_api_address,
            method,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(f"Xray API failed: {stderr.decode().strip()}")
        output = stdout.decode().strip()
        return json.loads(output) if output else {}

    async def create_peer(self, client_email: str, label: str) -> PeerProfile:
        credential = str(uuid.uuid4())
        await self.activate_peer(credential, client_email)
        return PeerProfile(credential, client_email, self.render_uri(credential, label))

    async def activate_peer(self, credential: str, client_email: str) -> None:
        changed = await asyncio.to_thread(
            self._mutate_clients, credential=credential, client_email=client_email
        )
        if changed:
            await self._restart_xray()

    async def revoke_peer(self, client_email: str) -> None:
        changed = await asyncio.to_thread(
            self._mutate_clients, client_email=client_email, remove=True
        )
        if changed:
            await self._restart_xray()

    async def peer_stats(self) -> dict[str, PeerStats]:
        response = await self._grpc(
            "xray.app.stats.command.StatsService/QueryStats",
            {"pattern": "user>>>", "reset": False},
        )
        result: dict[str, PeerStats] = {}
        for item in response.get("stat", []):
            parts = item.get("name", "").split(">>>")
            if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
                continue
            email, direction = parts[1], parts[3]
            previous = result.get(email, PeerStats(None, 0, 0))
            value = int(item.get("value", 0))
            active_at = datetime.now(UTC) if value else previous.last_activity_at
            result[email] = PeerStats(
                active_at,
                value if direction == "downlink" else previous.transfer_rx,
                value if direction == "uplink" else previous.transfer_tx,
            )
        return result


def build_vpn_backend(settings: Settings) -> VpnBackend:
    if settings.vpn_backend == "xray":
        required = {
            "XRAY_PUBLIC_HOST": settings.xray_public_host,
            "XRAY_REALITY_PUBLIC_KEY": settings.xray_reality_public_key,
            "XRAY_REALITY_SHORT_ID": settings.xray_reality_short_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing Xray settings: {', '.join(missing)}")
        return XrayBackend(settings)
    return MockVpnBackend(settings)


def new_client_email() -> str:
    return f"device-{secrets.token_hex(12)}@vpn.local"
