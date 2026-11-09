"""Command-line entry point for the relay."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import pathlib
import signal
import sys

from .admin import AdminServer
from .config import DEFAULT_CONFIG_NAME, ConfigError, describe, load_config
from .docker import Docker
from .log import setup_logging
from .manager import ServiceManager
from .proxy import ProxyServer

log = logging.getLogger("relay")


def _default_config_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="HTTP/WebSocket relay that keeps one of several GPU services running.",
    )
    parser.add_argument(
        "--config",
        default=str(_default_config_path()),
        help="path to config.toml (default: %(default)s)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the configuration and exit",
    )
    parser.add_argument(
        "--startup-mode",
        choices=("default", "idle"),
        help=(
            "override relay.startup_mode: 'default' keeps the default service, "
            "'idle' starts no container until the first request"
        ),
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.startup_mode and args.startup_mode != config.startup_mode:
        config = dataclasses.replace(config, startup_mode=args.startup_mode)

    if args.check:
        print(describe(config))
        print("configuration is valid")
        return 0

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        return 130
    return 0


async def _run(config) -> None:
    logger = setup_logging(config)
    logger.info(
        "starting relay: %s (cooldown %ss, default %s, startup %s)",
        config.path,
        f"{config.cooldown_seconds:g}",
        config.default_service or "(none)",
        config.startup_mode,
    )

    docker = Docker()
    manager = ServiceManager(config, docker)
    await manager.load_state()

    proxies = [ProxyServer(config, manager, name) for name in config.services]
    admin = AdminServer(config, manager)

    try:
        for proxy in proxies:
            await proxy.start()
    except OSError as exc:
        logger.error(
            "cannot bind a service port (%s). Stop the services first: "
            "docker compose stop in their project directories.",
            exc,
        )
        for proxy in proxies:
            await proxy.close()
        raise

    try:
        await admin.start()
    except OSError as exc:
        logger.error("cannot bind the admin port %s: %s", config.admin_port, exc)
        for proxy in proxies:
            await proxy.close()
        raise

    manager.start_background()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    logger.info("relay is up; admin API: http://%s:%s/status", config.admin_host, admin.port)
    await stop_event.wait()

    logger.info("shutting down relay (managed containers stay as they are)")
    await manager.shutdown()
    for proxy in proxies:
        await proxy.close()
    await admin.close()
    logger.info("relay stopped")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
