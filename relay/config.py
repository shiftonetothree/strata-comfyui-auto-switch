"""Configuration loading and validation for the auto-switch relay."""

from __future__ import annotations

import dataclasses
import pathlib
import tomllib
from typing import Any

DEFAULT_CONFIG_NAME = "config.toml"


class ConfigError(ValueError):
    """Raised when the configuration file is missing or invalid."""


@dataclasses.dataclass(frozen=True)
class ServiceConfig:
    """One service the relay can activate.

    ``listen_port`` is the public port the relay binds; ``upstream`` is where the
    service actually listens (loopback-only, moved out of the way in compose).
    """

    name: str
    container: str
    compose_file: pathlib.Path
    compose_dir: pathlib.Path
    service: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    health_path: str
    health_json: dict[str, Any]
    stop_timeout_seconds: float
    startup_timeout_seconds: float
    # Per-service bind address. None means "use relay.bind_host". The ComfyUI
    # listener uses 127.0.0.1 because the public entry is run_comfyui_proxy.sh
    # (port 8189), which forwards into this listener.
    bind_host: str | None = None
    # Path prefixes that are health/status polling, not real use. They never
    # trigger a switch and never refresh the cooldown, so an idle UI polling
    # /health or /system_stats does not keep a service "in use" forever.
    ignore_activity_paths: tuple[str, ...] = ()

    @property
    def upstream(self) -> str:
        return f"{self.upstream_host}:{self.upstream_port}"


@dataclasses.dataclass(frozen=True)
class RelayConfig:
    path: pathlib.Path
    bind_host: str
    admin_host: str
    admin_port: int
    default_service: str | None
    cooldown_seconds: float
    request_timeout_seconds: float
    health_interval_seconds: float
    health_timeout_seconds: float
    stream_timeout_seconds: float
    startup_reconcile: bool
    auto_restart_active: bool
    watchdog_interval_seconds: float
    state_file: pathlib.Path
    log_file: pathlib.Path
    log_level: str
    services: dict[str, ServiceConfig]


# --------------------------------------------------------------------------- helpers


def _as_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{what} must be a non-empty string")
    return value


def _as_int(value: Any, what: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{what} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{what} must be between {minimum} and {maximum}")
    return value


def _as_float(value: Any, what: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{what} must be a number")
    result = float(value)
    if result < minimum:
        raise ConfigError(f"{what} must be >= {minimum}")
    return result


def _as_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{what} must be a boolean")
    return value


def _parse_endpoint(value: str, what: str) -> tuple[str, int]:
    """Split ``host:port`` (IPv6 hosts must be bracketed)."""
    text = value.strip()
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]:")
        if not sep:
            raise ConfigError(f"{what} must look like host:port (got {value!r})")
        port_text = rest
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep:
            raise ConfigError(f"{what} must look like host:port (got {value!r})")
    if not host:
        raise ConfigError(f"{what} has an empty host (got {value!r})")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigError(f"{what} has a non-numeric port (got {value!r})") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"{what} port must be between 1 and 65535 (got {port})")
    return host, port


def _resolve_path(base: pathlib.Path, value: Any, what: str) -> pathlib.Path:
    path = pathlib.Path(_as_str(value, what))
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _load_service(base: pathlib.Path, name: str, raw: Any) -> ServiceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"services.{name} must be a table")

    where = f"services.{name}"
    container = _as_str(raw.get("container"), f"{where}.container")
    service = _as_str(raw.get("service"), f"{where}.service")
    listen_port = _as_int(raw.get("listen_port"), f"{where}.listen_port", minimum=1, maximum=65535)
    upstream_host, upstream_port = _parse_endpoint(
        _as_str(raw.get("upstream"), f"{where}.upstream"), f"{where}.upstream"
    )
    if upstream_port == listen_port:
        raise ConfigError(
            f"{where}: upstream port {upstream_port} collides with the relay's listen_port"
        )

    compose_file = _resolve_path(base, raw.get("compose_file"), f"{where}.compose_file")
    if not compose_file.exists():
        raise ConfigError(f"{where}.compose_file does not exist: {compose_file}")

    compose_dir_raw = raw.get("compose_dir")
    if compose_dir_raw is None:
        compose_dir = compose_file.parent
    else:
        compose_dir = _resolve_path(base, compose_dir_raw, f"{where}.compose_dir")
    if not compose_dir.is_dir():
        raise ConfigError(f"{where}.compose_dir is not a directory: {compose_dir}")

    bind_host_raw = raw.get("bind_host")
    bind_host = (
        _as_str(bind_host_raw, f"{where}.bind_host") if bind_host_raw is not None else None
    )

    ignore_raw = raw.get("ignore_activity_paths", [])
    if not isinstance(ignore_raw, list) or not all(
        isinstance(item, str) and item.startswith("/") for item in ignore_raw
    ):
        raise ConfigError(
            f"{where}.ignore_activity_paths must be a list of paths starting with '/'"
        )
    ignore_activity_paths = tuple(ignore_raw)

    health_path = _as_str(raw.get("health_path", "/"), f"{where}.health_path")
    if not health_path.startswith("/"):
        raise ConfigError(f"{where}.health_path must start with '/'")

    health_json = raw.get("health_json", {})
    if not isinstance(health_json, dict):
        raise ConfigError(f"{where}.health_json must be a table")

    return ServiceConfig(
        name=name,
        container=container,
        compose_file=compose_file,
        compose_dir=compose_dir,
        service=service,
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        health_path=health_path,
        health_json=dict(health_json),
        stop_timeout_seconds=_as_float(
            raw.get("stop_timeout_seconds", 120), f"{where}.stop_timeout_seconds", minimum=1.0
        ),
        startup_timeout_seconds=_as_float(
            raw.get("startup_timeout_seconds", 300),
            f"{where}.startup_timeout_seconds",
            minimum=1.0,
        ),
        bind_host=bind_host,
        ignore_activity_paths=ignore_activity_paths,
    )


