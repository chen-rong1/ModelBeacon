import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from monitor_runtime import MonitorController


class FakeMonitor:
    def __init__(self):
        self.session = "A"
        self.status = "ready"

    def bind(self, session):
        self.session = session

    def follow_recent(self):
        self.session = None

    def poll(self):
        return {"session_id": self.session or "recent", "selection_mode": "bound" if self.session else "recent_activity",
                "turn_id": "turn-1", "status": self.status, "context_available": True,
                "selected_model": "gpt-6.1-sol", "selected_effort": "medium", "selected_provider": "openai"}


class FakeEngine:
    def __init__(self):
        self.commands = []
        self.observations = []
        self.results = {}
        self.automatic_enabled = False

    def observe(self, session, **kwargs):
        self.observations.append((session, kwargs))

    def snapshot(self, session):
        return {"session_id": session, "enabled": self.automatic_enabled,
                "available": True, "result": self.results.get(session)}

    def request_manual(self, session):
        self.commands.append(("manual", session))
        return {"accepted": True}

    def set_automatic(self, session, enabled):
        self.commands.append(("auto", session, enabled))
        self.automatic_enabled = enabled
        return {"accepted": True}

    def cancel(self, session):
        self.commands.append(("cancel", session))

    def suspend(self, session):
        self.commands.append(("suspend", session))

    def advance(self):
        pass

    def close(self):
        pass


class ControllerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.monitor, self.engine = FakeMonitor(), FakeEngine()
        self.controller = MonitorController(self.monitor, self.engine)

    def test_late_result_from_a_never_changes_b_or_its_result(self):
        self.controller.poll()
        self.controller.command({"action": "attribute", "session_id": "A"})
        self.controller.command({"action": "bind", "session_id": "B"})
        self.engine.results["A"] = {"outcome": "close"}
        state = self.controller.poll()
        self.assertEqual(state["session_id"], "B")
        self.assertIsNone(state["attribution"]["result"])
        self.assertEqual(state["status"], "ready")
        self.assertEqual([row for row in self.engine.commands if row[0] == "manual"], [("manual", "A")])

    def test_old_click_cannot_operate_on_new_binding(self):
        self.controller.poll()
        self.controller.command({"action": "bind", "session_id": "B"})
        self.controller.poll()
        self.controller.command({"action": "attribute", "session_id": "A"})
        self.assertIn("会话已变化", self.controller.command_error)
        self.assertFalse(any(row[0] == "manual" for row in self.engine.commands))

    def test_session_provider_is_used_for_sampling_without_fabricating_selection(self):
        poll = self.monitor.poll
        self.monitor.poll = lambda: dict(poll(), selected_provider=None, session_provider="openai")
        state = self.controller.poll()
        self.assertIsNone(state["selected_provider"])
        self.assertEqual(self.engine.observations[-1][1]["config"]["provider"], "openai")

    def test_recorded_turn_configuration_can_be_verified_for_sampling_without_choice_event(self):
        poll = self.monitor.poll
        self.monitor.poll = lambda: dict(poll(), selected_model=None, selected_effort=None,
                                        selected_provider=None, configuration_model="gpt-6.1-sol",
                                        configuration_effort="ultra", session_provider="openai")
        state = self.controller.poll()
        self.assertIsNone(state["selected_model"])
        self.assertIsNone(state["selected_effort"])
        config = self.engine.observations[-1][1]["config"]
        self.assertEqual((config["model"], config["effort"], config["provider"]),
                         ("gpt-6.1-sol", "ultra", "openai"))

    def test_recent_activity_only_samples_after_explicit_click_and_binds_that_id(self):
        self.controller.command({"action": "follow_recent"})
        state = self.controller.poll()
        self.assertFalse(any(row[0] == "manual" for row in self.engine.commands))
        self.assertFalse(self.engine.observations[-1][1]["allow_automatic"])
        self.controller.command({"action": "attribute", "session_id": state["session_id"]})
        self.assertEqual(self.monitor.session, state["session_id"])
        self.assertIn(("manual", state["session_id"]), self.engine.commands)
        self.assertTrue(self.engine.observations[-1][1]["normal"])
        self.assertTrue(self.engine.observations[-1][1]["allow_automatic"])
        self.assertTrue(self.engine.observations[-1][1]["context_available"])
        self.controller.command({"action": "set_auto", "session_id": "recent", "enabled": True})
        self.assertIn(("auto", "recent", True), self.engine.commands)

    def test_recent_auto_opt_in_captures_displayed_id_without_manual_request(self):
        self.controller.command({"action": "follow_recent"})
        self.controller.poll()
        self.controller.command({"action": "set_auto", "session_id": "recent", "enabled": True})
        self.assertEqual(self.monitor.session, "recent")
        self.assertIn(("auto", "recent", True), self.engine.commands)
        self.assertFalse(any(row[0] == "manual" for row in self.engine.commands))
        self.assertTrue(self.engine.observations[-1][1]["allow_automatic"])

    def test_no_conversation_offers_explicit_new_session_sampling(self):
        self.monitor.poll = lambda: {"session_id": None, "status": "ready"}
        state = self.controller.poll()
        self.assertTrue(state["attribution"]["manual_available"])
        self.assertTrue(state["attribution"]["requires_new_sample"])
        self.assertFalse(state["attribution"]["auto_available"])
        self.assertEqual(self.engine.commands, [])

    def test_global_auto_remains_visible_without_a_readable_conversation(self):
        self.engine.automatic_enabled = True
        self.monitor.poll = lambda: {"session_id": None, "status": "idle"}
        state = self.controller.poll()
        self.assertTrue(state["attribution"]["enabled"])
        self.assertFalse(state["attribution"]["auto_available"])
        self.assertEqual(self.engine.commands, [])

    def test_disable_global_auto_never_polls_or_advances_before_switching_off(self):
        self.engine.automatic_enabled = True
        self.controller.poll()
        self.monitor.poll = lambda: self.fail("Closing automatic monitoring must not advance pending work")
        self.engine.advance = lambda: self.fail("A closing command must not start a new task")
        self.controller.command({"action": "set_auto", "session_id": "A", "enabled": False})
        self.assertFalse(self.engine.automatic_enabled)
        self.assertEqual(self.engine.commands, [("auto", None, False)])
        self.assertIsNone(self.controller.command_error)

    def test_disable_global_auto_is_available_without_session_or_after_read_error(self):
        for state in ({"session_id": None, "status": "idle"}, {"session_id": "A", "status": "error"}):
            with self.subTest(state=state):
                self.engine.automatic_enabled = True
                self.controller.current = state
                self.controller.command({"action": "set_auto", "session_id": state["session_id"], "enabled": False})
                self.assertFalse(self.engine.automatic_enabled)
                self.assertIsNone(self.controller.command_error)

    def test_new_session_sampling_keeps_monitored_configuration_and_cancel_targets_task_id(self):
        self.engine.request_new_sample = lambda config: {"accepted": True, "session_id": "new-sample-test"}
        snapshot = self.engine.snapshot
        self.engine.snapshot = lambda session: ({"sample_scope": "new_session", "task": {"id": "task-new"},
                                                 "result": {"candidate": "gpt-5.6-sol"}}
                                                if session == "new-sample-test" else snapshot(session))
        self.controller.poll()
        self.controller.command({"action": "sample_new", "model": "gpt-6.1-sol", "provider": "openai", "effort": "ultra"})
        state = self.controller.poll()
        self.assertEqual(state["session_id"], "A")
        self.assertEqual(state["selected_model"], "gpt-6.1-sol")
        self.assertEqual(state["attribution"]["sample_scope"], "new_session")
        self.controller.command({"action": "cancel_attribution", "session_id": None, "task_id": "other-task"})
        self.assertNotIn(("cancel", "new-sample-test"), self.engine.commands)
        self.controller.command({"action": "cancel_attribution", "session_id": None, "task_id": "task-new"})
        self.assertIn(("cancel", "new-sample-test"), self.engine.commands)

    def test_completed_independent_result_never_hides_active_monitored_task(self):
        self.controller._new_sample_id = "new-sample-done"
        self.engine.snapshot = lambda session: ({"session_id": "A", "enabled": True, "task": {"id": "auto-live"}}
                                               if session == "A" else {"result": {"outcome": "close"}, "task": None})
        state = self.controller.poll()
        self.assertEqual(state["attribution"]["task"]["id"], "auto-live")
        self.assertTrue(state["attribution"]["enabled"])
        self.assertIsNone(self.controller._new_sample_id)

    def test_independent_result_does_not_replace_monitored_automatic_toggle(self):
        self.controller._new_sample_id = "new-sample-done"
        self.engine.snapshot = lambda session: ({"session_id": "A", "enabled": True, "task": None}
                                               if session == "A" else {"sample_scope": "new_session", "enabled": False,
                                                                          "auto_available": False, "result": {"outcome": "close"}})
        state = self.controller.poll()
        self.assertEqual(state["attribution"]["sample_scope"], "new_session")
        self.assertTrue(state["attribution"]["enabled"])
        self.assertTrue(state["attribution"]["auto_available"])

    def test_independent_result_keeps_monitored_automatic_tool_progress(self):
        self.controller._new_sample_id = "new-sample-done"
        self.engine.snapshot = lambda session: ({"session_id": "A", "enabled": True, "task": None,
                                                "tools": 8, "tool_threshold": 23, "pending": False, "completed_at": 1000}
                                               if session == "A" else {"sample_scope": "new_session", "enabled": False,
                                                                          "tools": 0, "result": {"outcome": "close"}})
        state = self.controller.poll()
        self.assertEqual(state["attribution"]["auto_progress"], {
            "tools": 8, "threshold": 23, "pending": False, "last_completed_at": 1000})

    def test_resumed_rollout_segment_for_same_session_starts_fresh_runtime_scope_once(self):
        poll = self.monitor.poll
        segment = ["/sessions/rollout-A.jsonl"]
        self.monitor.poll = lambda: dict(poll(), source_file=segment[0])
        self.controller.poll()
        segment[0] = "/sessions/rollout-A_run-id.jsonl"
        self.controller.poll()
        self.controller.poll()
        self.assertEqual(self.engine.commands.count(("suspend", "A")), 1)
        self.assertTrue(self.engine.observations[-1][1]["allow_automatic"])

    def test_read_failure_cancels_unsent_samples_and_disables_manual_submission(self):
        self.controller.poll()
        self.monitor.status = "error"
        state = self.controller.poll()
        self.assertFalse(state["attribution"]["available"])
        self.assertIn(("suspend", "A"), self.engine.commands)
        self.controller.command({"action": "attribute", "session_id": "A"})
        self.assertIn("读取失败", self.controller.command_error)
        self.assertFalse(any(row[0] == "manual" for row in self.engine.commands))

    def test_completed_configuration_is_not_borrowed_from_current_aborted_turn(self):
        poll = self.monitor.poll
        completed = {"model": "gpt-5.6-sol", "effort": "medium", "provider": "test", "context_segment": "old"}
        self.monitor.poll = lambda: dict(poll(), last_completed_turn_config=completed,
                                         configuration_model="gpt-6.1-sol", configuration_effort="high")
        self.controller.poll()
        self.assertEqual(self.engine.observations[-1][1]["turn_config"], completed)





