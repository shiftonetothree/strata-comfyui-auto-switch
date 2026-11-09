"""Shared fakes for the relay tests."""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from typing import Any

from relay.config import RelayConfig, ServiceConfig


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeDocker:
    """In-memory stand-in for relay.docker.Docker."""

    def __init__(self, running: dict[str, bool] | None = None):
        self.running = dict(running or {})
        self.calls: list[tuple[str, str]] = []

    async def inspect_running(self, container: str) -> bool | None:
        return self.running.get(container, False)

    async def stop_service(self, service: ServiceConfig) -> None:
        self.calls.append(("stop", service.name))
        self.running[service.container] = False

    async def start_service(self, service: ServiceConfig) -> None:
        self.calls.append(("start", service.name))
        self.running[service.container] = True

    async def logs_tail(self, container: str, lines: int = 25) -> str:
        return "(fake logs)"


class BlockingDocker(FakeDocker):
    """start_service blocks until ``release`` is set (to test concurrent switches)."""

    def __init__(self, running: dict[str, bool] | None = None):
        super().__init__(running)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def start_service(self, service: ServiceConfig) -> None:
        self.calls.append(("start", service.name))
        self.started.set()
        await self.release.wait()
        self.running[service.container] = True


class FailingStopDocker(FakeDocker):
    async def stop_service(self, service: ServiceConfig) -> None:
        self.calls.append(("stop", service.name))
        raise RuntimeError("docker stop exploded")


class Prober:
    """Injected health prober: always healthy unless configured otherwise."""

    def __init__(self, healthy: bool = True):
        self.healthy = healthy
        self.checked: list[str] = []

    async def __call__(self, service: ServiceConfig) -> bool:
        self.checked.append(service.name)
        return self.healthy


def make_service(
    directory: pathlib.Path,
    name: str,
    *,
    container: str | None = None,
    listen_port: int = 8000,
    upstream_port: int = 18000,
    startup_timeout: float = 1.0,
    stop_timeout: float = 1.0,
    health_json: dict[str, Any] | None = None,
    bind_host: str | None = None,
) -> ServiceConfig:
    compose = directory / f"{name}.compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    return ServiceConfig(
        name=name,
        container=container or f"{name}-container",
        compose_file=compose,
        compose_dir=directory,
        service=name,
        listen_port=listen_port,
        upstream_host="127.0.0.1",
        upstream_port=upstream_port,
        health_path="/health",
        health_json=dict(health_json or {}),
        stop_timeout_seconds=stop_timeout,
        startup_timeout_seconds=startup_timeout,
        bind_host=bind_host,
    )


def make_config(
    directory: pathlib.Path,
    *,
    default_service: str | None = "comfyui",
    startup_mode: str = "default",
    cooldown: float = 120.0,
    startup_reconcile: bool = False,
    auto_restart_active: bool = False,
    request_timeout: float = 5.0,
    health_interval: float = 0.01,
) -> RelayConfig:
    services = {
        "comfyui": make_service(
            directory,
            "comfyui",
            listen_port=8188,
            upstream_port=18188,
            startup_timeout=0.25,
        ),
        "strata": make_service(
            directory,
            "strata",
            listen_port=8080,
            upstream_port=18080,
            startup_timeout=0.25,
        ),
    }
    return RelayConfig(
        path=directory / "config.toml",
        bind_host="127.0.0.1",
        admin_host="127.0.0.1",
        admin_port=9099,
        default_service=default_service,
        startup_mode=startup_mode,
        cooldown_seconds=cooldown,
        request_timeout_seconds=request_timeout,
        health_interval_seconds=health_interval,
        health_timeout_seconds=0.2,
        stream_timeout_seconds=2.0,
        startup_reconcile=startup_reconcile,
        auto_restart_active=auto_restart_active,
        watchdog_interval_seconds=60.0,
        state_file=directory / "run" / "state.json",
        log_file=directory / "logs" / "relay.log",
        log_level="DEBUG",
        services=services,
    )


class TemporaryProject:
    def __init__(self):
        self._tempdir = tempfile.TemporaryDirectory()

    def __enter__(self) -> pathlib.Path:
        return pathlib.Path(self._tempdir.name)

    def __exit__(self, *exc) -> None:
        self._tempdir.cleanup()
