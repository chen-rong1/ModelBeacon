import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from passive_monitor import (
    Conversation, PassiveMonitor, SessionTitleIndex, clean_title, load_model_config,
)
from test_passive_monitor import context, event, header, write_rows


def setting(model="gpt-6.1-sol", session="main", second=1):
    return event("event_msg", {"type": "thread_settings_applied", "thread_id": session,
                               "thread_settings": {"model": model}}, second)


class SessionTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.index = self.home / "session_index.jsonl"
        self.sessions = self.home / "sessions"
        self.rollout = self.sessions / "2026/10/28/rollout-main.jsonl"

    def index_rows(self, *rows):
        self.index.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def test_primary_conversation_name_is_resolved_by_exact_session_id(self):
        self.index_rows({"id": "main", "thread_name": "项目发布整理"}, {"id": "other", "thread_name": "其他会话"})
        write_rows(self.rollout, [header(), context()])
        state = PassiveMonitor(self.sessions).poll(0)
        self.assertEqual(state["session_title"], "项目发布整理")
        self.assertEqual(state["session_title_source"], "session_index")

    def test_latest_index_row_and_rename_update_without_rereading_rollout(self):
        self.index_rows({"id": "main", "thread_name": "旧名称"})
        write_rows(self.rollout, [header(), context()])
        monitor = PassiveMonitor(self.sessions)
        self.assertEqual(monitor.poll(0)["session_title"], "旧名称")
        with self.index.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "main", "thread_name": "新名称"}) + "\n")
        self.assertEqual(monitor.poll(1)["session_title"], "新名称")
        with patch.object(Path, "open", side_effect=AssertionError("unchanged title and rollout must stay cached")):
            self.assertEqual(monitor.poll(2)["session_title"], "新名称")

    def test_missing_index_falls_back_to_explicit_session_metadata(self):
        meta = header()
        meta["payload"]["title"] = "会话元数据名称"
        write_rows(self.rollout, [meta, context()])
        state = PassiveMonitor(self.sessions).poll(0)
        self.assertEqual(state["session_title"], "会话元数据名称")
        self.assertEqual(state["session_title_source"], "session_meta")
        self.assertFalse(self.index.exists())

    def test_index_overrides_older_rollout_title_and_never_uses_message_body(self):
        meta = header()
        meta["payload"]["title"] = "旧标题"
        self.index_rows({"id": "main", "thread_name": "重命名后的标题"})
        write_rows(self.rollout, [meta, context(), event("response_item", {"type": "message", "role": "user", "content": [{"text": "PRIVATE BODY"}]}, 2)])
        state = PassiveMonitor(self.sessions).poll(0)
        self.assertEqual(state["session_title"], "重命名后的标题")
        self.assertNotIn("PRIVATE BODY", json.dumps(state))

    def test_title_is_a_bounded_single_line_without_invisible_controls(self):
        self.assertEqual(clean_title("  名称\n\t第二行\x00\u202e  "), "名称 第二行")
        self.assertEqual(len(clean_title("字" * 500)), 160)
        self.assertTrue(clean_title("字" * 500).endswith("…"))
        self.assertIsNone(clean_title("\x00\u202e  "))
        self.assertIsNone(clean_title({"title": "forged"}))

    def test_corrupt_and_unrelated_index_records_do_not_supply_a_title(self):
        self.index.write_text('not json\n[]\n' + json.dumps({"id": "other", "thread_name": "别人的标题"}) + '\n')
        write_rows(self.rollout, [header(), context()])
        state = PassiveMonitor(self.sessions).poll(0)
        self.assertEqual(state["session_title"], "未命名会话")
        self.assertIsNone(state["session_title_source"])

    def test_index_access_error_does_not_break_model_monitor(self):
        write_rows(self.rollout, [header(), context()])
        resolver = SessionTitleIndex(self.home)
        with patch.object(Path, "stat", side_effect=PermissionError):
            self.assertEqual(resolver.resolve("main"), ("未命名会话", None))
        self.assertEqual(PassiveMonitor(self.sessions).poll(0)["observed_model"], "gpt-6.1-sol")

    def test_index_replacement_and_removal_drop_old_names(self):
        self.index_rows({"id": "main", "thread_name": "旧标题"})
        resolver = SessionTitleIndex(self.home)
        self.assertEqual(resolver.resolve("main")[0], "旧标题")
        replacement = self.home / "replacement.jsonl"
        replacement.write_text(json.dumps({"id": "other", "thread_name": "其他标题"}) + "\n")
        replacement.replace(self.index)
        self.assertEqual(resolver.resolve("main")[0], "未命名会话")
        self.index.unlink()
        self.assertEqual(resolver.resolve("other")[0], "未命名会话")

    def test_cleared_name_does_not_keep_an_earlier_index_value(self):
        self.index_rows({"id": "main", "thread_name": "旧标题"}, {"id": "main", "thread_name": ""})
        self.assertEqual(SessionTitleIndex(self.home).resolve("main")[0], "未命名会话")

    def test_explicit_name_reset_blocks_an_older_rollout_title(self):
        self.index_rows({"id": "main", "thread_name": ""})
        meta = header()
        meta["payload"]["title"] = "旧的会话名称"
        write_rows(self.rollout, [meta, context()])
        state = PassiveMonitor(self.sessions).poll(0)
        self.assertEqual(state["session_title"], "未命名会话")
        self.assertEqual(state["session_title_source"], "session_index")

    def test_empty_name_tombstones_share_the_existing_entry_limit(self):
        self.index_rows(*({"id": str(index), "thread_name": ""} for index in range(10)))
        with patch("passive_monitor.MAX_TITLE_ENTRIES", 2):
            resolver = SessionTitleIndex(self.home)
            self.assertEqual(resolver.resolve("9", "旧标题")[0], "未命名会话")
            self.assertLessEqual(len(resolver._titles), 2)

    def test_title_index_read_and_cache_size_are_bounded(self):
        self.index_rows(*({"id": str(index), "thread_name": "标题" * 10} for index in range(20)))
        with patch("passive_monitor.MAX_TITLE_INDEX_BYTES", 600), patch("passive_monitor.MAX_TITLE_ENTRIES", 2):
            resolver = SessionTitleIndex(self.home)
            self.assertEqual(resolver.resolve("19")[0], "标题" * 10)
            self.assertLessEqual(len(resolver._titles), 2)
            self.assertEqual(resolver.resolve("0")[0], "未命名会话")

    def test_locked_chat_can_show_its_name_while_waiting_for_rollout(self):
        self.index_rows({"id": "missing", "thread_name": "指定会话"})
        state = PassiveMonitor(self.sessions, session="missing").poll(0)
        self.assertEqual(state["session_title"], "指定会话")
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["session_id"], "missing")


class LocalModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.conversation = Conversation(Path("rollout-main.jsonl"), load_model_config())
        self.conversation.consume(header())

    def test_local_choice_is_recorded_separately_from_config_and_request(self):
        self.conversation.consume(setting())
        self.conversation.consume(context())
        state = self.conversation.snapshot()
        self.assertEqual(state["selected_model"], "gpt-6.1-sol")
        self.assertEqual(state["selected_model_source"], "thread_settings_applied")
        self.assertEqual(state["observed_model"], "gpt-6.1-sol")
        self.assertEqual(state["status"], "ready")


    def test_choice_changed_for_next_turn_does_not_relabel_previous_request(self):
        self.conversation.consume(setting("gpt-5.6-luna"))
        self.conversation.consume(context("gpt-5.6-luna"))
        self.conversation.consume(event("event_msg", {"type": "api_request", "request_id": "request-1", "model": "gpt-5.6-luna"}, 2))
        self.conversation.consume(setting("gpt-6.1-sol", second=3))
        self.conversation.consume(event("event_msg", {"type": "api_response", "request_id": "request-1", "model": "gpt-5.6-luna"}, 4))
        state = self.conversation.snapshot()
        self.assertEqual(state["selected_model"], "gpt-6.1-sol")
        self.assertEqual(state["status"], "ready")
        self.conversation.consume(context("gpt-6.1-sol", 5, "turn-2"))
        self.conversation.consume(event("event_msg", {"type": "api_response", "request_id": "request-1", "turn_id": "turn-1", "model": "gpt-5.6-luna"}, 6))
        after = self.conversation.snapshot()
        self.assertEqual(after["selected_model"], "gpt-6.1-sol")
        self.assertEqual(after["status"], "ready")

    def test_selection_persists_between_turns_but_not_between_sessions(self):
        self.conversation.consume(setting())
        self.conversation.consume(context())
        self.conversation.consume(event("event_msg", {"type": "task_started", "turn_id": "turn-2"}, 3))
        self.assertEqual(self.conversation.selected_model, "gpt-6.1-sol")
        self.conversation.consume(header("other"))
        self.assertIsNone(self.conversation.selected_model)



    def test_outer_and_nested_foreign_ids_override_other_matching_ids(self):
        self.conversation.consume(context())
        pair = {"type": "thread_settings_applied", "thread_id": "main", "thread_settings": {"model": "gpt-5.6-luna"}}
        for forged in (
            {**event("event_msg", pair, 2), "session_id": "other"},
            event("event_msg", {**pair, "response": {"session_id": "other"}}, 2),
            event("event_msg", {**pair, "request": {"threadId": "other"}}, 2),
        ):
            self.conversation.consume(forged)
            self.assertIsNone(self.conversation.selected_model)
        self.conversation.consume(setting(session="main") | {"session_id": "other"})
        self.assertIsNone(self.conversation.selected_model)


    def test_other_thread_settings_and_message_body_cannot_supply_selection(self):
        self.conversation.consume(setting(session="other"))
        self.conversation.consume(event("response_item", {"type": "message", "role": "user", "content": [{"text": json.dumps(setting())}]}, 2))
        self.assertIsNone(self.conversation.selected_model)

    def test_reset_to_default_clears_explicit_saved_choice(self):
        self.conversation.consume(setting())
        self.conversation.consume(setting(None))
        self.assertIsNone(self.conversation.selected_model)
        self.assertIsNone(self.conversation.selected_model_source)

    def test_replaced_or_truncated_rollout_cannot_keep_a_previous_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-main.jsonl"
            write_rows(path, [header(), setting(), context()])
            monitor = PassiveMonitor(root)
            self.assertEqual(monitor.poll(0)["selected_model"], "gpt-6.1-sol")
            write_rows(path, [header(), context("gpt-5.6-luna")])
            state = monitor.poll(1)
            self.assertIsNone(state["selected_model"])
            self.assertEqual(state["observed_model"], "gpt-5.6-luna")
            replacement = path.with_suffix(".tmp")
            write_rows(replacement, [header(), setting("gpt-6.1-terra"), context("gpt-6.1-terra")])
            replacement.replace(path)
            self.assertEqual(monitor.poll(2)["selected_model"], "gpt-6.1-terra")

    def test_bounded_bootstrap_recovers_local_choice_without_actual_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-main.jsonl"
            write_rows(path, [header(), setting(), context()])
            with path.open("a") as handle:
                for _ in range(40):
                    handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 100000}}) + "\n")
            state = PassiveMonitor(root).poll(0)
            self.assertEqual(state["selected_model"], "gpt-6.1-sol")
            self.assertEqual(state["observed_model"], "gpt-6.1-sol")
            self.assertEqual(state["status"], "ready")

    def test_bootstrap_cannot_strip_a_foreign_session_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-main.jsonl"
            foreign_setting = setting("gpt-5.6-luna")
            foreign_setting["payload"]["session_id"] = "other"
            foreign_context = context("gpt-5.6-luna", session_id="other")
            write_rows(path, [header(), setting(), context(), foreign_setting, foreign_context])
            with path.open("a") as handle:
                for _ in range(40):
                    handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 100000}}) + "\n")
            state = PassiveMonitor(root).poll(0)
            self.assertEqual(state["selected_model"], "gpt-6.1-sol")
            self.assertEqual(state["observed_model"], "gpt-6.1-sol")




if __name__ == "__main__":
    unittest.main()