def load_config(path: str | pathlib.Path) -> RelayConfig:
    """Load and validate ``config.toml``.

    Relative paths inside the file are resolved against the directory that holds
    the file, so the project can be moved as a whole.
    """
    config_path = pathlib.Path(path).resolve()
    if not config_path.is_file():
        raise ConfigError(f"configuration file not found: {config_path}")

    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top level must be a table")

    relay_raw = raw.get("relay", {})
    if not isinstance(relay_raw, dict):
        raise ConfigError("[relay] must be a table")

    base = config_path.parent
    services_raw = raw.get("services")
    if not isinstance(services_raw, dict) or not services_raw:
        raise ConfigError("at least one [services.<name>] table is required")

    services = {
        name: _load_service(base, name, service_raw)
        for name, service_raw in services_raw.items()
    }

    bind_host = _as_str(relay_raw.get("bind_host", "0.0.0.0"), "relay.bind_host")
    admin_host = _as_str(relay_raw.get("admin_host", "127.0.0.1"), "relay.admin_host")
    admin_port = _as_int(relay_raw.get("admin_port", 9099), "relay.admin_port", minimum=1, maximum=65535)

    listen_ports: dict[int, str] = {}
    for name, service in services.items():
        other = listen_ports.get(service.listen_port)
        if other is not None:
            raise ConfigError(
                f"services.{name}.listen_port {service.listen_port} is already used by services.{other}"
            )
        listen_ports[service.listen_port] = name
        if service.listen_port == admin_port:
            raise ConfigError(
                f"services.{name}.listen_port {service.listen_port} collides with relay.admin_port"
            )

    default_service = relay_raw.get("default_service")
    if default_service is not None:
        default_service = _as_str(default_service, "relay.default_service")
        if default_service not in services:
            raise ConfigError(
                f"relay.default_service {default_service!r} is not one of {sorted(services)}"
            )

    return RelayConfig(
        path=config_path,
        bind_host=bind_host,
        admin_host=admin_host,
        admin_port=admin_port,
        default_service=default_service,
        cooldown_seconds=_as_float(
            relay_raw.get("cooldown_seconds", 120), "relay.cooldown_seconds", minimum=0.0
        ),
        request_timeout_seconds=_as_float(
            relay_raw.get("request_timeout_seconds", 600),
            "relay.request_timeout_seconds",
            minimum=1.0,
        ),
        health_interval_seconds=_as_float(
            relay_raw.get("health_interval_seconds", 2),
            "relay.health_interval_seconds",
            minimum=0.1,
        ),
        health_timeout_seconds=_as_float(
            relay_raw.get("health_timeout_seconds", 3),
            "relay.health_timeout_seconds",
            minimum=0.1,
        ),
        stream_timeout_seconds=_as_float(
            relay_raw.get("stream_timeout_seconds", 900),
            "relay.stream_timeout_seconds",
            minimum=1.0,
        ),
        startup_reconcile=_as_bool(
            relay_raw.get("startup_reconcile", True), "relay.startup_reconcile"
        ),
        auto_restart_active=_as_bool(
            relay_raw.get("auto_restart_active", True), "relay.auto_restart_active"
        ),
        watchdog_interval_seconds=_as_float(
            relay_raw.get("watchdog_interval_seconds", 30),
            "relay.watchdog_interval_seconds",
            minimum=1.0,
        ),
        state_file=_resolve_path(base, relay_raw.get("state_file", "run/state.json"), "relay.state_file"),
        log_file=_resolve_path(base, relay_raw.get("log_file", "logs/relay.log"), "relay.log_file"),
        log_level=_as_str(relay_raw.get("log_level", "INFO"), "relay.log_level").upper(),
        services=services,
    )


def describe(config: RelayConfig) -> str:
    """A short human-readable summary used by ``relay.py --check``."""
    lines = [
        f"config:        {config.path}",
        f"bind:          {config.bind_host}",
        f"admin:         http://{config.admin_host}:{config.admin_port}",
        f"default:       {config.default_service or '(none)'}",
        f"cooldown:      {config.cooldown_seconds:g}s",
    ]
    for name, service in config.services.items():
        host = service.bind_host or config.bind_host
        lines.append(
            f"service {name}: listen {host}:{service.listen_port} -> {service.upstream} "
            f"(container {service.container})"
        )
    return "\n".join(lines)