class ReaderCommandIntegrationTests(unittest.TestCase):
    def test_long_running_reader_rebinds_without_restart_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            sessions.mkdir()
            for name in ("A", "B"):
                events = [
                    {"type": "session_meta", "payload": {"id": name, "title": "会话 " + name, "source": "cli"}},
                    {"type": "event_msg", "timestamp": "2026-10-28T01:00:00Z", "payload": {"type": "task_started", "turn_id": "turn-" + name}},
                    {"type": "turn_context", "timestamp": "2026-10-28T01:00:01Z", "payload": {"turn_id": "turn-" + name, "model": "gpt-6.1-sol", "effort": "medium"}},
                    {"type": "event_msg", "timestamp": "2026-10-28T01:00:02Z", "payload": {"type": "task_complete", "turn_id": "turn-" + name}},
                ]
                (sessions / ("rollout-test-" + name + ".jsonl")).write_text("".join(json.dumps(row) + "\n" for row in events))
            state_file = root / "state" / "monitor.json"
            command = [sys.executable, "-B", str(ROOT / "passive_monitor.py"), "--watch", "--control-stdin",
                       "--sessions-dir", str(sessions), "--state-file", str(state_file), "--session", "A"]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pending = b""

            def receive(predicate):
                nonlocal pending
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if b"\n" not in pending:
                        ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
                        self.assertTrue(ready, "reader did not return a state")
                        chunk = os.read(process.stdout.fileno(), 65536)
                        self.assertTrue(chunk, "reader exited unexpectedly")
                        pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        state = json.loads(line)
                        if predicate(state):
                            return state
                self.fail("expected state was not emitted")

            try:
                receive(lambda state: state.get("session_id") == "A")
                pid = process.pid
                process.stdin.write(b'{"action":"bind","session_id":"B"}\n')
                process.stdin.flush()
                bound = receive(lambda state: state.get("session_id") == "B")
                self.assertEqual(bound["selection_mode"], "bound")
                self.assertFalse(bound["attribution"]["available"])
                self.assertEqual(process.pid, pid)
                self.assertEqual(json.loads(state_file.read_text())["session_id"], "B")
                process.stdin.write(b'{"action":"attribute","session_id":"A"}\n')
                process.stdin.flush()
                refused = receive(lambda state: bool(state.get("command_error")))
                self.assertEqual(refused["session_id"], "B")
            finally:
                process.terminate()
                process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
