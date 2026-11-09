"""The switching state machine.

Invariant: at most one managed service runs at a time.

A request for the inactive service only triggers a switch when the active
service has not received a request for ``cooldown_seconds``. While the cooldown
has not elapsed (or while a switch is already running) such a request is
rejected with a :class:`CooldownError` / :class:`BusyError`; the proxy turns that
into a 503 + ``Retry-After``.

A switch stops the old container, waits for it to really stop, starts the new
one, and waits until its health endpoint reports ready. All requests that arrive
during the switch to the same target wait for the one shared switch task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .config import RelayConfig, ServiceConfig
from .docker import Docker, DockerError

log = logging.getLogger("relay.manager")

Prober = Callable[[ServiceConfig], Awaitable[bool]]


class RelayError(Exception):
    """Base class for expected relay errors."""


class CooldownError(RelayError):
    """The active service was used too recently, so the switch is refused."""

    def __init__(self, active: str, remaining: float):
        self.active = active
        self.remaining = remaining
        super().__init__(
            f"service {active!r} received a request less than the cooldown ago; "
            f"wait {remaining:.0f}s before switching"
        )


class BusyError(RelayError):
    """A switch to a different service is already in progress."""

    def __init__(self, switching_to: str | None):
        self.switching_to = switching_to
        super().__init__(f"a switch to {switching_to!r} is already in progress")


class SwitchFailed(RelayError):
    """The service could not be activated."""


class ServiceManager:
    def __init__(
        self,
        config: RelayConfig,
        docker: Docker,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        prober: Prober | None = None,
    ):
        self.config = config
        self.docker = docker
        self.services: dict[str, ServiceConfig] = dict(config.services)
        self.clock = clock
        self.wall = wall
        self._prober: Prober = prober or self._probe_http

        self.active: str | None = None
        self.switching_to: str | None = None
        self.last_activity: dict[str, float] = {}
        self.last_activity_wall: dict[str, float] = {}
        self.last_request: dict[str, dict[str, str | float]] = {}
        self.last_ignored: dict[str, dict[str, str | float]] = {}
        self.last_error: str | None = None
        self.switch_count = 0
        self.request_count = 0

        self._guard = asyncio.Lock()
        self._switch_task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._started_mono = clock()
        self._closing = False
        self._last_persist = 0.0

    # ------------------------------------------------------------------ state

    async def load_state(self) -> None:
        """Restore counters (and, when reconcile is off, the active service)."""
        path = self.config.state_file
        data: dict[str, Any] = {}
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read state file %s: %s", path, exc)

        self.switch_count = _safe_int(data.get("switch_count"))
        self.request_count = _safe_int(data.get("request_count"))
        self.last_error = data.get("last_error") or None

        if not self.config.startup_reconcile:
            active = data.get("active")
            if active in self.services:
                self.active = active
                self._note_activity(active)
                log.info("restored active service from %s: %s", path, active)
            elif self.config.default_service:
                self.active = self.config.default_service
                self._note_activity(self.active)
            self._persist()

    def start_background(self) -> None:
        if self.config.startup_reconcile:
            self._startup_task = asyncio.create_task(self._reconcile(), name="startup-reconcile")
        if self.config.auto_restart_active:
            self._watchdog_task = asyncio.create_task(self._watchdog(), name="watchdog")

    async def shutdown(self) -> None:
        """Stop background work. Managed containers are intentionally left running."""
        self._closing = True
        for task in (self._watchdog_task, self._startup_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._watchdog_task, self._startup_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._switch_task is not None and not self._switch_task.done():
            # Let an in-flight switch finish rather than leaving a half-stopped machine.
            try:
                await asyncio.wait_for(asyncio.shield(self._switch_task), timeout=120)
            except (TimeoutError, Exception):
                self._switch_task.cancel()
                try:
                    await self._switch_task
                except (asyncio.CancelledError, Exception):
                    pass
        self._persist()

    # ------------------------------------------------------------- requests

    async def ensure_active(self, target: str, *, force: bool = False) -> None:
        """Make ``target`` the active service, switching if the cooldown allows it.

        Raises :class:`CooldownError`, :class:`BusyError` or :class:`SwitchFailed`
        when the request cannot be served.
        """
        if target not in self.services:
            raise KeyError(target)

        startup = self._startup_task
        if startup is not None and not startup.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(startup), self.config.request_timeout_seconds
                )
            except TimeoutError:
                raise SwitchFailed("startup reconciliation did not finish in time") from None

        while True:
            wait_task: asyncio.Task | None = None
            async with self._guard:
                if self.active == target:
                    return

                running = self._switch_task
                if running is not None and not running.done():
                    if self.switching_to == target:
                        wait_task = running
                    else:
                        raise BusyError(self.switching_to)
                else:
                    remaining = self._cooldown_remaining()
                    if self.active is not None and not force and remaining > 0:
                        raise CooldownError(self.active, remaining)
                    self.switching_to = target
                    self.last_error = None
                    self.switch_count += 1
                    self._switch_task = asyncio.create_task(
                        self._switch(target), name=f"switch-to-{target}"
                    )
                    self._switch_task.add_done_callback(_consume_task_exception)
                    wait_task = self._switch_task

            if wait_task is None:  # pragma: no cover - the guard sets one above
                continue

            try:
                await asyncio.wait_for(
                    asyncio.shield(wait_task), self.config.request_timeout_seconds
                )
            except TimeoutError:
                raise SwitchFailed(
                    f"switching to {target!r} did not finish within "
                    f"{self.config.request_timeout_seconds:g}s"
                ) from None

            async with self._guard:
                if self.active == target:
                    return
            raise SwitchFailed(self.last_error or f"failed to activate {target!r}")

    def note_request(
        self,
        name: str,
        *,
        method: str | None = None,
        path: str | None = None,
        peer: str | None = None,
    ) -> None:
        """Record a real client request for a service (marks it as 'in use')."""
        self.request_count += 1
        self._note_activity(name)
        self.last_request[name] = _request_entry(self.wall(), method, path, peer)

    def note_ignored(
        self,
        name: str,
        *,
        method: str | None = None,
        path: str | None = None,
        peer: str | None = None,
        active: bool = False,
    ) -> None:
        """Record a health/status poll that deliberately does not count as use."""
        entry = _request_entry(self.wall(), method, path, peer)
        entry["active"] = active
        self.last_ignored[name] = entry

    def note_frame(self, name: str) -> None:
        """Record WebSocket data traffic, which also means the service is in use."""
        self._note_activity(name)

    def _note_activity(self, name: str) -> None:
        self.last_activity[name] = self.clock()
        self.last_activity_wall[name] = self.wall()

    def cooldown_remaining(self) -> float:
        return self._cooldown_remaining()

    def _cooldown_remaining(self) -> float:
        if self.active is None:
            return 0.0
        last = self.last_activity.get(self.active)
        if last is None:
            return 0.0
        return max(0.0, self.config.cooldown_seconds - (self.clock() - last))

    # --------------------------------------------------------------- switching

    async def _switch(self, target: str) -> None:
        previous = self.active
        started = self.wall()
        log.info(
            "switching %s -> %s (container %s)",
            previous or "(none)",
            target,
            self.services[target].container,
        )

        try:
            if previous is not None and previous != target:
                await self._stop(previous)
            elif previous == target:
                log.info("%s is already the active service; ensuring it is healthy", target)

            async with self._guard:
                self.active = None  # nothing is serving while the target boots

            await self._start(target)

            async with self._guard:
                self.active = target
                self.switching_to = None
                self._note_activity(target)
                self._persist()
            log.info("switched to %s in %.1fs", target, self.wall() - started)
        except asyncio.CancelledError:
            log.warning("switch to %s was cancelled", target)
            async with self._guard:
                self.switching_to = None
            raise
        except Exception as exc:
            self.last_error = f"switch to {target!r} failed: {exc}"
            log.error("%s", self.last_error)

            # Try to keep the "at most one service running" invariant and leave a
            # clean state so the next request can retry.
            remaining: str | None = None
            if previous is not None and previous != target and await self._safe_running(previous):
                remaining = previous  # stopping the old service failed; it is still up
            else:
                await self._safe_stop(target)
            async with self._guard:
                self.active = remaining
                self.switching_to = None
                self._persist()
            raise SwitchFailed(self.last_error) from exc

    async def _stop(self, name: str) -> None:
        service = self.services[name]
        if not await self._safe_running(name):
            log.info("service %s is already stopped", name)
            return
        log.info("stopping %s (this can take up to %ss)", name, service.stop_timeout_seconds)
        started = self.clock()
        await self.docker.stop_service(service)
        if await self._safe_running(name):
            raise RelayError(f"{name} is still running after docker compose stop")
        log.info("stopped %s in %.1fs", name, self.clock() - started)

    async def _start(self, name: str) -> None:
        service = self.services[name]
        running = await self._safe_running(name)
        if not running:
            log.info("starting %s (container %s)", name, service.container)
            await self.docker.start_service(service)
        else:
            log.info("%s container is already running; waiting for it to become ready", name)

        ready = await self._wait_healthy(service)
        if not ready:
            tail = await self.docker.logs_tail(service.container)
            raise RelayError(
                f"{name} did not become healthy within {service.startup_timeout_seconds:g}s "
                f"(health: {service.health_path} {service.health_json or ''})\n{tail}"
            )
        log.info("%s is healthy at %s", name, service.upstream)

    async def _wait_healthy(self, service: ServiceConfig) -> bool:
        # Real time here: this loop waits on docker, not on the cooldown clock
        # (tests inject a frozen clock for the cooldown logic).
        started = time.monotonic()
        deadline = started + service.startup_timeout_seconds
        last_log = 0.0
        while time.monotonic() < deadline:
            running = await self._safe_running(service.name)
            if running is False:
                return False
            if await self._prober(service):
                return True
            now = time.monotonic()
            if now - last_log >= 10:
                log.info(
                    "waiting for %s to become ready (%.0fs)",
                    service.name,
                    now - started,
                )
                last_log = now
            await asyncio.sleep(self.config.health_interval_seconds)
        return False

    async def _safe_running(self, name: str) -> bool:
        try:
            return bool(await self.docker.inspect_running(self.services[name].container))
        except (DockerError, OSError) as exc:
            log.warning("could not inspect container for %s: %s", name, exc)
            return False

    async def _safe_stop(self, name: str) -> None:
        try:
            await self.docker.stop_service(self.services[name])
        except Exception as exc:  # best effort during error handling
            log.warning("could not stop %s while cleaning up: %s", name, exc)

    # ------------------------------------------------------------- background

    async def _reconcile(self) -> None:
        """Startup: keep the configured default service and stop every other one."""
        default = self.config.default_service
        if default is None:
            log.warning("no relay.default_service configured; leaving services untouched")
            return

        self.switching_to = default
        log.info("startup reconcile: default service is %s", default)
        try:
            for name, service in self.services.items():
                if name == default:
                    continue
                if await self._safe_running(name):
                    log.info("startup: stopping non-default service %s", name)
                    await self._stop(name)

            if not await self._safe_running(default):
                await self._start(default)
            else:
                ready = await self._wait_healthy(self.services[default])
                if not ready:
                    log.warning(
                        "startup: %s container is running but its health check is not passing",
                        default,
                    )

            async with self._guard:
                self.active = default
                self._note_activity(default)
                self._persist()
            log.info("startup reconcile done: %s is active", default)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"startup reconcile failed: {exc}"
            log.error("%s", self.last_error)
        finally:
            async with self._guard:
                self.switching_to = None

    async def _watchdog(self) -> None:
        """Restart the active container if it disappears unexpectedly."""
        while not self._closing:
            try:
                await asyncio.sleep(self.config.watchdog_interval_seconds)
            except asyncio.CancelledError:
                return
            try:
                async with self._guard:
                    active = self.active
                    busy = self.switching_to is not None or (
                        self._switch_task is not None and not self._switch_task.done()
                    )
                if active is None or busy:
                    continue

                running = await self._safe_running(active)
                if running is not False:
                    self._persist()
                    continue

                log.warning("active service %s is not running; restarting it", active)
                async with self._guard:
                    if (
                        self._closing
                        or self.active != active
                        or self.switching_to is not None
                        or (self._switch_task is not None and not self._switch_task.done())
                    ):
                        continue
                    self.switching_to = active
                    self.switch_count += 1
                    self._switch_task = asyncio.create_task(
                        self._switch(active), name=f"restart-{active}"
                    )
                    self._switch_task.add_done_callback(_consume_task_exception)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("watchdog iteration failed")

    # ------------------------------------------------------------ health/state

    async def _probe_http(self, service: ServiceConfig) -> bool:
        timeout = self.config.health_timeout_seconds
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    service.upstream_host,
                    service.upstream_port,
                    limit=256 * 1024,
                ),
                timeout,
            )
            request = (
                f"GET {service.health_path} HTTP/1.1\r\n"
                f"Host: {service.upstream_host}:{service.upstream_port}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(request.encode("latin-1"))
            await writer.drain()

            status_line = await asyncio.wait_for(reader.readline(), timeout)
            parts = status_line.split()
            if len(parts) < 2:
                return False
            status = int(parts[1])
            if not 200 <= status < 400:
                return False
            if not service.health_json:
                return True

            # Skip the response headers before reading the (small) body.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout)
                if line in (b"\r\n", b"\n", b""):
                    break
            body = await self._read_probe_body(reader, timeout)
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                return False
            if not isinstance(payload, dict):
                return False
            return all(payload.get(key) == value for key, value in service.health_json.items())
        except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError):
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, asyncio.CancelledError):
                    pass

    @staticmethod
    async def _read_probe_body(reader: asyncio.StreamReader, timeout: float) -> bytes:
        buf = bytearray()
        while len(buf) < 256 * 1024:
            chunk = await asyncio.wait_for(reader.read(65536), timeout)
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    async def status(self) -> dict[str, Any]:
        now = self.clock()
        services: dict[str, Any] = {}
        for name, service in self.services.items():
            running = await self._safe_running(name)
            healthy = False
            if running:
                try:
                    healthy = await asyncio.wait_for(
                        self._prober(service), min(self.config.health_timeout_seconds, 2.0)
                    )
                except (TimeoutError, Exception):
                    healthy = False
            last_wall = self.last_activity_wall.get(name)
            services[name] = {
                "container": service.container,
                "running": running,
                "healthy": healthy,
                "listen_port": service.listen_port,
                "upstream": service.upstream,
                "last_activity": _iso(last_wall) if last_wall else None,
                "last_request": self.last_request.get(name),
                "last_ignored": self.last_ignored.get(name),
                "idle_seconds": (
                    round(now - self.last_activity[name], 1)
                    if name in self.last_activity
                    else None
                ),
            }

        return {
            "active": self.active,
            "switching_to": self.switching_to,
            "default_service": self.config.default_service,
            "cooldown_seconds": self.config.cooldown_seconds,
            "cooldown_remaining_seconds": round(self._cooldown_remaining(), 1),
            "last_error": self.last_error,
            "switch_count": self.switch_count,
            "request_count": self.request_count,
            "uptime_seconds": round(now - self._started_mono, 1),
            "pid": os.getpid(),
            "services": services,
        }

    def _persist(self) -> None:
        path = self.config.state_file
        data = {
            "active": self.active,
            "last_activity": {
                name: value for name, value in self.last_activity_wall.items()
            },
            "switch_count": self.switch_count,
            "request_count": self.request_count,
            "last_error": self.last_error,
            "updated_at": _iso(self.wall()),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            self._last_persist = self.clock()
        except OSError as exc:
            log.warning("could not write state file %s: %s", path, exc)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _request_entry(
    wall: float, method: str | None, path: str | None, peer: str | None
) -> dict[str, str | float]:
    entry: dict[str, str | float] = {"at": _iso(wall)}
    if method is not None:
        entry["method"] = method
    if path is not None:
        entry["path"] = path
    if peer is not None:
        entry["peer"] = peer
    return entry


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _consume_task_exception(task: asyncio.Task) -> None:
    """Mark a background task's exception as retrieved (callers may have timed out)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.debug("background task %s ended with %r", task.get_name(), exc)
