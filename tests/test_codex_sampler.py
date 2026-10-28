"""Native-fork protocol checks; no real account, transcript or model calls."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_sampler import (AppServerClient, CodexSampler, SamplerError,
                           SAFE_SAMPLER_REASONS, codex_executable)


CONFIG = {"model": "gpt-6.1-sol", "provider": "openai", "effort": "ultra"}
SOURCE = "019a0000-0000-7000-8000-000000000001"
NEW_SOURCE = "new-sample-019a0000-0000-7000-8000-000000000002"


class FixtureClient:
    def __init__(self, *, source_change=None, fork_change=None, status="completed", output="[1,2,3]", goal=None,
                 turns=None, compaction=False, event_turn="probe-turn", snapshots=None, descendants=None,
                 reject_last_turn=False, mutate_base_during_fork=False):
        self.calls = []
        self.events = []
        self.closed = False
        self.source_change = source_change or {}
        self.fork_change = fork_change or {}
        self.status = status
        self.output = output
        self.forks = 0
        self.starts = 0
        self.goal = goal
        self.turns = [{"id": "source-turn", "status": status}] if turns is None else turns
        self.compaction = compaction
        self.event_turn = event_turn
        self.snapshots = snapshots if snapshots is not None else {}
        self.descendants = descendants or []
        self.reject_last_turn = reject_last_turn
        self.mutate_base_during_fork = mutate_base_during_fork

    def request(self, method, params=None, timeout=None):
        params = copy.deepcopy(params or {})
        self.calls.append((method, params))
        if method == "initialize":
            return {"userAgent": "fixture"}
        if method == "thread/read":
            if params["threadId"] in self.snapshots:
                return {"thread": copy.deepcopy(self.snapshots[params["threadId"]]["thread"])}
            return {"thread": {"id": SOURCE, "ephemeral": False, "path": "/fixture/source.jsonl",
                               "cwd": "/fixture", "model": CONFIG["model"],
                               "modelProvider": CONFIG["provider"], "reasoningEffort": CONFIG["effort"],
                               "status": {"type": "notLoaded"}, **self.source_change}}
        if method == "thread/turns/list":
            if params["threadId"] in self.snapshots:
                return {"data": copy.deepcopy(self.snapshots[params["threadId"]]["turns"])}
            return {"data": self.turns}
        if method == "thread/list":
            return {"data": self.descendants}
        if method == "thread/delete":
            self.snapshots.pop(params["threadId"], None)
            return {}
        if method == "thread/goal/get":
            return {"goal": self.goal}
        if method == "thread/start":
            self.starts += 1
            return {"thread": {"id": f"new-{id(self)}-{self.starts}", "ephemeral": True, "path": None,
                               "forkedFromId": None, "cwd": params["cwd"]},
                    "model": CONFIG["model"], "modelProvider": CONFIG["provider"],
                    "reasoningEffort": CONFIG["effort"], "cwd": params["cwd"], "approvalPolicy": "never",
                    "sandbox": {"type": "readOnly"}, **self.fork_change}
        if method == "thread/fork":
            if self.reject_last_turn and params.get("lastTurnId"):
                raise SamplerError("source_busy")
            self.forks += 1
            if params.get("ephemeral") is not True:
                ident = f"snapshot-{id(self)}-{self.forks}"
                thread = {"id": ident, "ephemeral": False, "path": f"/fixture/{ident}.jsonl",
                          "forkedFromId": SOURCE, "cwd": "/fixture", "model": CONFIG["model"],
                          "modelProvider": CONFIG["provider"], "reasoningEffort": CONFIG["effort"],
                          "status": {"type": "notLoaded"}}
                turns = copy.deepcopy(self.turns)
                for turn in turns:
                    if turn["status"] == "inProgress": turn["status"] = "interrupted"
                self.snapshots[ident] = {"thread": thread, "turns": turns}
                return {"thread": thread, "model": CONFIG["model"], "modelProvider": CONFIG["provider"],
                        "reasoningEffort": CONFIG["effort"], "cwd": "/fixture", "approvalPolicy": "never",
                        "sandbox": {"type": "readOnly"}}
            if self.mutate_base_during_fork:
                self.snapshots[params["threadId"]]["turns"].append({"id": "unexpected-new-turn", "status": "completed"})
            return {"thread": {"id": "fork-" + str(self.forks), "ephemeral": True, "path": None,
                               "forkedFromId": params["threadId"], "cwd": "/fixture"},
                    "model": CONFIG["model"], "modelProvider": CONFIG["provider"],
                    "reasoningEffort": CONFIG["effort"], "cwd": "/fixture", "approvalPolicy": "never",
                    "sandbox": {"type": "readOnly"}, **self.fork_change}
        if method == "turn/start":
            fork = params["threadId"]
            events = [
                {"method": "turn/started", "params": {"threadId": fork, "turn": {"id": "probe-turn"}}},
                {"method": "item/completed", "params": {"threadId": fork, "turnId": self.event_turn,
                    "item": {"type": "agentMessage", "phase": "final_answer", "text": self.output}}},
                {"method": "turn/completed", "params": {"threadId": fork,
                    "turn": {"id": "probe-turn", "status": "completed"}}},
            ]
            if self.compaction:
                events[1:1] = [
                    {"method": method, "params": {"threadId": fork, "turnId": "probe-turn",
                      "item": {"id": "compact-1", "type": "contextCompaction", "summary": "PRIVATE-CONTEXT [9,8,7]"}}}
                    for method in ("item/started", "item/completed")]
            self.events.extend(events)
            return {"turn": {"id": "probe-turn"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError("unexpected method: " + method)

    def notify(self, method, params=None):
        self.calls.append((method, params or {}))

    def next_event(self, deadline):
        if not self.events:
            raise SamplerError("timeout")
        return self.events.pop(0)

    def close(self):
        self.closed = True


class SamplerTests(unittest.TestCase):
    def sampler(self, **kwargs):
        clients = []
        commands = []
        snapshots = {}
        ownership = tempfile.TemporaryDirectory(prefix="beacon-snapshot-tests-")
        self.addCleanup(ownership.cleanup)

        def factory(command, env, timeout):
            client = FixtureClient(snapshots=snapshots, **kwargs)
            clients.append(client)
            commands.append((command, env))
            return client

        sampler = CodexSampler(env={"MODEL_BEACON_CODEX_PATH": sys.executable, "CODEX_THREAD_ID": "wrong"},
                               client_factory=factory, ownership_dir=Path(ownership.name))
        return sampler, clients, commands

    def test_availability_never_starts_a_process(self):
        sampler, clients, _ = self.sampler()
        for _ in range(10):
            self.assertIsNone(sampler.availability(SOURCE, CONFIG))
        self.assertEqual(clients, [])
        self.assertIsNotNone(sampler.availability(SOURCE, {**CONFIG, "effort": None}))
        self.assertIsNotNone(sampler.availability("invalid target", CONFIG))

    def test_preparation_only_checks_runtime_and_target_not_missing_settings(self):
        sampler, clients, _ = self.sampler()
        self.assertIsNone(sampler.preparation_availability(SOURCE))
        self.assertIsNone(sampler.preparation_availability(NEW_SOURCE))
        self.assertIsNotNone(sampler.preparation_availability("new-sample-untrusted"))
        self.assertIsNotNone(sampler.availability(NEW_SOURCE, {}))
        self.assertEqual(clients, [])

    def test_standalone_sampling_has_explicit_config_and_fresh_ephemeral_thread_per_answer(self):
        sampler, clients, _ = self.sampler()
        context = sampler.create_context(NEW_SOURCE, CONFIG)
        workspace = Path(context["cwd"])
        self.assertTrue(workspace.is_dir())
        self.assertEqual(context["sample_scope"], "new_session")
        self.assertEqual(context["source_session_id"], NEW_SOURCE)
        self.assertFalse(any(method == "turn/start" for method, _ in clients[0].calls))
        for _ in range(4):
            self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
        self.assertEqual(len(clients), 4)
        thread_ids = []
        for client in clients:
            self.assertTrue(client.closed)
            starts = [params for method, params in client.calls if method == "thread/start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["model"], CONFIG["model"])
            self.assertEqual(starts[0]["modelProvider"], CONFIG["provider"])
            self.assertEqual(starts[0]["config"]["model_reasoning_effort"], CONFIG["effort"])
            self.assertEqual(starts[0]["cwd"], str(workspace))
            self.assertTrue(starts[0]["ephemeral"])
            self.assertFalse(any(method in ("thread/read", "thread/fork", "thread/delete", "thread/resume", "thread/goal/get")
                                 for method, _ in client.calls))
            turns = [params for method, params in client.calls if method == "turn/start"]
            self.assertEqual(len(turns), 1)
            thread_ids.append(turns[0]["threadId"])
        self.assertEqual(len(set(thread_ids)), 4)
        with self.assertRaises(SamplerError):
            sampler.sample(context, "numeric challenge", CONFIG)
        sampler.finish_context(context)
        self.assertFalse(workspace.exists())

    def test_standalone_preflight_mismatch_cleans_only_its_owned_workspace(self):
        sampler, clients, _ = self.sampler(fork_change={"reasoningEffort": "low"})
        with self.assertRaises(SamplerError) as caught:
            sampler.create_context(NEW_SOURCE, CONFIG)
        self.assertEqual(caught.exception.code, "fork_mismatch")
        params = next(params for method, params in clients[0].calls if method == "thread/start")
        self.assertFalse(Path(params["cwd"]).exists())
        self.assertTrue(clients[0].closed)
        self.assertFalse(any(method == "turn/start" for method, _ in clients[0].calls))

    def test_standalone_mode_never_resolves_a_global_default_model(self):
        sampler, clients, _ = self.sampler()
        with self.assertRaises(SamplerError):
            sampler.create_context(NEW_SOURCE, {"provider": "openai", "effort": "ultra"})
        with self.assertRaises(SamplerError):
            sampler.read_settings(NEW_SOURCE)
        self.assertEqual(clients, [])

    def test_standalone_workspace_is_removed_on_application_shutdown(self):
        sampler, clients, _ = self.sampler()
        context = sampler.create_context(NEW_SOURCE, CONFIG)
        workspace = Path(context["cwd"])
        sampler.close()
        self.assertTrue(context["_closed"])
        self.assertTrue(clients[0].closed)
        self.assertFalse(workspace.exists())

    def test_explicit_runtime_requires_absolute_executable(self):
        with self.assertRaisesRegex(SamplerError, "绝对路径"):
            codex_executable({"MODEL_BEACON_CODEX_PATH": "./codex"})
        self.assertEqual(codex_executable({"MODEL_BEACON_CODEX_PATH": sys.executable}), sys.executable)

    def test_doctor_checks_an_actual_ephemeral_fork_without_inference(self):
        sampler, clients, commands = self.sampler()
        result = sampler.doctor(SOURCE, CONFIG)
        self.assertEqual(result["inference_requests"], 0)
        methods = [method for method, _ in clients[0].calls]
        self.assertIn("thread/fork", methods)
        self.assertNotIn("turn/start", methods)
        self.assertTrue(clients[0].closed)
        command, env = commands[0]
        self.assertIn("features.shell_tool=false", command)
        self.assertIn("features.hooks=false", command)
        self.assertIn("mcp_servers={}", command)
        # An incomplete provider override shadows Codex's built-in provider
        # and prevents a real app-server from even initializing.
        self.assertFalse(any(value.startswith("model_providers.") for value in command))
        self.assertNotIn("CODEX_THREAD_ID", env)

    def test_initial_and_three_confirmations_have_distinct_processes_and_fixed_boundary(self):
        sampler, clients, _ = self.sampler()
        context = sampler.create_context(SOURCE, CONFIG)
        for _ in range(4):
            self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
        with self.assertRaisesRegex(SamplerError, "初检加 3 次复测"):
            sampler.sample(context, "numeric challenge", CONFIG)
        self.assertEqual(len(clients), 4)
        self.assertTrue(all(client.closed for client in clients))
        for client in clients:
            forks = [params for method, params in client.calls if method == "thread/fork" and params.get("ephemeral")]
            self.assertEqual(len(forks), 1)
            self.assertEqual(forks[0]["threadId"], context["snapshot_id"])
            self.assertNotIn("lastTurnId", forks[0])
            self.assertTrue(forks[0]["ephemeral"])
            starts = [params for method, params in client.calls if method == "turn/start"]
            self.assertEqual(len(starts), 1)
            self.assertNotEqual(starts[0]["threadId"], SOURCE)
            self.assertEqual(starts[0]["effort"], "ultra")
            self.assertEqual(starts[0]["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
            self.assertFalse(any(method in ("thread/delete", "thread/resume", "thread/goal/set")
                                 for method, _ in client.calls))
        base = next(params for method, params in clients[0].calls if method == "thread/fork" and not params.get("ephemeral"))
        self.assertEqual(base["threadId"], SOURCE)
        self.assertTrue(base["deferGoalContinuation"])
        self.assertNotIn("lastTurnId", base)
        sampler.finish_context(context)
        deleted = [params["threadId"] for client in clients for method, params in client.calls if method == "thread/delete"]
        self.assertEqual(deleted, [context["snapshot_id"]])
        self.assertFalse(list(sampler.ownership_dir.glob("*.json")))

    def test_stage_timings_include_preparation_once_and_keep_only_metadata(self):
        sampler, clients, _ = self.sampler(compaction=True)
        context = sampler.create_context(SOURCE, CONFIG)
        self.assertEqual(context["phase"], "preparing")
        self.assertEqual(set(context["timings"]), {"startup_seconds", "metadata_seconds", "fork_seconds", "total_seconds"})
        self.assertTrue(all(isinstance(value, float) and value >= 0 for value in context["timings"].values()))
        for _ in range(2):
            self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
            timing = context["last_sample_timing"]
            for key in ("startup_seconds", "metadata_seconds", "fork_seconds", "model_seconds", "total_seconds"):
                self.assertGreaterEqual(timing[key], 0)
            self.assertGreaterEqual(timing["total_seconds"], timing["model_seconds"])
            self.assertEqual(timing["compaction_count"], 1)
            self.assertTrue(timing["model_completed"])
            self.assertIsNotNone(timing["first_answer_seconds"])
            self.assertEqual(context["phase"], "model")
            self.assertEqual(sampler.phase, "model")
            self.assertNotIn("PRIVATE", json.dumps(timing))
            self.assertNotIn("numeric challenge", json.dumps(timing))
        self.assertEqual(context["compaction_count"], 2)
        self.assertEqual(len(clients), 2)
        sampler.finish_context(context)

    def test_wrong_source_or_changed_configuration_never_falls_back(self):
        for change in ({"id": "wrong"}, {"model": "gpt-6-luna"}, {"reasoningEffort": "high"},
                       {"modelProvider": "other"}, {"model": None}, {"path": None}):
            with self.subTest(change=change):
                sampler, clients, _ = self.sampler(source_change=change)
                with self.assertRaises(SamplerError):
                    sampler.create_context(SOURCE, CONFIG)
                self.assertTrue(clients[0].closed)
                self.assertFalse(any(method == "turn/start" for method, _ in clients[0].calls))

    def test_empty_thread_can_fork_without_a_prior_message_or_rollout(self):
        sampler, clients, _ = self.sampler(turns=[], source_change={"path": None})
        context = sampler.create_context(SOURCE, CONFIG)
        self.assertIsNone(context["source_turn_id"])
        self.assertFalse(context["source_has_turns"])
        self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
        params = next(params for method, params in clients[0].calls if method == "thread/fork" and params.get("ephemeral"))
        self.assertNotIn("lastTurnId", params)
        self.assertEqual(params["threadId"], context["snapshot_id"])
        self.assertTrue(params["ephemeral"])
        sampler.finish_context(context)

    def test_new_messages_do_not_change_the_frozen_empty_sampling_context(self):
        sampler, clients, _ = self.sampler(turns=[], source_change={"path": None})
        context = sampler.create_context(SOURCE, CONFIG)
        clients[0].source_change = {"path": "/fixture/source.jsonl"}
        clients[0].turns = [{"id": "new-normal-turn", "status": "completed"}]
        # Future forks still inherit the captured empty base, even if the
        # real conversation acquires a new message meanwhile.
        context.pop("_prepared")
        self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
        turns = [params for method, params in clients[0].calls if method == "turn/start"]
        self.assertEqual(len(turns), 1)
        self.assertNotEqual(turns[0]["threadId"], SOURCE)
        sampler.finish_context(context)

    def test_read_settings_resolves_empty_thread_metadata_without_inference(self):
        sampler, clients, _ = self.sampler(turns=[], source_change={"path": None})
        settings = sampler.read_settings(SOURCE)
        self.assertEqual({key: settings[key] for key in CONFIG}, CONFIG)
        self.assertEqual(settings["cwd"], "/fixture")
        self.assertFalse(settings["source_has_turns"])
        self.assertFalse(settings["busy"])
        self.assertTrue(clients[0].closed)
        self.assertFalse(any(method in ("thread/fork", "turn/start", "thread/resume") for method, _ in clients[0].calls))

    def test_read_settings_checks_supplied_hints_but_allows_busy_configuration_read(self):
        sampler, clients, _ = self.sampler(status="inProgress")
        self.assertTrue(sampler.read_settings(SOURCE, {"model": CONFIG["model"]})["busy"])
        with self.assertRaises(SamplerError) as caught:
            sampler.read_settings(SOURCE, {"effort": "low"})
        self.assertEqual(caught.exception.code, "source_changed")
        self.assertTrue(all(client.closed for client in clients))

    def test_compaction_is_preprocessing_and_only_final_numeric_text_is_returned(self):
        sampler, clients, _ = self.sampler(compaction=True)
        context = sampler.create_context(SOURCE, CONFIG)
        self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
        self.assertTrue(context["compacted"])
        self.assertEqual(context["compaction_count"], 1)
        self.assertTrue(clients[0].closed)
        sampler.finish_context(context)

    def test_wrong_turn_answer_stays_rejected_after_compaction(self):
        sampler, clients, _ = self.sampler(compaction=True, event_turn="wrong-turn")
        context = sampler.create_context(SOURCE, CONFIG)
        with self.assertRaises(SamplerError) as caught:
            sampler.sample(context, "numeric challenge", CONFIG)
        self.assertEqual(caught.exception.code, "unexpected_turn")
        self.assertTrue(clients[0].closed)
        sampler.finish_context(context)

    def test_effective_fork_configuration_is_checked_before_send(self):
        for change in ({"model": "gpt-6-luna"}, {"reasoningEffort": "high"},
                       {"modelProvider": "other"}, {"cwd": "/wrong"},
                       {"sandbox": {"type": "dangerFullAccess"}}, {"approvalPolicy": "on-request"},
                       {"thread": {"id": SOURCE, "ephemeral": True, "path": None,
                                   "forkedFromId": SOURCE, "cwd": "/fixture"}}):
            with self.subTest(change=change):
                sampler, clients, _ = self.sampler(fork_change=change)
                with self.assertRaises(SamplerError):
                    sampler.create_context(SOURCE, CONFIG)
                self.assertTrue(clients[0].closed)
                self.assertFalse(any(method == "turn/start" for method, _ in clients[0].calls))

    def test_busy_source_is_frozen_without_waiting_or_resuming_original(self):
        sampler, clients, _ = self.sampler(status="inProgress", source_change={"status": {"type": "active"}})
        context = sampler.create_context(SOURCE, CONFIG)
        self.assertEqual(context["source_turn_id"], "source-turn")
        self.assertEqual(context["source_turn_status"], "interrupted")
        self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
        self.assertFalse(any(method in ("thread/resume", "turn/interrupt") and params["threadId"] == SOURCE
                             for client in clients for method, params in client.calls))
        self.assertFalse(any(method == "turn/start" and params["threadId"] == SOURCE
                             for client in clients for method, params in client.calls))
        sampler.finish_context(context)

    def test_fixed_base_forks_without_native_last_turn_index(self):
        # Native paginated metadata can say interrupted while lastTurnId's
        # underlying rollout index still says in-progress. The already frozen
        # whole-base fork is the identical boundary and avoids that index.
        sampler, clients, _ = self.sampler(status="inProgress", reject_last_turn=True)
        context = sampler.create_context(SOURCE, CONFIG)
        self.assertEqual(context["source_turn_status"], "interrupted")
        self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
        for client in clients:
            for method, params in client.calls:
                if method == "thread/fork": self.assertNotIn("lastTurnId", params)
                self.assertNotEqual(method, "thread/resume")
        sampler.finish_context(context)

    def test_base_changed_during_fork_is_rejected_before_any_model_request(self):
        sampler, clients, _ = self.sampler(mutate_base_during_fork=True)
        with self.assertRaises(SamplerError) as caught:
            sampler.create_context(SOURCE, CONFIG)
        self.assertEqual(caught.exception.code, "snapshot_changed")
        self.assertEqual(caught.exception.stage, "branch")
        self.assertFalse(any(method == "turn/start" for client in clients for method, _ in client.calls))
        self.assertFalse(any(method == "thread/delete" for client in clients for method, _ in client.calls))

    def test_preparation_errors_record_exact_fixed_stage(self):
        for kwargs, stage in (({"source_change": {"model": "gpt-6-luna"}}, "configuration"),
                              ({"status": "unknown"}, "snapshot"),
                              ({"fork_change": {"model": "gpt-6-luna"}}, "branch")):
            with self.subTest(stage=stage):
                sampler, clients, _ = self.sampler(**kwargs)
                with self.assertRaises(SamplerError) as caught:
                    sampler.create_context(SOURCE, CONFIG)
                self.assertEqual(caught.exception.stage, stage)
                self.assertFalse(any(method == "turn/start" for client in clients for method, _ in client.calls))

    def test_invalid_snapshot_boundary_is_rejected_without_sampling(self):
        sampler, clients, _ = self.sampler(status="unknown")
        with self.assertRaises(SamplerError):
            sampler.create_context(SOURCE, CONFIG)
        self.assertTrue(clients[0].closed)
        self.assertFalse(any(method == "turn/start" for method, _ in clients[0].calls))

    def test_reopened_terminal_boundaries_are_explicitly_identified(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                sampler, clients, _ = self.sampler(status=status)
                result = sampler.doctor(SOURCE, CONFIG)
                self.assertEqual(result["source_turn_status"], status)
                self.assertEqual(result["source_turn_id"], "source-turn")
                self.assertEqual(result["inference_requests"], 0)

    def test_source_configuration_can_change_after_snapshot_without_changing_sample(self):
        sampler, clients, _ = self.sampler()
        context = sampler.create_context(SOURCE, CONFIG)
        clients[0].source_change = {"reasoningEffort": "low"}
        self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
        self.assertTrue(clients[0].closed)
        self.assertEqual(context["_attempts"], 1)
        turn = next(params for method, params in clients[0].calls if method == "turn/start")
        self.assertEqual(turn["effort"], "ultra")
        sampler.finish_context(context)

    def test_snapshot_ancestry_settings_and_entire_boundary_are_rechecked(self):
        for change in ("parent", "path", "settings", "boundary"):
            with self.subTest(change=change):
                sampler, clients, _ = self.sampler(turns=[{"id": "first", "status": "completed"},
                                                        {"id": "source-turn", "status": "completed"}])
                context = sampler.create_context(SOURCE, CONFIG)
                base = clients[0].snapshots[context["snapshot_id"]]
                if change == "parent": base["thread"]["forkedFromId"] = "unrelated-source"
                elif change == "path": base["thread"]["path"] = "/fixture/other.jsonl"
                elif change == "settings": base["thread"]["reasoningEffort"] = "low"
                else: base["turns"][0]["status"] = "interrupted"
                with self.assertRaises(SamplerError) as caught:
                    sampler.sample(context, "numeric challenge", CONFIG)
                self.assertEqual(caught.exception.code, "snapshot_changed")
                self.assertFalse(any(method == "turn/start" for method, _ in clients[0].calls))
                sampler.finish_context(context)
                self.assertFalse(any(method == "thread/delete" for client in clients for method, _ in client.calls))
                self.assertTrue(list(sampler.ownership_dir.glob("*.json")))

    def test_persisted_snapshot_descendants_prevent_cascade_deletion(self):
        sampler, clients, _ = self.sampler(descendants=[{"id": "user-descendant"}])
        context = sampler.create_context(SOURCE, CONFIG)
        sampler.finish_context(context)
        self.assertFalse(context["snapshot_cleanup_complete"])
        self.assertFalse(any(method == "thread/delete" for client in clients for method, _ in client.calls))
        ledger = json.loads(next(sampler.ownership_dir.glob("*.json")).read_text())
        self.assertEqual(ledger["snapshot"]["id"], context["snapshot_id"])
        self.assertEqual(ledger["status"], "pending")

    def test_cleanup_checks_both_active_and_archived_descendants_before_delete(self):
        sampler, clients, _ = self.sampler()
        context = sampler.create_context(SOURCE, CONFIG)
        sampler.finish_context(context)
        requests = [params for client in clients for method, params in client.calls if method == "thread/list"]
        self.assertEqual([row["archived"] for row in requests], [False, True])
        self.assertTrue(all(row["ancestorThreadId"] == context["snapshot_id"] and row["sourceKinds"]
                            and row["useStateDbOnly"] for row in requests))
        self.assertTrue(context["snapshot_cleanup_complete"])

    def test_pending_owned_cleanup_is_retried_without_model_requests(self):
        sampler, clients, _ = self.sampler(descendants=[{"id": "user-descendant"}])
        context = sampler.create_context(SOURCE, CONFIG)
        snapshot_id = context["snapshot_id"]
        sampler.finish_context(context)
        client = sampler._open(CONFIG)
        client.descendants = []
        sampler._recover_snapshots(client)
        self.assertNotIn(snapshot_id, client.snapshots)
        self.assertFalse(list(sampler.ownership_dir.glob("*.json")))
        self.assertFalse(any(method in ("turn/start", "thread/resume") for method, _ in client.calls))
        sampler._close_client(client)

    def test_recovery_cannot_delete_active_or_foreign_ownership_records(self):
        sampler, clients, _ = self.sampler()
        context = sampler.create_context(SOURCE, CONFIG)
        client = context["_client"]
        sampler._recover_snapshots(client)
        self.assertFalse(any(method == "thread/delete" for method, _ in client.calls))
        ledger_path = next(sampler.ownership_dir.glob("*.json"))
        original = ledger_path.read_text()
        for field, value in (("purpose", "another-app"), ("runtime_scope", "other-account")):
            record = json.loads(original)
            record[field] = value
            record["status"] = "pending"
            ledger_path.write_text(json.dumps(record))
            sampler._recover_snapshots(client)
            self.assertFalse(any(method == "thread/delete" for method, _ in client.calls))
        ledger_path.write_text(original)
        sampler.finish_context(context)

    def test_application_shutdown_closes_only_owned_ephemeral_runtime(self):
        sampler, clients, _ = self.sampler()
        context = sampler.create_context(SOURCE, CONFIG)
        sampler.close()
        self.assertTrue(clients[0].closed)
        self.assertTrue(context["_closed"])
        self.assertIsNotNone(sampler.availability(SOURCE, CONFIG))
        with self.assertRaises(SamplerError):
            sampler.sample(context, "numeric challenge", CONFIG)

    def test_active_goal_is_rejected_before_a_fork_can_auto_continue(self):
        sampler, clients, _ = self.sampler(goal={"threadId": SOURCE, "status": "active"})
        with self.assertRaisesRegex(SamplerError, "持续目标"):
            sampler.create_context(SOURCE, CONFIG)
        self.assertTrue(clients[0].closed)
        self.assertFalse(any(method in ("thread/fork", "turn/start", "thread/goal/set", "thread/goal/clear")
                             for method, _ in clients[0].calls))

    def test_shutdown_owns_clients_still_waiting_in_preflight(self):
        for blocking_method in ("initialize", "thread/read", "thread/fork"):
            with self.subTest(blocking_method=blocking_method):
                entered, release = threading.Event(), threading.Event()
                clients = []

                class BlockingClient(FixtureClient):
                    def request(self, method, params=None, timeout=None):
                        if method == blocking_method:
                            entered.set()
                            if not release.wait(2):
                                raise AssertionError("preflight shutdown did not release client")
                            raise SamplerError("context_closed")
                        return super().request(method, params, timeout)

                    def close(self):
                        super().close()
                        release.set()

                def factory(*args, **kwargs):
                    client = BlockingClient()
                    clients.append(client)
                    return client

                sampler = CodexSampler(env={"MODEL_BEACON_CODEX_PATH": sys.executable}, client_factory=factory)
                errors = []

                def worker():
                    try:
                        sampler.create_context(SOURCE, CONFIG)
                    except SamplerError as error:
                        errors.append(error)

                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(entered.wait(1))
                self.assertEqual(sampler._contexts, {})
                sampler.close()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertTrue(clients[0].closed)
                self.assertEqual(sampler._clients, {})
                self.assertEqual(len(errors), 1)

    def test_cancel_active_wakes_preparation_and_sampler_can_be_reused(self):
        for blocking_method in ("initialize", "thread/read", "thread/fork"):
            with self.subTest(blocking_method=blocking_method), tempfile.TemporaryDirectory() as directory:
                entered, release = threading.Event(), threading.Event()
                clients, shared = [], {}

                class BlockingClient(FixtureClient):
                    def request(self, method, params=None, timeout=None):
                        if method == blocking_method:
                            entered.set()
                            release.wait(3)
                            raise SamplerError("context_closed")
                        return super().request(method, params, timeout)

                    def close(self):
                        super().close()
                        release.set()

                def factory(*args, **kwargs):
                    kind = BlockingClient if not clients else FixtureClient
                    client = kind(snapshots=shared)
                    clients.append(client)
                    return client

                sampler = CodexSampler(env={"MODEL_BEACON_CODEX_PATH": sys.executable},
                                       client_factory=factory, ownership_dir=Path(directory))
                errors = []

                def worker():
                    try: sampler.create_context(SOURCE, CONFIG)
                    except SamplerError as error: errors.append(error)

                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                sampler.cancel_active()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertLess(time.monotonic() - started, 1)
                self.assertFalse(sampler._closed)
                self.assertTrue(clients[0].closed)
                self.assertEqual(sampler._clients, {})
                self.assertEqual(len(errors), 1)
                self.assertIsNone(sampler.availability(SOURCE, CONFIG))
                context = sampler.create_context(SOURCE, CONFIG)
                self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
                sampler.finish_context(context)
                self.assertFalse(list(Path(directory).glob("*.json")))
                sampler.close()

    def test_cancel_during_client_creation_closes_late_client_without_initialize(self):
        entered, release = threading.Event(), threading.Event()
        clients, errors = [], []
        with tempfile.TemporaryDirectory() as directory:
            def factory(*args, **kwargs):
                client = FixtureClient()
                clients.append(client)
                entered.set()
                release.wait(3)
                return client

            sampler = CodexSampler(env={"MODEL_BEACON_CODEX_PATH": sys.executable},
                                   client_factory=factory, ownership_dir=Path(directory))

            def worker():
                try: sampler.create_context(SOURCE, CONFIG)
                except SamplerError as error: errors.append(error)

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(entered.wait(1))
            sampler.cancel_active()
            release.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors[0].code, "cancelled")
            self.assertTrue(clients[0].closed)
            self.assertEqual(clients[0].calls, [])
            self.assertFalse(sampler._closed)
            self.assertEqual(sampler._clients, {})
            sampler.close()

    def test_cancel_active_sample_is_prompt_and_snapshot_cleanup_is_idempotent(self):
        entered, release = threading.Event(), threading.Event()
        clients, shared, errors = [], {}, []
        with tempfile.TemporaryDirectory() as directory:
            class BlockingEventsClient(FixtureClient):
                def next_event(self, deadline):
                    entered.set()
                    release.wait(3)
                    raise SamplerError("context_closed")

                def close(self):
                    super().close()
                    release.set()

            def factory(*args, **kwargs):
                kind = BlockingEventsClient if not clients else FixtureClient
                client = kind(snapshots=shared)
                clients.append(client)
                return client

            sampler = CodexSampler(env={"MODEL_BEACON_CODEX_PATH": sys.executable},
                                   client_factory=factory, ownership_dir=Path(directory))
            context = sampler.create_context(SOURCE, CONFIG)

            def worker():
                try: sampler.sample(context, "numeric challenge", CONFIG)
                except SamplerError as error: errors.append(error)

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            sampler.cancel_active()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(errors[0].code, "cancelled")
            self.assertTrue(context["_cancelled"])
            self.assertFalse(sampler._closed)
            self.assertTrue(clients[0].closed)
            sampler.finish_context(context)
            sampler.finish_context(context)
            self.assertFalse(list(Path(directory).glob("*.json")))
            deleted = [params["threadId"] for client in clients for method, params in client.calls if method == "thread/delete"]
            self.assertEqual(deleted, [context["snapshot_id"]])
            self.assertFalse(any(method in ("turn/start", "turn/interrupt", "thread/resume", "thread/delete")
                                 and params.get("threadId") == SOURCE for client in clients for method, params in client.calls))
            next_context = sampler.create_context(SOURCE, CONFIG)
            self.assertEqual(sampler.sample(next_context, "numeric challenge", CONFIG), "[1,2,3]")
            sampler.finish_context(next_context)
            sampler.close()


class StdioProtocolTests(unittest.TestCase):
    def client(self, behavior):
        # A real child process exercises message framing and event/response
        # races.  It is a protocol fixture, not a model-output reference.
        code = """
