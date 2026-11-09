"""Tests for the cooldown / switch state machine."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from relay.manager import (
    BusyError,
    CooldownError,
    ServiceManager,
    SwitchFailed,
)

from tests.helpers import (
    BlockingDocker,
    FailingStopDocker,
    FakeClock,
    FakeDocker,
    Prober,
    make_config,
)

WALL = 1_700_000_000.0


class ManagerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.dir = Path(self._tempdir.name)

    async def asyncTearDown(self) -> None:
        self._tempdir.cleanup()

    def make_manager(
        self,
        docker: FakeDocker,
        clock: FakeClock | None = None,
        healthy: bool = True,
        **config_overrides,
    ) -> tuple[ServiceManager, FakeClock, Prober]:
        config = make_config(self.dir, **config_overrides)
        clock = clock or FakeClock()
        prober = Prober(healthy)
        manager = ServiceManager(
            config,
            docker,
            clock=clock,
            wall=lambda: WALL,
            prober=prober,
        )
        return manager, clock, prober

    def set_active(self, manager: ServiceManager, clock: FakeClock, name: str, idle: float):
        manager.active = name
        manager.last_activity[name] = clock.now - idle
        manager.last_activity_wall[name] = WALL - idle

    # ------------------------------------------------------------- cooldown

    async def test_switch_refused_within_cooldown(self):
        docker = FakeDocker({"comfyui-container": True})
        manager, clock, _ = self.make_manager(docker)
        self.set_active(manager, clock, "comfyui", idle=30)

        with self.assertRaises(CooldownError) as raised:
            await manager.ensure_active("strata")

        self.assertAlmostEqual(raised.exception.remaining, 90.0)
        self.assertEqual(docker.calls, [])
        self.assertIsNone(manager.switching_to)

    async def test_switch_allowed_after_cooldown(self):
        docker = FakeDocker({"comfyui-container": True})
        manager, clock, prober = self.make_manager(docker)
        self.set_active(manager, clock, "comfyui", idle=120.5)

        await manager.ensure_active("strata")

        self.assertEqual(manager.active, "strata")
        self.assertEqual(docker.calls, [("stop", "comfyui"), ("start", "strata")])
        self.assertEqual(manager.last_activity["strata"], clock.now)
        self.assertEqual(manager.switch_count, 1)
        self.assertIsNone(manager.switching_to)
        self.assertIn("strata", prober.checked)

        state = json.loads(manager.config.state_file.read_text())
        self.assertEqual(state["active"], "strata")

    async def test_force_ignores_cooldown(self):
        docker = FakeDocker({"comfyui-container": True})
        manager, clock, _ = self.make_manager(docker)
        self.set_active(manager, clock, "comfyui", idle=1)

        await manager.ensure_active("strata", force=True)

        self.assertEqual(manager.active, "strata")

    async def test_note_request_resets_cooldown(self):
        docker = FakeDocker({"comfyui-container": True})
        manager, clock, _ = self.make_manager(docker)
        self.set_active(manager, clock, "comfyui", idle=110)

        manager.note_request("comfyui")
        clock.now += 5

        with self.assertRaises(CooldownError) as raised:
            await manager.ensure_active("strata")
        self.assertAlmostEqual(raised.exception.remaining, 115.0)
        self.assertEqual(manager.request_count, 1)

    # ------------------------------------------------------------- switching

    async def test_concurrent_requests_share_one_switch(self):
        docker = BlockingDocker({"comfyui-container": True})
        manager, clock, _ = self.make_manager(docker)
        self.set_active(manager, clock, "comfyui", idle=200)

        first = asyncio.create_task(manager.ensure_active("strata"))
        await asyncio.wait_for(docker.started.wait(), 2)

        second = asyncio.create_task(manager.ensure_active("strata"))
        await asyncio.sleep(0)
        docker.release.set()

        await asyncio.gather(first, second)
        self.assertEqual(manager.active, "strata")
        self.assertEqual(docker.calls, [("stop", "comfyui"), ("start", "strata")])
        self.assertEqual(manager.switch_count, 1)

    async def test_request_for_other_service_is_busy_during_switch(self):
        docker = BlockingDocker({"comfyui-container": True})
        manager, clock, _ = self.make_manager(docker)
        self.set_active(manager, clock, "comfyui", idle=200)

        switch = asyncio.create_task(manager.ensure_active("strata"))
        await asyncio.wait_for(docker.started.wait(), 2)

        with self.assertRaises(BusyError) as raised:
            await manager.ensure_active("comfyui")
        self.assertEqual(raised.exception.switching_to, "strata")

        docker.release.set()
        await switch
        self.assertEqual(manager.active, "strata")

    async def test_failed_health_leaves_nothing_active(self):
        docker = FakeDocker({"comfyui-container": True})
        manager, clock, _ = self.make_manager(docker, healthy=False)
        self.set_active(manager, clock, "comfyui", idle=200)

        with self.assertRaises(SwitchFailed):
            await manager.ensure_active("strata")

        self.assertIsNone(manager.active)
        self.assertIsNone(manager.switching_to)
        self.assertIn("did not become healthy", manager.last_error)
        self.assertEqual(("start", "strata"), docker.calls[-2])
        self.assertEqual(("stop", "strata"), docker.calls[-1])
        self.assertFalse(docker.running["strata-container"])

    async def test_failed_stop_keeps_previous_service_active(self):
        docker = FailingStopDocker({"comfyui-container": True})
        manager, clock, _ = self.make_manager(docker)
        self.set_active(manager, clock, "comfyui", idle=200)

        with self.assertRaises(SwitchFailed):
            await manager.ensure_active("strata")

        self.assertEqual(manager.active, "comfyui")
        self.assertIsNone(manager.switching_to)
        self.assertNotIn(("start", "strata"), docker.calls)

    async def test_switch_times_out_but_background_task_survives(self):
        docker = BlockingDocker({"comfyui-container": True})
        manager, clock, _ = self.make_manager(docker, request_timeout=0.1)
        self.set_active(manager, clock, "comfyui", idle=200)

        with self.assertRaises(SwitchFailed):
            await manager.ensure_active("strata")

        # The switch itself keeps running; the request only stopped waiting.
        self.assertEqual(manager.switching_to, "strata")
        docker.release.set()
        for _ in range(100):
            if manager.active == "strata":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(manager.active, "strata")

    # ------------------------------------------------------------- startup

    async def test_reconcile_keeps_default_and_stops_the_other(self):
        docker = FakeDocker({"comfyui-container": True, "strata-container": True})
        manager, clock, prober = self.make_manager(
            docker, startup_reconcile=True, default_service="comfyui"
        )

        manager.start_background()
        await asyncio.wait_for(manager._startup_task, 2)

        self.assertEqual(manager.active, "comfyui")
        self.assertEqual(docker.calls, [("stop", "strata")])
        self.assertEqual(manager.last_activity["comfyui"], clock.now)
        self.assertIsNone(manager.switching_to)

    async def test_reconcile_starts_default_when_stopped(self):
        docker = FakeDocker({"comfyui-container": False, "strata-container": True})
        manager, clock, _ = self.make_manager(
            docker, startup_reconcile=True, default_service="comfyui"
        )

        manager.start_background()
        await asyncio.wait_for(manager._startup_task, 2)

        self.assertEqual(manager.active, "comfyui")
        self.assertEqual(docker.calls, [("stop", "strata"), ("start", "comfyui")])

    async def test_reconcile_waits_for_default_to_be_ready(self):
        docker = FakeDocker({"comfyui-container": True, "strata-container": True})
        manager, _, prober = self.make_manager(
            docker, startup_reconcile=True, default_service="comfyui"
        )
        prober.healthy = False

        manager.start_background()
        await asyncio.wait_for(manager._startup_task, 2)

        # Container was already up, so it is kept; health failure is only logged.
        self.assertEqual(manager.active, "comfyui")
        self.assertEqual(docker.calls, [("stop", "strata")])

    async def test_idle_startup_does_not_touch_containers(self):
        docker = FakeDocker({"comfyui-container": True, "strata-container": True})
        manager, clock, _ = self.make_manager(
            docker, startup_mode="idle", startup_reconcile=True
        )
        manager.config.state_file.parent.mkdir(parents=True, exist_ok=True)
        manager.config.state_file.write_text(json.dumps({"active": "comfyui"}))

        await manager.load_state()
        manager.start_background()
        await asyncio.sleep(0.05)

        self.assertIsNone(manager.active)
        self.assertIsNone(manager._startup_task)
        self.assertEqual(docker.calls, [])
        self.assertNotIn("comfyui", manager.last_activity)

    async def test_idle_first_request_stops_manually_started_other(self):
        docker = FakeDocker({"comfyui-container": True, "strata-container": False})
        manager, clock, _ = self.make_manager(docker, startup_mode="idle")

        await manager.ensure_active("strata")

        self.assertEqual(manager.active, "strata")
        self.assertEqual(
            docker.calls, [("stop", "comfyui"), ("start", "strata")]
        )

    async def test_load_state_restores_active_when_reconcile_is_off(self):
        docker = FakeDocker({"strata-container": True})
        config = make_config(self.dir, startup_reconcile=False)
        config.state_file.parent.mkdir(parents=True, exist_ok=True)
        config.state_file.write_text(
            json.dumps({"active": "strata", "switch_count": 7, "request_count": 42})
        )
        manager = ServiceManager(config, docker, clock=FakeClock(), wall=lambda: WALL)

        await manager.load_state()

        self.assertEqual(manager.active, "strata")
        self.assertEqual(manager.switch_count, 7)
        self.assertEqual(manager.request_count, 42)
        self.assertIn("strata", manager.last_activity)

    async def test_ignored_poll_does_not_refresh_activity(self):
        docker = FakeDocker({"comfyui-container": True})
        manager, clock, _ = self.make_manager(docker)
        self.set_active(manager, clock, "comfyui", idle=30)

        manager.note_request("comfyui", method="POST", path="/prompt", peer="192.168.31.41")
        clock.now += 5
        manager.note_ignored(
            "comfyui", method="GET", path="/system_stats", peer="127.0.0.1", active=True
        )
        clock.now += 5

        self.assertEqual(manager.cooldown_remaining(), manager.config.cooldown_seconds - 10)
        status = await manager.status()
        self.assertEqual(status["services"]["comfyui"]["last_request"]["path"], "/prompt")
        self.assertEqual(
            status["services"]["comfyui"]["last_ignored"]["path"], "/system_stats"
        )

    async def test_status_reports_services_and_cooldown(self):
        docker = FakeDocker({"comfyui-container": True})
        manager, clock, prober = self.make_manager(docker)
        self.set_active(manager, clock, "comfyui", idle=30)

        status = await manager.status()

        self.assertEqual(status["active"], "comfyui")
        self.assertEqual(status["services"]["comfyui"]["running"], True)
        self.assertEqual(status["services"]["strata"]["running"], False)
        self.assertEqual(status["cooldown_remaining_seconds"], 90.0)
        self.assertEqual(status["services"]["comfyui"]["idle_seconds"], 30.0)


if __name__ == "__main__":
    unittest.main()
