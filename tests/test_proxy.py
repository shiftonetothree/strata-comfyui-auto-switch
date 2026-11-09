"""End-to-end tests for the HTTP/WebSocket proxy and the admin API."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from relay.admin import AdminServer
from relay.config import RelayConfig
from relay.manager import BusyError, CooldownError, SwitchFailed
from relay.proxy import ProxyServer

from tests.helpers import make_config

REQUEST_TIMEOUT = 5


class StubManager:
    def __init__(self, ensure_error: Exception | None = None):
        self.ensure_error = ensure_error
        self.ensure_calls: list[tuple[str, bool]] = []
        self.requests: list[str] = []
        self.ignored: list[tuple[str, dict]] = []
        self.frames = 0
        self.active = "comfyui"
        self.switching_to = None
        self.services = {"comfyui": object(), "strata": object()}

    async def ensure_active(self, name: str, *, force: bool = False) -> None:
        self.ensure_calls.append((name, force))
        if self.ensure_error is not None:
            raise self.ensure_error

    def note_request(self, name: str, **kwargs) -> None:
        self.requests.append(name)

    def note_ignored(self, name: str, **kwargs) -> None:
        self.ignored.append((name, kwargs))

    def note_frame(self, name: str) -> None:
        self.frames += 1

    async def status(self) -> dict:
        return {
            "active": self.active,
            "switching_to": self.switching_to,
            "services": {name: {"running": True} for name in self.services},
        }

    async def status_only(self) -> dict:
        return await self.status()


class FakeUpstream:
    def __init__(self):
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.requests: list[dict] = []
        self.ws_payloads: list[bytes] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, limit=256 * 1024
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw[:-4].split(b"\r\n")
            method, target, _ = lines[0].split(b" ")
            headers: dict[bytes, list[bytes]] = {}
            for line in lines[1:]:
                name, sep, value = line.partition(b":")
                if sep:
                    headers.setdefault(name.strip().lower(), []).append(value.strip())

            body = b""
            if b"content-length" in headers:
                body = await reader.readexactly(int(headers[b"content-length"][0]))
            elif b"chunked" in (headers.get(b"transfer-encoding", [b""])[0]).lower():
                body = await self._read_chunked(reader)

            self.requests.append(
                {
                    "method": method,
                    "target": target,
                    "headers": headers,
                    "body": body,
                }
            )
            path = target.split(b"?")[0]

            if path == b"/hello":
                self._respond(writer, b"200 OK", b"text/plain", b"hello")
            elif path == b"/health":
                self._respond(writer, b"200 OK", b"application/json", b'{"status": "ok"}')
            elif path == b"/echo":
                self._respond(writer, b"200 OK", b"application/octet-stream", body)
            elif path == b"/chunked":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                    b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
                )
                await writer.drain()
            elif path == b"/ws":
                writer.write(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: dummy\r\n\r\n"
                )
                await writer.drain()
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    self.ws_payloads.append(data)
                    writer.write(data)
                    await writer.drain()
                return
            else:
                self._respond(writer, b"404 Not Found", b"text/plain", b"missing")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    @staticmethod
    async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            line = await reader.readuntil(b"\r\n")
            size = int(line.split(b";")[0], 16)
            if size == 0:
                while True:
                    trailer = await reader.readuntil(b"\r\n")
                    if trailer == b"\r\n":
                        return bytes(body)
            body += await reader.readexactly(size)
            await reader.readexactly(2)

    @staticmethod
    def _respond(writer, status: bytes, content_type: bytes, body: bytes) -> None:
        writer.write(
            b"HTTP/1.1 " + status + b"\r\nContent-Type: " + content_type
            + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )


class ProxyTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.dir = Path(self._tempdir.name)
        self.upstream = FakeUpstream()
        await self.upstream.start()
        self.manager = StubManager()
        self.proxy: ProxyServer | None = None
        self.admin: AdminServer | None = None

    async def asyncTearDown(self) -> None:
        if self.proxy is not None:
            await self.proxy.close()
        if self.admin is not None:
            await self.admin.close()
        await self.upstream.close()
        self._tempdir.cleanup()

    def build_config(self) -> RelayConfig:
        config = make_config(self.dir)
        service = dataclasses.replace(
            config.services["comfyui"],
            listen_port=0,
            upstream_port=self.upstream.port,
        )
        return dataclasses.replace(config, services={"comfyui": service})

    async def start_proxy(self) -> int:
        self.proxy = ProxyServer(self.build_config(), self.manager, "comfyui")
        await self.proxy.start()
        return self.proxy.port

    async def raw_request(self, port: int, payload: bytes) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), REQUEST_TIMEOUT)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return data

    # ---------------------------------------------------------------- proxying

    async def test_service_bind_host_overrides_relay_bind(self):
        config = dataclasses.replace(self.build_config(), bind_host="0.0.0.0")
        config = dataclasses.replace(
            config,
            services={
                "comfyui": dataclasses.replace(
                    config.services["comfyui"], bind_host="127.0.0.1"
                )
            },
        )
        self.proxy = ProxyServer(config, self.manager, "comfyui")
        await self.proxy.start()
        self.assertEqual(
            self.proxy.server.sockets[0].getsockname()[0], "127.0.0.1"
        )

    async def test_get_is_proxied(self):
        port = await self.start_proxy()
        response = await self.raw_request(
            port, b"GET /hello?x=1 HTTP/1.1\r\nHost: example:8188\r\n\r\n"
        )

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertIn(b"Connection: close", response)
        self.assertTrue(response.endswith(b"hello"))
        self.assertEqual(self.manager.requests, ["comfyui"])

        request = self.upstream.requests[0]
        self.assertEqual(request["method"], b"GET")
        self.assertEqual(request["target"], b"/hello?x=1")
        self.assertEqual(request["headers"][b"host"], [b"example:8188"])
        self.assertEqual(request["headers"][b"connection"], [b"close"])
        self.assertIn(b"x-forwarded-for", request["headers"])

    async def test_post_body_is_streamed(self):
        port = await self.start_proxy()
        body = b'{"prompt": "a cat"}'
        response = await self.raw_request(
            port,
            b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nContent-Type: application/json\r\n\r\n"
            + body,
        )

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertTrue(response.endswith(body))
        self.assertEqual(self.upstream.requests[0]["body"], body)

    async def test_chunked_request_body(self):
        port = await self.start_proxy()
        response = await self.raw_request(
            port,
            b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n",
        )

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertTrue(response.endswith(b"hello world"))
        self.assertEqual(self.upstream.requests[0]["body"], b"hello world")

    async def test_chunked_response_is_relayed_verbatim(self):
        port = await self.start_proxy()
        response = await self.raw_request(port, b"GET /chunked HTTP/1.1\r\nHost: x\r\n\r\n")

        head, _, body = response.partition(b"\r\n\r\n")
        self.assertIn(b"Transfer-Encoding: chunked", head)
        self.assertEqual(body, b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")

    async def test_head_has_no_body(self):
        port = await self.start_proxy()
        response = await self.raw_request(port, b"HEAD /hello HTTP/1.1\r\nHost: x\r\n\r\n")

        head, _, body = response.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(body, b"")

    async def test_ignored_path_when_inactive_returns_503_without_switch(self):
        self.manager.active = "strata"
        config = self.build_config()
        config = dataclasses.replace(
            config,
            services={
                "comfyui": dataclasses.replace(
                    config.services["comfyui"],
                    ignore_activity_paths=("/health",),
                )
            },
        )
        self.proxy = ProxyServer(config, self.manager, "comfyui")
        await self.proxy.start()

        response = await self.raw_request(
            self.proxy.port,
            b"GET /health HTTP/1.1\r\nHost: x\r\nAccept: application/json\r\n\r\n",
        )

        head, _, body = response.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 503"))
        self.assertIn(b"inactive", body)
        self.assertEqual(self.manager.ensure_calls, [])
        self.assertEqual(self.manager.requests, [])
        self.assertEqual(len(self.manager.ignored), 1)
        self.assertFalse(self.manager.ignored[0][1]["active"])
        self.assertEqual(self.upstream.requests, [])

    async def test_ignored_path_when_active_is_proxied_without_activity(self):
        self.manager.active = "comfyui"
        config = self.build_config()
        config = dataclasses.replace(
            config,
            services={
                "comfyui": dataclasses.replace(
                    config.services["comfyui"],
                    ignore_activity_paths=("/health",),
                )
            },
        )
        self.proxy = ProxyServer(config, self.manager, "comfyui")
        await self.proxy.start()

        response = await self.raw_request(
            self.proxy.port, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
        )

        self.assertTrue(response.startswith(b"HTTP/1.1 200"))
        self.assertEqual(self.manager.ensure_calls, [])
        self.assertEqual(self.manager.requests, [])
        self.assertEqual(len(self.manager.ignored), 1)
        self.assertTrue(self.manager.ignored[0][1]["active"])
        self.assertEqual(self.upstream.requests[0]["target"], b"/health")

    async def test_websocket_upgrade_tunnels_and_counts_frames(self):
        port = await self.start_proxy()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /ws?clientId=abc HTTP/1.1\r\nHost: x\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        await writer.drain()

        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), REQUEST_TIMEOUT)
        self.assertTrue(head.startswith(b"HTTP/1.1 101 Switching Protocols"))
        self.assertIn(b"Sec-WebSocket-Accept: dummy", head)

        mask = b"\x01\x02\x03\x04"
        payload = b"hello"
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        frame = b"\x81" + bytes([0x80 | len(payload)]) + mask + masked
        writer.write(frame)
        await writer.drain()

        echoed = await asyncio.wait_for(reader.readexactly(len(frame)), REQUEST_TIMEOUT)
        self.assertEqual(echoed, frame)

        for _ in range(100):
            if self.manager.frames:
                break
            await asyncio.sleep(0.01)
        self.assertGreaterEqual(self.manager.frames, 1)
        self.assertIn(b"/ws?clientId=abc", self.upstream.requests[0]["target"])

        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    # ---------------------------------------------------------------- refusals

    async def test_cooldown_returns_503_with_retry_after(self):
        self.manager.ensure_error = CooldownError("comfyui", 87.4)
        port = await self.start_proxy()

        response = await self.raw_request(
            port, b"GET /hello HTTP/1.1\r\nHost: x\r\nAccept: application/json\r\n\r\n"
        )

        head, _, body = response.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 503 Service Unavailable"))
        self.assertIn(b"Retry-After: 88", head)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "cooldown")
        self.assertEqual(payload["active"], "comfyui")
        self.assertEqual(payload["requested"], "comfyui")
        self.assertEqual(self.manager.requests, [])
        self.assertEqual(self.upstream.requests, [])

    async def test_cooldown_returns_html_for_browsers(self):
        self.manager.ensure_error = CooldownError("comfyui", 60)
        port = await self.start_proxy()

        response = await self.raw_request(
            port,
            b"GET / HTTP/1.1\r\nHost: x\r\nAccept: text/html,application/xhtml+xml\r\n\r\n",
        )

        head, _, body = response.partition(b"\r\n\r\n")
        self.assertIn(b"Content-Type: text/html", head)
        self.assertIn("冷却".encode(), body)
        self.assertIn(b'http-equiv="refresh"', body)

    async def test_busy_returns_503(self):
        self.manager.ensure_error = BusyError("strata")
        port = await self.start_proxy()

        response = await self.raw_request(port, b"GET /hello HTTP/1.1\r\nHost: x\r\n\r\n")

        head, _, body = response.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 503"))
        self.assertIn(b"Retry-After: 10", head)
        self.assertEqual(json.loads(body)["error"], "busy")

    async def test_switch_failure_returns_503(self):
        self.manager.ensure_error = SwitchFailed("boom")
        port = await self.start_proxy()

        response = await self.raw_request(port, b"GET /hello HTTP/1.1\r\nHost: x\r\n\r\n")

        self.assertTrue(response.startswith(b"HTTP/1.1 503"))
        self.assertIn(b"switch_failed", response)

    async def test_unreachable_upstream_returns_502(self):
        await self.upstream.close()
        port = await self.start_proxy()

        response = await self.raw_request(port, b"GET /hello HTTP/1.1\r\nHost: x\r\n\r\n")

        self.assertTrue(response.startswith(b"HTTP/1.1 502 Bad Gateway"))
        self.assertIn(b"upstream_unreachable", response)

    async def test_bad_request_line_returns_400(self):
        port = await self.start_proxy()

        response = await self.raw_request(port, b"BOGUS\r\n\r\n")

        self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request"))

    # ------------------------------------------------------------------- admin

    async def start_admin(self) -> int:
        config = dataclasses.replace(self.build_config(), admin_port=0)
        self.admin = AdminServer(config, self.manager)
        await self.admin.start()
        return self.admin.port

    async def admin_request(self, port: int, payload: bytes) -> tuple[int, dict, bytes]:
        raw = await self.raw_request(port, payload)
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {}
        return status, parsed, head

    async def test_admin_status_and_health(self):
        port = await self.start_admin()

        status, payload, _ = await self.admin_request(port, b"GET /status HTTP/1.1\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertEqual(payload["active"], "comfyui")

        status, payload, _ = await self.admin_request(port, b"GET /health HTTP/1.1\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    async def test_admin_manual_switch(self):
        port = await self.start_admin()

        status, payload, _ = await self.admin_request(
            port, b"POST /switch/strata?force=1 HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["active"], "comfyui")  # StubManager.status is static
        self.assertEqual(self.manager.ensure_calls, [("strata", True)])

    async def test_admin_switch_requires_post_and_known_service(self):
        port = await self.start_admin()

        status, payload, _ = await self.admin_request(
            port, b"GET /switch/strata HTTP/1.1\r\n\r\n"
        )
        self.assertEqual(status, 405)

        status, payload, _ = await self.admin_request(
            port, b"POST /switch/nope HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown_service")

    async def test_admin_reports_cooldown_with_409(self):
        self.manager.ensure_error = CooldownError("comfyui", 45)
        port = await self.start_admin()

        status, payload, head = await self.admin_request(
            port, b"POST /switch/strata HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "cooldown")
        self.assertIn(b"Retry-After: 45", head)


if __name__ == "__main__":
    unittest.main()
