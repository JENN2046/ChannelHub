"""Tests of pilot ownership, forwarding and artifact boundaries, not plugin behavior."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location("devctl", Path(__file__).with_name("devctl.py"))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class PilotBoundaries(unittest.TestCase):
    def test_relay_rejects_foreign_sites_and_path_smuggling(self):
        expected = "127.0.0.1:36123"
        for target in ["/v1/chat/completions", "/__dev/shutdown", "http://evil.invalid/",
                       "/admin_api/channelHub/../other", "/admin_api/channelHub/%2e%2e/",
                       "/admin_api/channelHub/health%3fx=x", "/admin_api/channelHub/\\other"]:
            self.assertFalse(d.relay_allowed("POST", target, expected, None, expected), target)
        for host, origin in [("evil.invalid", None), (expected, "https://evil.invalid"),
                             (expected, "null"), ("localhost:36123", None)]:
            self.assertFalse(d.relay_allowed("GET", "/admin_api/channelHub/health", host, origin, expected))
        self.assertTrue(d.relay_allowed("GET", "/admin_api/channelHub/ui/app.js", expected, None, expected))
        self.assertTrue(d.relay_allowed("PATCH", "/admin_api/channelHub/adapters/synthetic", expected, "http://" + expected, expected))

    def test_relay_network_entry_does_not_accept_forged_host_or_origin(self):
        server = d.make_relay("/nonexistent/backend.sock")
        try:
            import threading
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            client = socket.create_connection(server.server_address, timeout=2)
            client.sendall(b"GET /admin_api/channelHub/health HTTP/1.0\r\nHost: evil.invalid\r\n\r\n")
            self.assertIn(b"403", client.recv(512).split(b"\r\n")[0])
            client.close()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        finally:
            server.server_close()

    def test_forbidden_inputs(self):
        for name in ["/etc/passwd", "../private", "Plugin/ChannelHub/config.env",
                     "dev/.env", "dev/secrets/key.txt", "dev/link.key", "dev/state-private/a",
                     "Plugin/ChannelHub/state/tasks.json", "dev/private.sqlite"]:
            self.assertFalse(d.input_name(name), name)
        self.assertTrue(d.input_name("Plugin/ChannelHub/config.env.example"))
        self.assertFalse(d.input_name("unrelated-project/source.js"))

    def test_symlink_resource_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "real").mkdir()
            (root / "alias").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(d.Refusal):
                d.owned_dir(root / "alias/subdir")
            self.assertFalse((root / "real/subdir").exists())

    def test_live_instance_cannot_be_reset_and_busy_lock_cannot_be_taken(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(d, "STATE", Path(raw)):
            (Path(raw) / "active.json").write_text("{}")
            with self.assertRaises(d.Refusal):
                d.reset()
            with d.lock("instance"):
                with self.assertRaises(d.Refusal):
                    with d.lock("instance"):
                        self.fail("duplicate lock accepted")

    def test_stop_refuses_stale_or_foreign_process_identity(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(d, "STATE", Path(raw)):
            record = {"schema": d.SCHEMA, "uid": os.getuid(), "instance": "a" * 32,
                      "pid": os.getpid(), "process_start": "not-the-running-process"}
            d.write_json(Path(raw) / "active.json", record)
            with patch.object(d.os, "kill") as kill:
                with self.assertRaises(d.Refusal):
                    d.stop()
                kill.assert_not_called()

    def test_start_does_not_lose_previous_stopped_state(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(d, "STATE", Path(raw)):
            (Path(raw) / "last-run.json").write_text("{}")
            with patch.object(d, "new_task") as create:
                with self.assertRaises(d.Refusal):
                    d.start()
                create.assert_not_called()

    def test_stop_and_reset_do_not_need_development_toolchain(self):
        for command in ("stop", "reset"):
            with patch.object(d.sys, "argv", ["devctl.py", command]), \
                    patch.object(d, "tools_ok", side_effect=AssertionError("toolchain should not run")), \
                    patch.object(d, "init_resources"), patch.object(d, command) as action:
                d.main()
                action.assert_called_once()

    def test_reset_cannot_delete_outside_owned_task(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            state.mkdir()
            outside = root / "important"
            outside.mkdir()
            (outside / "keep").write_text("keep")
            with patch.object(d, "STATE", state), patch.object(d, "CACHE", root / "cache"):
                d.write_json(state / "last-run.json", {"status": "STOPPED", "task": "../important"})
                with self.assertRaises(d.Refusal):
                    d.reset()
                self.assertEqual((outside / "keep").read_text(), "keep")

    def test_partial_or_unowned_resources_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw) / "cache"
            state = Path(raw) / "state"
            cache.mkdir(mode=0o700)
            (cache / "other-owner").write_text("preserve")
            with patch.object(d, "CACHE", cache), patch.object(d, "STATE", state):
                with self.assertRaises(d.Refusal):
                    d.init_resources()
                self.assertTrue((cache / "other-owner").exists())

    def test_candidate_rejects_traversal_extra_tampered_and_duplicate_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = root / "source/Plugin/ChannelHub"
            plugin.mkdir(parents=True)
            for i in range(46):
                (plugin / f"{i}.js").write_text("frozen")
            artifact = root / "candidate.zip"

            def make(extra=None, tamper=False):
                with zipfile.ZipFile(artifact, "w") as out:
                    for p in plugin.iterdir():
                        out.writestr("ChannelHub/" + p.name, "changed" if tamper else p.read_text())
                    if extra:
                        out.writestr(extra, "forbidden")

            make()
            self.assertEqual(len(d.validate_zip(artifact, root / "source")), 64)
            for name in ["../escape", "ChannelHub/config.env", "ChannelHub/unexpected.js"]:
                make(extra=name)
                with self.assertRaises(d.Refusal):
                    d.validate_zip(artifact, root / "source")
            make(tamper=True)
            with self.assertRaises(d.Refusal):
                d.validate_zip(artifact, root / "source")
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                make(extra="ChannelHub/1.js")
            with self.assertRaises(d.Refusal):
                d.validate_zip(artifact, root / "source")


if __name__ == "__main__":
    unittest.main(verbosity=2)