import json,sys
def out(message):
 print(json.dumps(message),flush=True)
for line in sys.stdin:
 message=json.loads(line)
 if message.get('method')=='initialize': out({'id':message['id'],'result':{}})
 if message.get('method')=='turn/start':
  fork=message['params']['threadId']
  BEHAVIOR
""".replace("  BEHAVIOR", "\n".join("  " + line for line in behavior.splitlines()))
        return AppServerClient([sys.executable, "-u", "-c", code], dict(os.environ), timeout=2)

    def test_notifications_before_start_response_are_not_lost(self):
        client = self.client("""out({'method':'turn/started','params':{'threadId':fork,'turn':{'id':'t'}}})
out({'method':'item/completed','params':{'threadId':fork,'turnId':'t','item':{'type':'agentMessage','text':'[1,2]'}}})
out({'method':'turn/completed','params':{'threadId':fork,'turn':{'id':'t','status':'completed'}}})
out({'id':message['id'],'result':{'turn':{'id':'t'}}})""")
        try:
            client.request("initialize")
            response = client.request("turn/start", {"threadId": "fork"})
            self.assertEqual(response["turn"]["id"], "t")
            events = [client.next_event(time.monotonic() + 2) for _ in range(3)]
            self.assertEqual([event["method"] for event in events],
                             ["turn/started", "item/completed", "turn/completed"])
        finally:
            client.close()
        self.assertIsNotNone(client.process.poll())

    def test_close_wakes_a_real_child_event_waiter_without_deadline_expiry(self):
        client = self.client("pass")
        entered, errors = threading.Event(), []

        def waiter():
            entered.set()
            try: client.next_event(time.monotonic() + 180)
            except SamplerError as error: errors.append(error)

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        client.cancel_pending()
        thread.join(1)
        client.close()
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(errors[0].code, "context_closed")
        self.assertIsNotNone(client.process.poll())

    def test_sampler_cancel_stops_real_owned_child_and_next_sampling_still_works(self):
        code = """
