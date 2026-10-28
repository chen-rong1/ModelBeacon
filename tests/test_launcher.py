import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("float_launcher", ROOT / "start.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_exact_current_chat_and_custom_home_are_forwarded_to_launchservices(self):
        environment = {
            "CODEX_THREAD_ID": "test-current-chat",
            "CODEX_HOME": "/tmp/local-codex-home",
            "PASSIVE_MONITOR_PYTHON": "/usr/bin/python3",
        }
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(launcher.platform, "system", return_value="Darwin"), \
             patch.object(launcher, "needs_build", return_value=False), \
             patch.object(launcher.subprocess, "run") as run:
            self.assertEqual(launcher.main([]), 0)
        command = run.call_args.args[0]
        self.assertIn("MODEL_BEACON_SESSION=test-current-chat", command)
        self.assertIn("CODEX_HOME=/tmp/local-codex-home", command)
        self.assertEqual(command[-3:], ["-a", str(launcher.APP_PATH), "modelbeacon://bind?session=test-current-chat"])

    def test_double_click_style_launch_without_chat_does_not_invent_an_id(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(launcher.platform, "system", return_value="Darwin"), \
             patch.object(launcher, "needs_build", return_value=False), \
             patch.object(launcher.subprocess, "run") as run:
            self.assertEqual(launcher.main([]), 0)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", str(launcher.APP_PATH)])

    def test_explicit_binding_is_delivered_as_url_even_if_app_is_running(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "previous-session"}, clear=True), \
             patch.object(launcher.platform, "system", return_value="Darwin"), \
             patch.object(launcher, "needs_build", return_value=False), \
             patch.object(launcher.subprocess, "run") as run:
            self.assertEqual(launcher.main(["--session", "next-session"]), 0)
        self.assertEqual(run.call_args.args[0][-1], "modelbeacon://bind?session=next-session")

    def test_overlong_binding_is_rejected_before_launch(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(launcher.platform, "system", return_value="Darwin"), \
             patch.object(launcher, "needs_build", return_value=False), \
             patch.object(launcher.subprocess, "run") as run:
            self.assertEqual(launcher.main(["--session", "A" * 129]), 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
