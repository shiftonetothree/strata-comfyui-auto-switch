"""The small control-plane HTTP server (status + manual switch).

It binds to loopback by default. It is intentionally separate from the service
listeners: nothing is proxied here, so a switch cannot be triggered through a
service port.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from urllib.parse import parse_qs, unquote, urlsplit

from .config import RelayConfig
from .manager import BusyError, CooldownError, ServiceManager, SwitchFailed

log = logging.getLogger("relay.admin")

MAX_HEAD = 64 * 1024


class AdminServer:
    def __init__(self, config: RelayConfig, manager: ServiceManager):
        self.config = config
        self.manager = manager
        self.server: asyncio.AbstractServer | None = None
        self.port = config.admin_port

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle,
            self.config.admin_host,
            self.config.admin_port,
            limit=MAX_HEAD,
            reuse_address=True,
        )
        sockets = self.server.sockets or []
        if sockets:
            self.port = sockets[0].getsockname()[1]
        if self.config.admin_host not in ("127.0.0.1", "::1", "localhost"):
            log.warning(
                "admin API is bound to %s (reachable from the network); "
                "keep admin_host on loopback unless you trust the LAN",
                self.config.admin_host,
            )
        log.info(
            "admin:     listening on http://%s:%s", self.config.admin_host, self.port
        )

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            writer.close()
            return

        lines = raw[:-4].split(b"\r\n")
        if not lines:
            writer.close()
            return
        parts = lines[0].split(b" ")
        if len(parts) < 2:
            await self._json(writer, 400, {"error": "bad_request"})
            return

        method = parts[0].decode("latin-1").upper()
        parsed = urlsplit(parts[1].decode("latin-1"))
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if method == "GET" and path in ("/", "/status"):
                await self._json(writer, 200, await self.manager.status())
            elif method == "GET" and path == "/health":
                await self._json(
                    writer,
                    200,
                    {
                        "status": "ok",
                        "pid": os.getpid(),
                        "active": self.manager.active,
                        "switching_to": self.manager.switching_to,
                    },
                )
            elif path.startswith("/switch/"):
                await self._handle_switch(writer, method, path, query)
            else:
                await self._json(writer, 404, {"error": "not_found", "path": path})
        except Exception as exc:  # keep the admin endpoint from crashing the relay
            log.exception("admin request failed")
            await self._json(writer, 500, {"error": "internal_error", "message": str(exc)})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass

    async def _handle_switch(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        query: dict[str, list[str]],
    ) -> None:
        if method != "POST":
            await self._json(
                writer, 405, {"error": "method_not_allowed", "hint": "use POST"}
            )
            return

        target = unquote(path[len("/switch/") :]).strip("/")
        if target not in self.manager.services:
            await self._json(
                writer,
                404,
                {
                    "error": "unknown_service",
                    "service": target,
                    "known": sorted(self.manager.services),
                },
            )
            return

        force = query.get("force", ["0"])[0].lower() in ("1", "true", "yes")
        try:
            await self.manager.ensure_active(target, force=force)
        except CooldownError as exc:
            await self._json(
                writer,
                409,
                {
                    "error": "cooldown",
                    "message": str(exc),
                    "active": exc.active,
                    "retry_after": round(exc.remaining, 1),
                },
                retry_after=exc.remaining,
            )
            return
        except BusyError as exc:
            await self._json(
                writer,
                409,
                {"error": "busy", "message": str(exc), "switching_to": exc.switching_to},
                retry_after=10,
            )
            return
        except SwitchFailed as exc:
            await self._json(
                writer,
                503,
                {"error": "switch_failed", "message": str(exc), "active": self.manager.active},
                retry_after=30,
            )
            return

        await self._json(writer, 200, await self.manager.status())

    async def _json(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict,
        *,
        retry_after: float | None = None,
    ) -> None:
        body = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        retry = (
            f"Retry-After: {max(1, math.ceil(retry_after))}\r\n"
            if retry_after is not None
            else ""
        )
        head = (
            f"HTTP/1.1 {status} {_reason(status)}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
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


def _reason(status: int) -> str:
    return {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
        409: "Conflict",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status, "Error")
