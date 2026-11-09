"""Reverse HTTP/1.1 + WebSocket proxy, one listener per managed service.

The relay owns the public port for each service; this module forwards requests
unchanged (paths, headers, streaming bodies and WebSocket upgrades) to the
service's loopback upstream. It only rejects requests while the service is
switching or cooling down.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
from typing import Callable
from urllib.parse import urlsplit

from .config import RelayConfig, ServiceConfig
from .manager import BusyError, CooldownError, ServiceManager, SwitchFailed

log = logging.getLogger("relay.proxy")

HEAD_LIMIT = 256 * 1024
HEAD_TIMEOUT = 30.0
CONNECT_TIMEOUT = 5.0
CHUNK = 64 * 1024
REASON = {
    400: "Bad Request",
    405: "Method Not Allowed",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class BadRequest(Exception):
    pass


class FrameParser:
    """Count WebSocket data frames without buffering their payloads.

    Control frames (close/ping/pong) and continuation fragments are ignored, so
    an idle connection that only carries keepalive traffic is not considered
    "in use". ComfyUI sends real progress/status frames during a job, which are
    counted.
    """

    __slots__ = ("_buf", "_remaining", "_on_data")

    def __init__(self, on_data: Callable[[], None]):
        self._buf = bytearray()
        self._remaining = 0
        self._on_data = on_data

    def feed(self, data: bytes) -> None:
        self._buf += data
        while True:
            if self._remaining:
                take = min(self._remaining, len(self._buf))
                if take:
                    del self._buf[:take]
                    self._remaining -= take
                if self._remaining:
                    return

            if len(self._buf) < 2:
                return

            first, second = self._buf[0], self._buf[1]
            opcode = first & 0x0F
            masked = second & 0x80
            length = second & 0x7F
            header_len = 2
            if length == 126:
                header_len += 2
            elif length == 127:
                header_len += 8
            if masked:
                header_len += 4
            if len(self._buf) < header_len:
                return

            if length == 126:
                length = int.from_bytes(self._buf[2:4], "big")
            elif length == 127:
                length = int.from_bytes(self._buf[2:10], "big")

            if opcode not in (0x0, 0x8, 0x9, 0xA):
                self._on_data()

            del self._buf[:header_len]
            self._remaining = length


class ProxyServer:
    def __init__(self, config: RelayConfig, manager: ServiceManager, service_name: str):
        self.config = config
        self.manager = manager
        self.name = service_name
        self.service: ServiceConfig = config.services[service_name]
        self.server: asyncio.AbstractServer | None = None
        self.port = self.service.listen_port
        self._connections = 0

    async def start(self) -> None:
        bind_host = self.service.bind_host or self.config.bind_host
        self.server = await asyncio.start_server(
            self._handle,
            bind_host,
            self.service.listen_port,
            limit=HEAD_LIMIT,
            backlog=256,
            reuse_address=True,
        )
        sockets = self.server.sockets or []
        if sockets:
            self.port = sockets[0].getsockname()[1]
        log.info(
            "relay: listening on %s:%s -> %s (%s)",
            bind_host,
            self.port,
            self.service.upstream,
            self.name,
        )

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    # ------------------------------------------------------------------ serving

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._connections += 1
        peer = writer.get_extra_info("peername")
        try:
            await self._serve(reader, writer, peer)
        except BadRequest as exc:
            await self._send_error(writer, 400, "bad_request", str(exc))
        except (
            ConnectionResetError,
            BrokenPipeError,
            asyncio.IncompleteReadError,
        ):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("unexpected error while serving a %s request from %s", self.name, peer)
            await self._send_error(writer, 502, "relay_error", "internal relay error")
        finally:
            self._connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass

    async def _serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer,
    ) -> None:
        try:
            raw_head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT
            )
        except asyncio.LimitOverrunError:
            raise BadRequest("request headers are too large") from None
        except asyncio.TimeoutError:
            return

        lines = raw_head[:-4].split(b"\r\n")
        if not lines or not lines[0]:
            return
        parts = lines[0].split(b" ")
        if len(parts) < 3:
            raise BadRequest("malformed request line")
        method, raw_target, _version = parts[0], parts[1], parts[2]
        if method == b"CONNECT":
            raise BadRequest("CONNECT is not supported")
        headers = _parse_headers(lines)

        path = _normalize_target(raw_target)
        conn_tokens = {
            token.strip().lower()
            for token in (_header(headers, b"connection") or b"").split(b",")
        }
        upgrade_header = _header(headers, b"upgrade")
        is_upgrade = bool(upgrade_header) and b"upgrade" in conn_tokens

        try:
            body_mode, body_length = _request_body_mode(headers)
        except BadRequest:
            raise

        peer_ip = ""
        if isinstance(peer, (tuple, list)) and peer:
            peer_ip = str(peer[0])
        method_text = method.decode("latin-1")
        path_text = path.decode("latin-1", "replace")
        ignore_activity = self._ignores_activity(path_text)

        if ignore_activity:
            # Health/status polling must not switch services or keep one "in use".
            if self.manager.active != self.name:
                self.manager.note_ignored(
                    self.name,
                    method=method_text,
                    path=path_text,
                    peer=peer_ip,
                    active=False,
                )
                await self._send_error(
                    writer,
                    503,
                    "inactive",
                    (
                        f"{self.name} 当前未运行；健康/状态检查不会触发切换。"
                        f"请发送实际请求，或执行 ./run.sh switch {self.name} --force。"
                    ),
                    retry_after=15,
                    detail={"requested": self.name, "active": self.manager.active},
                    accept=_header(headers, b"accept") or b"",
                )
                return
            self.manager.note_ignored(
                self.name,
                method=method_text,
                path=path_text,
                peer=peer_ip,
                active=True,
            )
        else:
            try:
                await self.manager.ensure_active(self.name)
            except CooldownError as exc:
                log.info(
                    "rejected %s request %s %s: %s",
                    self.name,
                    method_text,
                    path_text,
                    exc,
                )
                await self._send_error(
                    writer,
                    503,
                    "cooldown",
                    (
                        f"{self.name} 正处于冷却期：{exc.active} 最近刚收到过请求，"
                        f"需要空闲 {self.config.cooldown_seconds:g} 秒后才能切换。"
                        f"请在 {math.ceil(exc.remaining)} 秒后重试。"
                    ),
                    retry_after=exc.remaining,
                    detail={
                        "active": exc.active,
                        "requested": self.name,
                        "retry_after": round(exc.remaining, 1),
                    },
                    accept=_header(headers, b"accept") or b"",
                )
                return
            except BusyError as exc:
                await self._send_error(
                    writer,
                    503,
                    "busy",
                    f"正在切换到 {exc.switching_to}，请稍候再试。",
                    retry_after=10,
                    detail={"switching_to": exc.switching_to, "requested": self.name},
                    accept=_header(headers, b"accept") or b"",
                )
                return
            except SwitchFailed as exc:
                await self._send_error(
                    writer,
                    503,
                    "switch_failed",
                    f"无法启动 {self.name}：{exc}",
                    retry_after=30,
                    detail={"requested": self.name, "reason": str(exc)},
                    accept=_header(headers, b"accept") or b"",
                )
                return

            self.manager.note_request(
                self.name, method=method_text, path=path_text, peer=peer_ip
            )

        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.service.upstream_host,
                    self.service.upstream_port,
                    limit=HEAD_LIMIT,
                ),
                CONNECT_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("upstream %s unreachable: %s", self.service.upstream, exc)
            await self._send_error(
                writer,
                502,
                "upstream_unreachable",
                f"{self.name} 的进程当前不可达（{self.service.upstream}）：{exc}",
                retry_after=30,
                detail={"upstream": self.service.upstream},
                accept=_header(headers, b"accept") or b"",
            )
            return

        try:
            await self._forward(
                reader,
                writer,
                upstream_reader,
                upstream_writer,
                method,
                path,
                headers,
                body_mode,
                body_length,
                is_upgrade,
                upgrade_header,
                peer_ip,
            )
        finally:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass

    async def _forward(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        method: bytes,
        path: bytes,
        headers: list[tuple[bytes, bytes]],
        body_mode: str,
        body_length: int,
        is_upgrade: bool,
        upgrade_header: bytes | None,
        peer_ip: str,
    ) -> None:
        upstream_writer.write(
            _build_request_head(
                method,
                path,
                headers,
                is_upgrade=is_upgrade,
                has_chunked_body=body_mode == "chunked",
                peer_ip=peer_ip,
            )
        )
        await upstream_writer.drain()

        response_started = False
        try:
            if body_mode != "none":
                await self._relay_body(
                    client_reader, upstream_writer, body_mode, body_length
                )
                await upstream_writer.drain()

            status_line, status, response_headers = await self._read_response_head(
                upstream_reader, self.config.stream_timeout_seconds
            )

            if is_upgrade and status == 101:
                client_writer.write(
                    _build_response_head(status_line, response_headers, is_upgrade=True)
                )
                await client_writer.drain()
                response_started = True
                await self._tunnel(
                    client_reader, client_writer, upstream_reader, upstream_writer
                )
                return

            has_chunked = _is_chunked(response_headers)
            client_writer.write(
                _build_response_head(
                    status_line,
                    response_headers,
                    is_upgrade=False,
                    has_chunked_body=has_chunked,
                )
            )
            await client_writer.drain()
            response_started = True

            response_mode, response_length = _response_body_mode(
                method, status, response_headers
            )
            if response_mode != "none":
                await self._relay_body(
                    upstream_reader, client_writer, response_mode, response_length
                )
                await client_writer.drain()
        except BadRequest as exc:
            log.warning("protocol error while proxying %s: %s", self.name, exc)
            if not response_started:
                await self._send_error(
                    client_writer,
                    502,
                    "upstream_protocol_error",
                    f"{self.name} 返回了无法解析的响应：{exc}",
                )
        except (ConnectionResetError, BrokenPipeError) as exc:
            log.debug("connection closed while proxying %s: %s", self.name, exc)
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
            log.warning("upstream I/O error while proxying %s: %s", self.name, exc)
            if not response_started:
                await self._send_error(
                    client_writer,
                    504,
                    "upstream_timeout",
                    f"{self.name} 的响应超时或中断：{exc}",
                    retry_after=30,
                )

    async def _read_response_head(
        self, reader: asyncio.StreamReader, timeout: float
    ) -> tuple[bytes, int, list[tuple[bytes, bytes]]]:
        while True:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
            except asyncio.LimitOverrunError:
                raise BadRequest("upstream response headers are too large") from None
            lines = raw[:-4].split(b"\r\n")
            parts = lines[0].split(b" ", 2)
            if len(parts) < 2:
                raise BadRequest("malformed upstream status line")
            try:
                status = int(parts[1])
            except ValueError:
                raise BadRequest("malformed upstream status code") from None
            headers = _parse_headers(lines)
            if 100 <= status < 200 and status != 101:
                continue
            return lines[0], status, headers

    async def _relay_body(
        self,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        mode: str,
        length: int,
    ) -> None:
        timeout = self.config.stream_timeout_seconds
        if mode == "none":
            return
        if mode == "length":
            await self._copy_exact(src, dst, length, timeout)
            return
        if mode == "chunked":
            await self._copy_chunked(src, dst, timeout)
            return
        # eof: read until the upstream closes (HTTP/1.0 or Connection: close)
        while True:
            chunk = await asyncio.wait_for(src.read(CHUNK), timeout)
            if not chunk:
                return
            dst.write(chunk)
            await dst.drain()

    async def _copy_exact(
        self,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        length: int,
        timeout: float,
    ) -> None:
        remaining = length
        while remaining > 0:
            chunk = await asyncio.wait_for(src.read(min(CHUNK, remaining)), timeout)
            if not chunk:
                raise BadRequest("connection closed in the middle of a body")
            dst.write(chunk)
            remaining -= len(chunk)
            await dst.drain()

    async def _copy_chunked(
        self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, timeout: float
    ) -> None:
        while True:
            line = await asyncio.wait_for(src.readuntil(b"\r\n"), timeout)
            dst.write(line)
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError:
                raise BadRequest(f"invalid chunk size {size_text!r}") from None
            if size == 0:
                while True:
                    trailer = await asyncio.wait_for(src.readuntil(b"\r\n"), timeout)
                    dst.write(trailer)
                    if trailer in (b"\r\n", b"\n"):
                        break
                await dst.drain()
                return
            await self._copy_exact(src, dst, size, timeout)
            crlf = await asyncio.wait_for(src.readexactly(2), timeout)
            if crlf != b"\r\n":
                raise BadRequest("malformed chunk terminator")
            dst.write(crlf)
            await dst.drain()

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            parser = FrameParser(lambda: self.manager.note_frame(self.name))
            try:
                while True:
                    data = await src.read(CHUNK)
                    if not data:
                        break
                    parser.feed(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                try:
                    dst.write_eof()
                except (OSError, RuntimeError):
                    pass

        tasks = [
            asyncio.create_task(pump(client_reader, upstream_writer)),
            asyncio.create_task(pump(upstream_reader, client_writer)),
        ]
        try:
            _done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    def _ignores_activity(self, path: str) -> bool:
        for prefix in self.service.ignore_activity_paths:
            if path == prefix or path.startswith(prefix + "?") or path.startswith(prefix + "/"):
                return True
        return False

    # ------------------------------------------------------------- error pages

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
        detail: dict | None = None,
        accept: bytes = b"",
    ) -> None:
        retry = ""
        if retry_after is not None:
            seconds = max(1, math.ceil(retry_after))
            retry = f"Retry-After: {seconds}\r\n"
        else:
            seconds = 0

        if b"text/html" in accept.lower():
            content_type = "text/html; charset=utf-8"
            title = {
                "cooldown": "服务冷却中",
                "busy": "正在切换服务",
                "switch_failed": "服务启动失败",
                "inactive": "服务未运行",
                "upstream_unreachable": "服务暂时不可达",
                "bad_request": "请求无效",
                "relay_error": "中转服务错误",
            }.get(code, "服务暂不可用")
            refresh = f'<meta http-equiv="refresh" content="{max(seconds, 5)}">' if seconds else ""
            body = (
                "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                f"<title>{title}</title>{refresh}"
                "<style>body{font-family:system-ui,sans-serif;background:#101417;color:#e6edf3;"
                "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
                "main{max-width:42rem;padding:2rem}h1{font-size:1.5rem;margin:0 0 1rem}"
                "p{line-height:1.7;color:#a9b4c0}.meta{margin-top:1.5rem;font-size:.85rem;color:#6e7a86}"
                "</style></head><body><main>"
                f"<h1>{title}</h1><p>{html.escape(message)}</p>"
                f"<p class=\"meta\">服务：{html.escape(self.name)} · HTTP {status} · {html.escape(code)}</p>"
                "</main></body></html>"
            ).encode("utf-8")
        else:
            content_type = "application/json; charset=utf-8"
            payload = {"error": code, "message": message, "service": self.name}
            if detail:
                payload.update(detail)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        head = (
            f"HTTP/1.1 {status} {REASON.get(status, 'Error')}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            f"{retry}"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            writer.write(head + body)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass


# --------------------------------------------------------------------- helpers


def _parse_headers(lines: list[bytes]) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            raise BadRequest("obsolete header folding is not supported")
        name, sep, value = line.partition(b":")
        if not sep or not name:
            raise BadRequest("malformed header")
        headers.append((name, value.strip()))
    return headers


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    for header_name, value in headers:
        if header_name.lower() == name:
            return value
    return None


def _headers_all(headers: list[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    return [value for header_name, value in headers if header_name.lower() == name]


def _normalize_target(target: bytes) -> bytes:
    lowered = target.lower()
    if lowered.startswith(b"http://") or lowered.startswith(b"https://"):
        parsed = urlsplit(target.decode("latin-1"))
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return path.encode("latin-1")
    return target


def _request_body_mode(headers: list[tuple[bytes, bytes]]) -> tuple[str, int]:
    transfer_encoding = _header(headers, b"transfer-encoding")
    if transfer_encoding:
        if b"chunked" not in transfer_encoding.lower():
            raise BadRequest("unsupported Transfer-Encoding")
        return "chunked", 0
    lengths = _headers_all(headers, b"content-length")
    if not lengths:
        return "none", 0
    values = set()
    for raw in lengths:
        try:
            value = int(raw)
        except ValueError:
            raise BadRequest("invalid Content-Length") from None
        if value < 0:
            raise BadRequest("invalid Content-Length")
        values.add(value)
    if len(values) != 1:
        raise BadRequest("conflicting Content-Length headers")
    return "length", values.pop()


def _is_chunked(headers: list[tuple[bytes, bytes]]) -> bool:
    transfer_encoding = _header(headers, b"transfer-encoding")
    return bool(transfer_encoding) and b"chunked" in transfer_encoding.lower()


def _response_body_mode(
    method: bytes, status: int, headers: list[tuple[bytes, bytes]]
) -> tuple[str, int]:
    if method == b"HEAD" or status in (204, 304) or 100 <= status < 200:
        return "none", 0
    if _is_chunked(headers):
        return "chunked", 0
    lengths = _headers_all(headers, b"content-length")
    if lengths:
        try:
            return "length", int(lengths[0])
        except ValueError:
            return "eof", 0
    return "eof", 0


def _build_request_head(
    method: bytes,
    path: bytes,
    headers: list[tuple[bytes, bytes]],
    *,
    is_upgrade: bool,
    has_chunked_body: bool,
    peer_ip: str,
) -> bytes:
    lines = [method + b" " + path + b" HTTP/1.1"]
    host_seen = False
    for name, value in headers:
        lowered = name.lower()
        if lowered in (b"proxy-connection", b"keep-alive", b"te", b"trailer", b"expect"):
            continue
        if lowered == b"connection":
            continue
        if lowered == b"upgrade":
            if is_upgrade:
                lines.append(b"Upgrade: " + value)
            continue
        if lowered == b"content-length" and has_chunked_body:
            continue
        if lowered == b"host":
            host_seen = True
        lines.append(name + b": " + value)
    if not host_seen:
        lines.append(b"Host: relay")
    lines.append(b"Connection: Upgrade" if is_upgrade else b"Connection: close")
    if peer_ip:
        lines.append(b"X-Forwarded-For: " + peer_ip.encode("latin-1", "replace"))
    lines.append(b"X-Forwarded-Proto: http")
    return b"\r\n".join(lines) + b"\r\n\r\n"


def _build_response_head(
    status_line: bytes,
    headers: list[tuple[bytes, bytes]],
    *,
    is_upgrade: bool,
    has_chunked_body: bool = False,
) -> bytes:
    lines = [status_line]
    upgrade_seen = False
    for name, value in headers:
        lowered = name.lower()
        if lowered in (b"connection", b"keep-alive", b"proxy-connection"):
            continue
        if lowered == b"content-length" and has_chunked_body:
            continue
        if lowered == b"upgrade":
            upgrade_seen = True
        lines.append(name + b": " + value)
    if is_upgrade:
        lines.append(b"Connection: Upgrade")
        if not upgrade_seen:
            lines.append(b"Upgrade: websocket")
    else:
        lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"