import json,sys
def out(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
 message=json.loads(line);method=message.get('method');params=message.get('params',{})
 if method=='initialize':out({'id':message['id'],'result':{}})
 if method=='thread/start':
  result={'thread':{'id':'owned-fixture-thread','ephemeral':True,'path':None,'forkedFromId':None,'cwd':params['cwd']},
   'model':params['model'],'modelProvider':params['modelProvider'],'reasoningEffort':params['config']['model_reasoning_effort'],
   'cwd':params['cwd'],'approvalPolicy':'never','sandbox':{'type':'readOnly'}}
  out({'id':message['id'],'result':result})
 if method=='turn/start':
  thread=params['threadId']
  out({'method':'turn/started','params':{'threadId':thread,'turn':{'id':'fixture-turn'}}})
  out({'id':message['id'],'result':{'turn':{'id':'fixture-turn'}}})
  if sys.argv[1]=='complete':
   out({'method':'item/completed','params':{'threadId':thread,'turnId':'fixture-turn','item':{'type':'agentMessage','phase':'final_answer','text':'[1,2,3]'}}})
   out({'method':'turn/completed','params':{'threadId':thread,'turn':{'id':'fixture-turn','status':'completed'}}})
"""
        waiting = threading.Event()
        clients, errors = [], []

        class FixtureProtocolClient(AppServerClient):
            def next_event(self, deadline):
                waiting.set()
                return super().next_event(deadline)

        def factory(command, env, timeout):
            client = FixtureProtocolClient([sys.executable, "-u", "-c", code,
                                            "stall" if not clients else "complete"], env, timeout=timeout)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as directory:
            sampler = CodexSampler(env={"MODEL_BEACON_CODEX_PATH": sys.executable},
                                   client_factory=factory, ownership_dir=Path(directory))
            context = sampler.create_context(NEW_SOURCE, CONFIG)

            def worker():
                try:sampler.sample(context, "offline protocol fixture", CONFIG)
                except SamplerError as error:errors.append(error)

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(waiting.wait(1))
            started = time.monotonic()
            sampler.cancel_active()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(errors[0].code, "cancelled")
            self.assertIsNotNone(clients[0].process.poll())
            self.assertFalse(sampler._closed)
            sampler.finish_context(context)
            next_context = sampler.create_context(NEW_SOURCE, CONFIG)
            self.assertEqual(sampler.sample(next_context, "offline protocol fixture", CONFIG), "[1,2,3]")
            sampler.finish_context(next_context)
            sampler.close()
            self.assertTrue(all(client.process.poll() is not None for client in clients))

    def test_unrequested_continuation_is_rejected(self):
        client = self.client("""out({'method':'turn/started','params':{'threadId':fork}})
out({'method':'turn/started','params':{'threadId':fork}})
out({'id':message['id'],'result':{'turn':{'id':'t'}}})""")
        try:
            with self.assertRaisesRegex(SamplerError, "未请求"):
                client.request("turn/start", {"threadId": "fork"})
        finally:
            client.close()

    def test_tools_and_server_permission_requests_are_rejected(self):
        for behavior in (
            """out({'method':'turn/started','params':{'threadId':fork}})
out({'method':'item/started','params':{'threadId':fork,'item':{'type':'commandExecution'}}})""",
            "out({'id':99,'method':'item/permissions/requestApproval','params':{'threadId':fork}})",
        ):
            with self.subTest(behavior=behavior):
                client = self.client(behavior)
                try:
                    with self.assertRaisesRegex(SamplerError, "工具"):
                        client.request("turn/start", {"threadId": "fork"})
                finally:
                    client.close()

    def test_raw_provider_error_is_not_exposed(self):
        client = self.client("out({'id':message['id'],'error':{'message':'secret-token-and-user-transcript'}})")
        try:
            with self.assertRaises(SamplerError) as caught:
                client.request("turn/start", {"threadId": "fork"})
            self.assertNotIn("secret", str(caught.exception))
            self.assertIn(caught.exception.safe_reason, SAFE_SAMPLER_REASONS)
        finally:
            client.close()

    def test_context_compaction_keeps_only_metadata_and_allows_final_answer(self):
        client = self.client("""out({'method':'turn/started','params':{'threadId':fork}})
out({'method':'item/started','params':{'threadId':fork,'turnId':'t','item':{'id':'c','type':'contextCompaction','summary':'PRIVATE-CONTEXT [9,8,7]'}}})
out({'method':'item/completed','params':{'threadId':fork,'turnId':'t','item':{'id':'c','type':'contextCompaction','summary':'PRIVATE-CONTEXT [9,8,7]'}}})
out({'method':'item/completed','params':{'threadId':fork,'turnId':'t','item':{'type':'agentMessage','phase':'final_answer','text':'[1,2]'}}})
out({'method':'turn/completed','params':{'threadId':fork,'turn':{'id':'t','status':'completed'}}})
out({'id':message['id'],'result':{'turn':{'id':'t'}}})""")
        try:
            self.assertEqual(client.request("turn/start", {"threadId": "fork"})["turn"]["id"], "t")
            events = [client.next_event(time.monotonic() + 2) for _ in range(5)]
            self.assertNotIn("PRIVATE-CONTEXT", json.dumps(events))
            compactions = [event for event in events if event.get("params", {}).get("item", {}).get("type") == "contextCompaction"]
            self.assertEqual(len(compactions), 2)
            self.assertTrue(all(event["params"]["item"] == {"id": "c", "type": "contextCompaction"} for event in compactions))
            self.assertIn("[1,2]", json.dumps(events))
        finally:
            client.close()

    def test_native_turn_timing_includes_compaction_and_uses_event_arrival_time(self):
        # Deliberately deliver the RPC response after every notification. The
        # timer must measure turn/completed arrival, not delayed consumption.
        client = self.client("""import time
out({'method':'turn/started','params':{'threadId':fork,'turn':{'id':'t'}}})
out({'method':'item/started','params':{'threadId':fork,'turnId':'t','item':{'id':'c','type':'contextCompaction','summary':'PRIVATE-COMPACTION'}}})
time.sleep(.025)
out({'method':'item/completed','params':{'threadId':fork,'turnId':'t','item':{'id':'c','type':'contextCompaction','summary':'PRIVATE-COMPACTION'}}})
out({'method':'item/started','params':{'threadId':fork,'turnId':'t','item':{'type':'reasoning'}}})
time.sleep(.025)
out({'method':'item/started','params':{'threadId':fork,'turnId':'t','item':{'type':'agentMessage'}}})
time.sleep(.025)
out({'method':'item/completed','params':{'threadId':fork,'turnId':'t','item':{'type':'agentMessage','phase':'final_answer','text':'[1,2,3]'}}})
out({'method':'turn/completed','params':{'threadId':fork,'turn':{'id':'t','status':'completed'}}})
time.sleep(.075)
out({'id':message['id'],'result':{'turn':{'id':'t'}}})""")
        sampler = CodexSampler(env={"MODEL_BEACON_CODEX_PATH": sys.executable})
        context = {"_config": CONFIG, "model": CONFIG["model"], "provider": CONFIG["provider"],
                   "effort": CONFIG["effort"], "sample_scope": "new_session", "source_session_id": NEW_SOURCE,
                   "cwd": "/fixture", "_client": client, "_prepared": {"id": "fixture-fork"},
                   "_attempts": 0, "_closed": False, "compaction_count": 0, "compacted": False}
        try:
            client.request("initialize")
            self.assertEqual(sampler.sample(context, "numeric challenge", CONFIG), "[1,2,3]")
            timing = context["last_sample_timing"]
            self.assertTrue(timing["model_completed"])
            self.assertEqual(timing["compaction_count"], 1)
            self.assertGreaterEqual(timing["first_reasoning_seconds"], .02)
            self.assertGreater(timing["first_answer_seconds"], timing["first_reasoning_seconds"])
            self.assertGreater(timing["model_seconds"], timing["first_answer_seconds"])
            self.assertGreater(timing["total_seconds"] - timing["model_seconds"], .05)
            self.assertNotIn("PRIVATE", json.dumps(timing))
            self.assertNotIn("text", timing)
            self.assertTrue(context["compacted"])
        finally:
            sampler.finish_context(context)

    def test_failed_native_turn_also_records_elapsed_stage_metadata(self):
        client = self.client("""import time
out({'method':'turn/started','params':{'threadId':fork,'turn':{'id':'t'}}})
time.sleep(.025)
out({'method':'turn/completed','params':{'threadId':fork,'turn':{'id':'t','status':'failed'}}})
out({'id':message['id'],'result':{'turn':{'id':'t'}}})""")
        sampler = CodexSampler(env={"MODEL_BEACON_CODEX_PATH": sys.executable})
        context = {"_config": CONFIG, "sample_scope": "new_session", "source_session_id": NEW_SOURCE,
                   "cwd": "/fixture", "_client": client, "_prepared": {"id": "fixture-fork"},
                   "_attempts": 0, "_closed": False, "compaction_count": 0, "compacted": False}
        try:
            client.request("initialize")
            with self.assertRaises(SamplerError):
                sampler.sample(context, "numeric challenge", CONFIG)
            timing = context["last_sample_timing"]
            self.assertFalse(timing["model_completed"])
            self.assertGreaterEqual(timing["model_seconds"], .02)
            self.assertGreaterEqual(timing["total_seconds"], timing["model_seconds"])
        finally:
            sampler.finish_context(context)

    def test_shutdown_wakes_a_pending_native_protocol_request(self):
        client = self.client("pass")
        errors = []

        def worker():
            try:
                client.request("turn/start", {"threadId": "fork"}, timeout=30)
            except SamplerError as error:
                errors.append(error)

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 1
        while not client._pending and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertTrue(client._pending)
        client.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn(errors[0].code, ("context_closed", "transport_closed"))


if __name__ == "__main__":
    unittest.main()
