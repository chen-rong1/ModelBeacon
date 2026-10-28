import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from passive_monitor import Conversation, PassiveMonitor, load_model_config
from test_passive_monitor import context, event, header, write_rows
from test_session_metadata import setting


class MonitorBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.state = self.root / "application/monitor-state.json"
        self.a = self.sessions / "2026/10/28/rollout-a.jsonl"
        self.b = self.a.with_name("rollout-b.jsonl")
        write_rows(self.a, [header("a"), context(requested_model="gpt-6.1-sol", served_model="gpt-5.6-luna")])
        write_rows(self.b, [header("b"), context("gpt-6.1-terra", 9)])
        (self.root / "session_index.jsonl").write_text(
            json.dumps({"id": "a", "thread_name": "开发 A"}) + "\n" +
            json.dumps({"id": "b", "thread_name": "开发 B"}) + "\n", encoding="utf-8")

    def test_binding_switches_live_and_restores(self):
        monitor = PassiveMonitor(self.sessions, state_file=self.state)
        self.assertEqual(monitor.poll(0)["selection_mode"], "recent_activity")
        monitor.bind("a")
        first = monitor.poll(1)
        self.assertEqual(first["session_id"], "a")
        self.assertEqual(first["status"], "ready")
        monitor.bind("b")
        second = monitor.poll(2)
        self.assertEqual(second["session_id"], "b")
        self.assertEqual(second["session_title"], "开发 B")
        self.assertEqual(PassiveMonitor(self.sessions, state_file=self.state).poll(0)["session_id"], "b")
        monitor.follow_recent()
        self.assertEqual(monitor.poll(3)["selection_mode"], "recent_activity")
        self.assertIsNone(PassiveMonitor(self.sessions, state_file=self.state).session)

    def test_name_catalog_includes_older_chats_and_binding_uses_full_identity(self):
        for index in range(12):
            target = self.a.with_name(f"rollout-other-{index}.jsonl")
            write_rows(target, [header(f"other-{index}"), context(second=index + 10)])
            os.utime(target, (1900000000 + index, 1900000000 + index))
        monitor = PassiveMonitor(self.sessions)
        choices = monitor.poll(0)["sessions"]
        self.assertTrue({"a", "b"}.issubset({row["session_id"] for row in choices}))
        monitor.bind("a")
        self.assertEqual(monitor.poll(1)["session_id"], "a")
        monitor.bind("missing")
        waiting = monitor.poll(2)
        self.assertEqual(waiting["bound_session_id"], "missing")
        self.assertEqual(waiting["status"], "idle")
        self.assertIsNone(waiting["observed_model"])
        self.assertEqual(waiting["session_id"], "missing")

    def test_binding_persistence_failure_is_visible(self):
        monitor = PassiveMonitor(self.sessions, state_file=self.state)
        with patch.object(Path, "mkdir", side_effect=PermissionError):
            monitor.bind("a")
        snapshot = monitor.poll(0)
        self.assertEqual(snapshot["session_id"], "a")
        self.assertIn("无法保存绑定", snapshot["binding_error"])
        self.assertIn("无法保存绑定", snapshot["detail"])

    def test_denied_directory_is_a_read_error_with_bound_identity(self):
        monitor = PassiveMonitor(self.sessions, session="a")
        with patch("passive_monitor.os.scandir", side_effect=PermissionError):
            snapshot = monitor.poll(0)
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["session_id"], "a")
        self.assertIn("权限", snapshot["detail"])
        self.assertFalse(snapshot["context_available"])


class SamplingConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.chat = Conversation(Path("rollout-main.jsonl"), load_model_config())
        self.chat.consume(header())

    def test_choice_and_turn_configuration_remain_separate(self):
        selected = setting()
        selected["payload"]["thread_settings"].update(reasoning_effort="high", model_provider="openai")
        self.chat.consume(selected)
        self.assertEqual(self.chat.snapshot()["label"], "会话已就绪")
        self.chat.consume(context("gpt-5.6-luna", second=2, effort="low"))
        self.chat.consume(event("event_msg", {"type": "api_request", "request_id": "r", "model": "gpt-5.6-luna", "effort": "low"}, 3))
        self.chat.consume(event("event_msg", {"type": "api_response", "request_id": "r", "model": "gpt-5.6-luna", "reasoning": {"effort": "low"}}, 4))
        state = self.chat.snapshot()
        self.assertEqual(state["selected_model"], "gpt-6.1-sol")
        self.assertEqual(state["selected_effort"], "high")
        self.assertEqual(state["selected_provider"], "openai")
        self.assertEqual(state["configuration_model"], "gpt-5.6-luna")
        self.assertEqual(state["configuration_effort"], "low")
        self.chat.consume(event("event_msg", {"type": "response_metadata", "request_id": "r", "actual_effort": "medium"}, 5))
        self.chat.consume(context(second=6, turn="turn-2"))
        after = self.chat.snapshot()
        self.assertEqual(after["label"], "会话已就绪")

    def test_selected_change_after_turn_is_explicitly_not_merged(self):
        self.chat.consume(setting(second=1))
        self.chat.consume(context(second=2))
        self.chat.consume(setting("gpt-5.6-luna", second=3))
        state = self.chat.snapshot()
        self.assertEqual(state["configuration_model"], "gpt-6.1-sol")
        self.assertEqual(state["selected_model"], "gpt-5.6-luna")




    def test_next_user_message_starts_new_configuration_scope_before_task_started(self):
        self.chat.consume(context(requested_model="gpt-6.1-sol", served_model="gpt-6.1-sol"))
        self.chat.consume(event("event_msg", {"type": "task_complete", "turn_id": "turn-1"}, 2))
        self.chat.consume(event("response_item", {"type": "message", "role": "user", "content": []}, 3))
        state = self.chat.snapshot()
        self.assertTrue(state["busy"])
        self.assertEqual(state["label"], "会话已就绪")

    def test_merged_poll_keeps_completed_configuration_when_new_turn_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-main.jsonl"
            write_rows(path, [header(), context(effort="high", model_provider="openai")])
            monitor = PassiveMonitor(path.parent, session="main")
            self.assertEqual(monitor.poll(0)["normal_completed_turns"], 0)
            selected = setting("gpt-6.1-terra", second=3)
            selected["payload"]["thread_settings"].update(reasoning_effort="medium", model_provider="openai")
            write_rows(path, [
                event("event_msg", {"type": "task_complete", "turn_id": "turn-1"}, 2),
                selected,
                event("event_msg", {"type": "task_started", "turn_id": "turn-2"}, 4),
                context("gpt-6.1-terra", second=5, turn="turn-2", effort="medium", model_provider="openai"),
                event("event_msg", {"type": "turn_aborted", "turn_id": "turn-2"}, 6),
            ], "a")
            state = monitor.poll(1)
            self.assertFalse(state["busy"])
            self.assertEqual(state["normal_completed_turns"], 1)
            self.assertEqual(state["turn_id"], "turn-2")
            self.assertEqual(state["configuration_model"], "gpt-6.1-terra")
            self.assertEqual(state["last_completed_turn_id"], "turn-1")
            self.assertEqual(state["last_completed_turn_config"], {
                "model": "gpt-6.1-sol", "effort": "high", "provider": "openai", "context_segment": "main:0",
            })

    def test_bounded_bootstrap_does_not_invent_a_historical_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-main.jsonl"
            write_rows(path, [header(), context(model_provider="openai"),
                              event("event_msg", {"type": "task_complete", "turn_id": "turn-1"}, 2)])
            with path.open("a", encoding="utf-8") as handle:
                for _ in range(40):
                    handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 100000}}) + "\n")
            state = PassiveMonitor(path.parent, session="main").poll(0)
            self.assertEqual(state["normal_completed_turns"], 0)
            self.assertIsNone(state["last_completed_turn_id"])
            self.assertIsNone(state["last_completed_turn_config"])

    def test_provider_in_session_header_is_retained_without_inventing_user_selection(self):
        self.chat.consume(event("session_meta", {"id": "main", "source": "cli", "model_provider": "openai"}, 0))
        self.chat.consume(context(second=2))
        state = self.chat.snapshot()
        self.assertEqual(state["session_provider"], "openai")
        self.assertEqual(state["configuration_provider"], "openai")
        self.assertIsNone(state["selected_provider"])
        self.chat.consume(event("session_meta", {"id": "other", "source": "cli"}, 3))
        self.assertIsNone(self.chat.snapshot()["session_provider"])
        self.assertIsNone(self.chat.snapshot()["configuration_provider"])

    def test_only_normal_completion_and_real_tool_call_increment_counters(self):
        self.chat.consume(event("event_msg", {"type": "task_started", "turn_id": "turn-1"}, 1))
        self.chat.consume(context(second=2))
        self.chat.consume(event("response_item", {"type": "function_call", "call_id": "call-1", "arguments": "private"}, 3))
        self.chat.consume(event("response_item", {"type": "function_call", "call_id": "call-1"}, 4))
        self.assertEqual(self.chat.snapshot()["normal_tool_calls"], 0)
        self.chat.consume(event("response_item", {"type": "function_call_output", "call_id": "call-1", "output": "private"}, 5))
        self.chat.consume(event("response_item", {"type": "function_call_output", "call_id": "call-1", "output": "private"}, 5))
        busy = self.chat.snapshot()
        self.assertTrue(busy["busy"])
        self.assertEqual(busy["normal_tool_calls"], 1)
        self.chat.consume(event("event_msg", {"type": "task_complete", "turn_id": "old"}, 6))
        self.assertTrue(self.chat.snapshot()["busy"])
        completed = event("event_msg", {"type": "task_complete", "turn_id": "turn-1"}, 7)
        self.chat.consume(completed)
        self.chat.consume(completed)
        state = self.chat.snapshot()
        self.assertFalse(state["busy"])
        self.assertEqual(state["normal_completed_turns"], 1)
        self.assertNotIn("private", json.dumps(state))
        before = state["context_segment"]
        self.chat.consume(event("compacted", {}, 8))
        self.assertNotEqual(self.chat.snapshot()["context_segment"], before)
        child = header("sample")
        child["payload"]["source"] = "model_beacon_attribution"
        self.chat.consume(child)
        self.chat.consume(context())
        self.chat.consume(completed)
        self.assertEqual(self.chat.normal_completed_turns, 0)


if __name__ == "__main__":
    unittest.main()
