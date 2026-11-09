"""Thin async wrapper around the docker CLI.

The relay deliberately uses ``docker compose`` rather than the docker API: the
two services are compose projects, and ``compose up -d`` also applies compose-file
changes (such as the port move) that a plain ``docker start`` would not.
"""

from __future__ import annotations

import asyncio
import logging

from .config import ServiceConfig

log = logging.getLogger("relay.docker")


class DockerError(RuntimeError):
    """A docker/compose command failed."""


class Docker:
    def __init__(self, binary: str = "docker", default_timeout: float = 60.0):
        self.binary = binary
        self.default_timeout = default_timeout

    async def _run(
        self,
        *args: str,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        timeout = self.default_timeout if timeout is None else timeout
        cmd = (self.binary, *args)
        log.debug("running: %s (cwd=%s, timeout=%ss)", " ".join(cmd), cwd, timeout)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise DockerError(f"{self.binary!r} was not found in PATH") from exc
        except OSError as exc:
            raise DockerError(f"could not run {self.binary!r}: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise DockerError(
                f"`{' '.join(cmd)}` did not finish within {timeout:g}s"
            ) from None

        out = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            detail = err or out or f"exit code {proc.returncode}"
            raise DockerError(f"`{' '.join(cmd)}` failed: {detail}")
        return out, err

    async def inspect_running(self, container: str) -> bool | None:
        """True if the container is running, False if stopped, None if it does not exist."""
        try:
            out, _ = await self._run(
                "inspect", "-f", "{{.State.Running}}", container, timeout=10
            )
        except DockerError as exc:
            message = str(exc).lower()
            if "no such" in message or "not found" in message or "no such container" in message:
                return None
            raise
        return out.strip().lower() == "true"

    async def stop_service(self, service: ServiceConfig) -> None:
        """Stop a compose service, honouring its stop grace period.

        ``docker compose stop`` sends SIGTERM and waits up to the service's
        ``stop_grace_period`` before SIGKILL; Strata uses its 2 minute grace to
        unload the model cleanly.
        """
        running = await self.inspect_running(service.container)
        if not running:
            log.info("container %s is not running; nothing to stop", service.container)
            return
        await self._run(
            "compose",
            "-f",
            str(service.compose_file),
            "stop",
            service.service,
            cwd=str(service.compose_dir),
            timeout=service.stop_timeout_seconds + 60,
        )

    async def start_service(self, service: ServiceConfig) -> None:
        """Start (or recreate) a compose service and return once docker reports it running."""
        await self._run(
            "compose",
            "-f",
            str(service.compose_file),
            "up",
            "-d",
            service.service,
            cwd=str(service.compose_dir),
            timeout=max(120.0, service.startup_timeout_seconds),
        )

    async def logs_tail(self, container: str, lines: int = 25) -> str:
        try:
            out, err = await self._run(
                "logs", "--tail", str(lines), container, timeout=15
            )
        except DockerError as exc:
            return f"(could not read logs: {exc})"
        return (out + ("\n" + err if err else "")).strip()
