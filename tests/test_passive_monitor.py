import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from passive_monitor import (  # noqa: E402
    MAX_LINE_BYTES,
    Conversation,
    FileCursor,
    PassiveMonitor,
    load_model_config,
    model_family,
    normalize_model,
)


def event(kind, payload, second=0):
    return {"timestamp": f"2026-10-28T10:00:{second:02d}Z", "type": kind, "payload": payload}


def header(identifier="main", subagent=False):
    payload = {"id": identifier, "source": {"subagent": {"thread_spawn": {}}} if subagent else "vscode"}
    return event("session_meta", payload)


def context(model="gpt-6.1-sol", second=1, turn="turn-1", **extra):
    return event("turn_context", {"turn_id": turn, "model": model, "effort": "ultra", **extra}, second)


def write_rows(path, rows, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")




class FileAndPollingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "2026/10/28/rollout-main.jsonl"
        self.config = load_model_config()

    def test_incremental_partial_corrupt_and_complete_lines(self):
        write_rows(self.path, [header(), context()])
        cursor = FileCursor(self.path, self.config)
        cursor.refresh()
        self.assertEqual(cursor.conversation.observed_model, "gpt-6.1-sol")
        encoded = (json.dumps(context("gpt-6.1-terra", 4, "turn-2")) + "\n").encode()
        with self.path.open("ab") as handle:
            handle.write(b"bad-json\n" + encoded[:20])
        cursor.refresh()
        self.assertEqual(cursor.conversation.observed_model, "gpt-6.1-sol")
        with self.path.open("ab") as handle:
            handle.write(encoded[20:])
        cursor.refresh()
        self.assertEqual(cursor.conversation.observed_model, "gpt-6.1-terra")
        self.assertLessEqual(len(cursor.pending), MAX_LINE_BYTES)

    def test_truncation_clears_old_configuration(self):
        write_rows(self.path, [header(), context(requested_model="gpt-6.1-sol", served_model="gpt-5.6-luna")])
        cursor = FileCursor(self.path, self.config)
        cursor.refresh()
        self.assertEqual(cursor.conversation.snapshot()["status"], "ready")
        write_rows(self.path, [header("replacement"), context("gpt-6.1-terra", 4)])
        cursor.refresh()
        self.assertEqual(cursor.conversation.snapshot()["status"], "ready")
        self.assertEqual(cursor.conversation.session_id, "replacement")

    def test_replaced_inode_clears_old_configuration(self):
        write_rows(self.path, [header(), context(requested_model="gpt-6.1-sol", served_model="gpt-5.6-luna")])
        cursor = FileCursor(self.path, self.config)
        cursor.refresh()
        replacement = self.path.with_suffix(".tmp")
        write_rows(replacement, [header("new"), context("gpt-6.1-terra", 4)])
        replacement.replace(self.path)
        cursor.refresh()
        self.assertEqual(cursor.conversation.session_id, "new")

    def test_oversized_record_is_skipped_and_metadata_recovers(self):
        write_rows(self.path, [header(), context()])
        cursor = FileCursor(self.path, self.config)
        cursor.refresh()
        with self.path.open("ab") as handle:
            handle.write(b'{"type":"response_item","payload":{"type":"message","text":"' + b"x" * (MAX_LINE_BYTES * 3) + b'"}}\n')
            handle.write((json.dumps(context("gpt-6.1-terra", 4, "turn-2")) + "\n").encode())
        cursor.refresh()
        self.assertLessEqual(len(cursor.pending), MAX_LINE_BYTES)
        cursor.refresh()
        self.assertEqual(cursor.conversation.observed_model, "gpt-6.1-terra")

    def test_large_tool_completion_after_body_is_counted_only_when_record_finishes(self):
        write_rows(self.path, [header(), context()])
        monitor = PassiveMonitor(self.root, session="main")
        self.assertEqual(monitor.poll(0)["normal_tool_calls"], 0)
        # Native metadata order is not guaranteed. The ID and subtype may occur
        # after several MiB of output, spanning more than one incremental read.
        row = {"timestamp": "2026-10-28T01:00:05Z", "payload": {
            "output": "PRIVATE OUTPUT " * MAX_LINE_BYTES,
            "call_id": "large-tool", "type": "function_call_output"}, "type": "response_item"}
        encoded = json.dumps(row).encode()
        with self.path.open("ab") as handle:
            handle.write(encoded)
        for tick in range(1, 5):
            snapshot = monitor.poll(tick)
            self.assertEqual(snapshot["normal_tool_calls"], 0)
            cursor = next(iter(monitor.cursors.values()))
            self.assertLessEqual(len(cursor.pending), MAX_LINE_BYTES)
        with self.path.open("ab") as handle:
            handle.write(b"\n")
        self.assertEqual(monitor.poll(5)["normal_tool_calls"], 1)
        self.assertEqual(monitor.poll(6)["normal_tool_calls"], 1)
        self.assertNotIn("PRIVATE OUTPUT", json.dumps(monitor.poll(7)))

    def test_large_tool_completions_share_id_deduplication_with_small_records(self):
        cursor = FileCursor(self.path, self.config)
        cursor.conversation.consume(header())
        for subtype, identifier in (("function_call_output", "shared"),
                                    ("custom_tool_call_output", "shared"),
                                    ("local_shell_call_output", None),
                                    ("local_shell_call_output", None)):
            payload = {"output": "x" * (MAX_LINE_BYTES + 1), "type": subtype}
            if identifier:
                payload["call_id"] = identifier
            encoded = (json.dumps(event("response_item", payload, 4)) + "\n").encode()
            for start in range(0, len(encoded), 4093):
                cursor._feed(encoded[start:start + 4093])
        cursor._feed((json.dumps(event("response_item", {
            "type": "function_call_output", "call_id": "shared"}, 5)) + "\n").encode())
        self.assertEqual(cursor.conversation.normal_tool_calls, 3)

    def test_large_record_uses_structural_metadata_and_rejects_foreign_identifiers(self):
        cursor = FileCursor(self.path, self.config)
        cursor.conversation.consume(header())
        private = "PRIVATE " * MAX_LINE_BYTES
        spoof = {"type": "function_call_output", "call_id": "forged", "thread_id": "main"}
        rows = [
            event("response_item", {"type": "message", "content": [{"text": private + json.dumps(spoof)}]}, 3),
            event("response_item", {"type": "message", "output": {"body": private, **spoof}}, 3),
        ]
        for source in ("root", "payload", "request", "response"):
            row = event("response_item", {"output": private, "type": "function_call_output", "call_id": source}, 4)
            if source == "root":
                row["thread_id"] = "other"
            elif source == "payload":
                row["payload"]["sessionId"] = "other"
            else:
                row["payload"][source] = {"threadId": "other"}
            rows.append(row)
        for row in rows:
            encoded = (json.dumps(row) + "\n").encode()
            for start in range(0, len(encoded), 64 * 1024):
                cursor._feed(encoded[start:start + 64 * 1024])
        self.assertEqual(cursor.conversation.normal_tool_calls, 0)

    def test_large_record_validates_json_before_counting_and_recovers_at_newline(self):
        cursor = FileCursor(self.path, self.config)
        cursor.conversation.consume(header())
        prefix = b'{"type":"response_item","payload":{"output":"' + b"x" * (MAX_LINE_BYTES + 1)
        suffix = b'","type":"function_call_output","call_id":"invalid"}}'
        malformed = (prefix + b'\\q' + suffix, prefix + suffix[:-1],
                     prefix + suffix + b" true", prefix + b"\xff" + suffix,
                     prefix + suffix[:-1] + b',"invalid_number":01}')
        for encoded in malformed:
            for start in range(0, len(encoded), 8191):
                cursor._feed(encoded[start:start + 8191])
            cursor._feed(b"\n")
        self.assertEqual(cursor.conversation.normal_tool_calls, 0)
        cursor._feed((json.dumps(event("response_item", {
            "type": "function_call_output", "call_id": "valid-after-errors"}, 5)) + "\n").encode())
        self.assertEqual(cursor.conversation.normal_tool_calls, 1)

    def test_large_record_handles_split_utf8_escapes_and_duplicate_metadata_keys(self):
        cursor = FileCursor(self.path, self.config)
        cursor.conversation.consume(header())
        prefix = b'{"type":"response_item","payload":{"output":"'
        body = ("数字🙂" * (MAX_LINE_BYTES // 4)).encode()
        suffix = b'","\\u0074ype":"function_call_output","call_id":"split-\\u0031","thread_id":"other","thread_id":"main"}}\n'
        encoded = prefix + body + suffix
        for start in range(0, len(encoded), 4093):
            cursor._feed(encoded[start:start + 4093])
        self.assertEqual(cursor.conversation.normal_tool_calls, 1)
        cursor._feed((json.dumps(event("response_item", {
            "type": "function_call_output", "call_id": "split-1"}, 6)) + "\n").encode())
        self.assertEqual(cursor.conversation.normal_tool_calls, 1)

    def test_switches_to_latest_real_conversation_and_excludes_subagent(self):
        write_rows(self.path, [header("main"), context()])
        other = self.path.with_name("rollout-other.jsonl")
        write_rows(other, [header("other"), context("gpt-6.1-terra", 5)])
        child = self.path.with_name("rollout-subagent.jsonl")
        write_rows(child, [header("child", True), context("gpt-5.6-luna", 9)])
        monitor = PassiveMonitor(self.root)
        self.assertEqual(monitor.poll(0)["session_id"], "other")
        write_rows(self.path, [context("gpt-6.1-sol", 10, "turn-2")], "a")
        self.assertEqual(monitor.poll(3)["session_id"], "main")

    def test_many_background_logs_cannot_evict_main_conversation(self):
        write_rows(self.path, [header("main"), context()])
        for index in range(12):
            child = self.path.with_name(f"rollout-child-{index}.jsonl")
            meta = header(f"child-{index}", True)
            if index % 2:
                meta["payload"]["source"] = {"Internal": "guardian"}
            if index % 3 == 0:
                meta["payload"]["source"] = "cli"
                meta["payload"]["thread_source"] = "subagent"
            write_rows(child, [meta, context("gpt-5.6-luna", 9)])
            os.utime(child, (1900000000 + index, 1900000000 + index))
        result = PassiveMonitor(self.root).poll(0)
        self.assertEqual(result["session_id"], "main")

    def test_session_lock_requires_filename_and_metadata_identity(self):
        target = self.path.with_name("rollout-2026-10-28-target-id.jsonl")
        write_rows(target, [header("target-id"), context("gpt-6.1-terra", 2)])
        write_rows(self.path, [header("main"), context("gpt-6.1-sol", 9)])
        monitor = PassiveMonitor(self.root, session="target-id")
        self.assertEqual(monitor.poll(0)["session_id"], "target-id")
        self.assertEqual(monitor.poll(3)["observed_model"], "gpt-6.1-terra")
        wrong = self.path.with_name("rollout-forged-target-id.jsonl")
        write_rows(wrong, [header("different-id"), context("gpt-5.6-luna", 10)])
        self.assertEqual(monitor.poll(16)["session_id"], "target-id")

    def test_resumed_native_run_is_current_and_old_segment_cannot_take_its_slot(self):
        write_rows(self.path, [header(), context("gpt-5.6-sol", 55)])
        resumed = self.path.with_name("rollout-2026-10-28T13-51-21-main_01a18110-251f-7452-94eb-3574a220baef.jsonl")
        write_rows(resumed, [header(), context("gpt-6.1-sol", 2)])
        modified = self.path.stat().st_mtime
        os.utime(resumed, (modified + 1, modified + 1))
        monitor = PassiveMonitor(self.root, session="main")
        snapshot = monitor.poll(0)
        self.assertEqual(snapshot["source_file"], str(resumed))
        self.assertEqual(snapshot["configuration_model"], "gpt-6.1-sol")
        self.assertEqual(list(monitor.cursors), [resumed])
        self.assertEqual([row["session_id"] for row in snapshot["sessions"]], ["main"])
        self.assertEqual(monitor.poll(16)["source_file"], str(resumed))

    def test_resumed_filename_cannot_override_foreign_or_background_header(self):
        write_rows(self.path, [header(), context()])
        for suffix, metadata in (("01a18110-251f-7452-94eb-3574a220baef", header("other")),
                                 ("01a18110-251f-7452-94eb-3574a220baee", header("main", True))):
            resumed = self.path.with_name(f"rollout-2026-10-28-main_{suffix}.jsonl")
            write_rows(resumed, [metadata, context("gpt-5.6-luna", 9)])
            os.utime(resumed, (1900000000, 1900000000))
        malformed = self.path.with_name("rollout-main_not-a-native-run-id.jsonl")
        write_rows(malformed, [header(), context("gpt-5.6-luna", 10)])
        snapshot = PassiveMonitor(self.root, session="main").poll(0)
        self.assertEqual(snapshot["source_file"], str(self.path))

    def test_incremental_counters_remain_continuous_when_native_run_changes(self):
        historic_tools = [event("response_item", {"type": "function_call_output", "call_id": f"history-{i}"}, 2)
                          for i in range(8)]
        write_rows(self.path, [header(), context(), *historic_tools])
        monitor = PassiveMonitor(self.root, session="main")
        self.assertEqual(monitor.poll(0)["normal_tool_calls"], 0)
        write_rows(self.path, [event("response_item", {"type": "function_call_output", "call_id": "live-old"}, 3),
                               event("event_msg", {"type": "task_complete", "turn_id": "turn-1"}, 4)], "a")
        before = monitor.poll(1)
        self.assertEqual((before["normal_tool_calls"], before["normal_completed_turns"]), (1, 1))
        resumed = self.path.with_name("rollout-2026-10-28-main_01a18110-251f-7452-94eb-3574a220baef.jsonl")
        write_rows(resumed, [header(), context(turn="turn-new", second=5), *historic_tools * 5])
        modified = self.path.stat().st_mtime
        os.utime(resumed, (modified + 1, modified + 1))
        switched = monitor.poll(16)
        self.assertEqual(switched["source_file"], str(resumed))
        self.assertEqual((switched["normal_tool_calls"], switched["normal_completed_turns"]), (1, 1))
        write_rows(resumed, [event("response_item", {"type": "custom_tool_call_output", "call_id": "live-new"}, 6),
                            event("response_item", {"type": "custom_tool_call_output", "call_id": "live-new"}, 6),
                            event("event_msg", {"type": "task_complete", "turn_id": "turn-new"}, 7)], "a")
        after = monitor.poll(17)
        self.assertEqual((after["normal_tool_calls"], after["normal_completed_turns"]), (2, 2))

    def test_long_turn_without_bootstrapped_context_still_counts_new_tool_outputs(self):
        write_rows(self.path, [header(), context()])
        write_rows(self.path, [event("response_item", {"type": "message", "role": "assistant",
                               "content": [{"text": "x" * 400}]}, 2)] * 20, "a")
        write_rows(self.path, [event("response_item", {"type": "function_call_output", "call_id": "warm-tool"}, 3)], "a")
        with patch("passive_monitor.WARM_START_BYTES", 1024), \
             patch("passive_monitor.BOOTSTRAP_SCAN_BYTES", 2048), \
             patch("passive_monitor.BOOTSTRAP_CHUNK_BYTES", 256):
            monitor = PassiveMonitor(self.root, session="main")
            initial = monitor.poll(0)
            self.assertEqual(initial["session_id"], "main")
            self.assertIsNone(initial["configuration_model"])
            self.assertEqual(initial["normal_tool_calls"], 0)
            write_rows(self.path, [event("response_item", {"type": "function_call_output", "call_id": "live-tool"}, 4)], "a")
            after = monitor.poll(1)
            self.assertEqual(after["normal_tool_calls"], 1)
            self.assertTrue(next(iter(monitor.cursors.values())).conversation.has_normal_activity)

    def test_missing_locked_session_does_not_fall_back_to_other_chat(self):
        write_rows(self.path, [header("main"), context()])
        result = PassiveMonitor(self.root, session="absent").poll(0)
        self.assertEqual(result["status"], "idle")
        self.assertIsNone(result["observed_model"])


    def test_incremental_same_chunk_uses_latest_configuration(self):
        write_rows(self.path, [header(), context()])
        monitor = PassiveMonitor(self.root)
        monitor.poll(0)
        write_rows(self.path, [event("event_msg", {"type": "model_reroute", "from_model": "gpt-6.1-sol", "to_model": "gpt-5.6-luna"}, 3), context("gpt-6.1-terra", 5, "turn-2")], "a")
        result = monitor.poll(1)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result, monitor.poll(2))


    def test_bootstrap_recovers_current_context_before_two_mib_tail(self):
        write_rows(self.path, [header(), context()])
        with self.path.open("a") as handle:
            for _ in range(40):
                handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 100000}}) + "\n")
        result = PassiveMonitor(self.root).poll(0)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["observed_model"], "gpt-6.1-sol")
        self.assertEqual(result["reasoning_effort"], "ultra")

    def test_bootstrap_later_task_start_does_not_borrow_old_context(self):
        write_rows(self.path, [header(), context(), event("event_msg", {"type": "task_started", "turn_id": "turn-2"}, 4)])
        with self.path.open("a") as handle:
            for _ in range(40):
                handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 100000}}) + "\n")
        result = PassiveMonitor(self.root).poll(0)
        self.assertEqual(result["status"], "ready")
        self.assertIsNone(result["observed_model"])


    def test_bootstrap_scan_limit_does_not_expand_to_full_history(self):
        write_rows(self.path, [header(), context()])
        with self.path.open("a") as handle:
            for _ in range(200):
                handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 100000}}) + "\n")
        write_rows(self.path, [event("response_item", {"type": "message", "role": "assistant", "content": []}, 5)], "a")
        result = PassiveMonitor(self.root).poll(0)
        self.assertEqual(result["status"], "ready")
        self.assertIsNone(result["observed_model"])

    def test_tail_new_turn_still_clears_bootstrapped_context(self):
        write_rows(self.path, [header(), context()])
        with self.path.open("a") as handle:
            for _ in range(40):
                handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 100000}}) + "\n")
        write_rows(self.path, [event("event_msg", {"type": "task_started", "turn_id": "turn-2"}, 5)], "a")
        result = PassiveMonitor(self.root).poll(0)
        self.assertEqual(result["status"], "ready")
        self.assertIsNone(result["observed_model"])

    def test_large_append_does_not_publish_middle_of_backlog_as_current(self):
        write_rows(self.path, [header(), context()])
        monitor = PassiveMonitor(self.root)
        monitor.poll(0)
        write_rows(self.path, [event("event_msg", {"type": "model_reroute", "from_model": "gpt-6.1-sol", "to_model": "gpt-5.6-luna"}, 3)], "a")
        with self.path.open("a") as handle:
            for _ in range(24):
                handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 100000}}) + "\n")
        write_rows(self.path, [context("gpt-6.1-terra", 5, "turn-2")], "a")
        while_catching_up = monitor.poll(1)
        self.assertEqual(while_catching_up["status"], "loading")
        caught_up = monitor.poll(2)
        self.assertEqual(caught_up["observed_model"], "gpt-6.1-terra")

    def test_repeated_poll_is_stable_and_does_not_reread_file(self):
        write_rows(self.path, [header(), context()])
        monitor = PassiveMonitor(self.root)
        first = monitor.poll(0)
        with patch.object(Path, "open", side_effect=AssertionError("unchanged files must not be reread")):
            self.assertEqual(first, monitor.poll(3))

    def test_missing_directory_waits_without_creating_files(self):
        missing = self.root / "does-not-exist"
        self.assertEqual(PassiveMonitor(missing).poll()["status"], "idle")
        self.assertFalse(missing.exists())

    def test_no_network_processes_auth_or_body_export(self):
        write_rows(self.path, [header(), context(), event("response_item", {"type": "message", "role": "assistant", "content": [{"text": "SECRET-CONTENT"}]}, 2)])
        blocked = AssertionError("passive monitor must not send requests or create processes")
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path.name, "auth.json")
            return original_open(path, *args, **kwargs)

        with patch.object(socket, "socket", side_effect=blocked), patch.object(socket, "create_connection", side_effect=blocked), patch.object(subprocess, "Popen", side_effect=blocked), patch.object(Path, "open", guarded_open):
            result = PassiveMonitor(self.root).poll(0)
        self.assertEqual(result["observed_model"], "gpt-6.1-sol")
        self.assertNotIn("SECRET-CONTENT", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
