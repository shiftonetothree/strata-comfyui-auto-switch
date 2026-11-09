"""Tests for config.toml parsing and validation."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from relay.config import ConfigError, describe, load_config


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.dir = Path(self._tempdir.name)
        (self.dir / "compose.yml").write_text("services: {}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def write_config(self, text: str) -> Path:
        path = self.dir / "config.toml"
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def base_config(self, extra: str = "") -> str:
        return f"""
        [relay]
        admin_port = 9099
        default_service = "a"

        [services.a]
        container = "a-container"
        service = "a"
        compose_file = "compose.yml"
        listen_port = 8188
        upstream = "127.0.0.1:18188"
        health_path = "/health"
        {extra}

        [services.b]
        container = "b-container"
        service = "b"
        compose_file = "compose.yml"
        listen_port = 8080
        upstream = "[::1]:18080"
        health_json = {{ loaded = true }}
        """

    def test_valid_config_resolves_relative_paths(self):
        config = load_config(self.write_config(self.base_config()))

        self.assertEqual(config.default_service, "a")
        self.assertEqual(config.cooldown_seconds, 120)
        self.assertEqual(config.admin_port, 9099)
        self.assertEqual(config.services["a"].compose_file, self.dir / "compose.yml")
        self.assertEqual(config.services["a"].compose_dir, self.dir)
        self.assertEqual(config.services["b"].upstream_host, "::1")
        self.assertEqual(config.services["b"].upstream_port, 18080)
        self.assertEqual(config.services["b"].health_json, {"loaded": True})
        self.assertIn("service a", describe(config))

    def test_per_service_bind_host(self):
        text = self.base_config().replace(
            "        listen_port = 8188",
            '        bind_host = "127.0.0.1"\n        listen_port = 8188',
        )
        config = load_config(self.write_config(text))
        self.assertEqual(config.services["a"].bind_host, "127.0.0.1")
        self.assertIsNone(config.services["b"].bind_host)

    def test_ignore_activity_paths(self):
        text = self.base_config().replace(
            "        listen_port = 8188",
            '        ignore_activity_paths = ["/health", "/system_stats"]\n        listen_port = 8188',
        )
        config = load_config(self.write_config(text))
        self.assertEqual(
            config.services["a"].ignore_activity_paths,
            ("/health", "/system_stats"),
        )
        self.assertEqual(config.services["b"].ignore_activity_paths, ())

    def test_invalid_ignore_activity_paths_rejected(self):
        text = self.base_config().replace(
            "        listen_port = 8188",
            '        ignore_activity_paths = ["health"]\n        listen_port = 8188',
        )
        with self.assertRaisesRegex(ConfigError, "ignore_activity_paths"):
            load_config(self.write_config(text))

    def test_duplicate_listen_port_is_rejected(self):
        text = self.base_config().replace("listen_port = 8080", "listen_port = 8188")
        with self.assertRaisesRegex(ConfigError, "already used"):
            load_config(self.write_config(text))

    def test_unknown_default_service_is_rejected(self):
        text = self.base_config().replace('default_service = "a"', 'default_service = "c"')
        with self.assertRaisesRegex(ConfigError, "not one of"):
            load_config(self.write_config(text))

    def test_upstream_must_differ_from_listen_port(self):
        text = self.base_config().replace('"127.0.0.1:18188"', '"127.0.0.1:8188"')
        with self.assertRaisesRegex(ConfigError, "collides"):
            load_config(self.write_config(text))

    def test_missing_compose_file_is_rejected(self):
        text = self.base_config().replace('compose_file = "compose.yml"', 'compose_file = "nope.yml"', 1)
        with self.assertRaisesRegex(ConfigError, "does not exist"):
            load_config(self.write_config(text))

    def test_missing_services_is_rejected(self):
        text = """
        [relay]
        admin_port = 9099
        """
        with self.assertRaisesRegex(ConfigError, "at least one"):
            load_config(self.write_config(text))

    def test_bad_cooldown_is_rejected(self):
        text = self.base_config().replace(
            "admin_port = 9099", "admin_port = 9099\ncooldown_seconds = -5"
        )
        with self.assertRaisesRegex(ConfigError, "cooldown_seconds"):
            load_config(self.write_config(text))

    def test_admin_port_collision_is_rejected(self):
        text = self.base_config().replace("admin_port = 9099", "admin_port = 8188")
        with self.assertRaisesRegex(ConfigError, "admin_port"):
            load_config(self.write_config(text))

    def test_missing_file_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "not found"):
            load_config(self.dir / "missing.toml")


if __name__ == "__main__":
    unittest.main()
