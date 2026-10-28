"""Synthetic, offline acceptance checks for attribution scheduling and isolation."""

import json
import random
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from attribution import AttributionEngine, FingerprintLibrary, ReferencePlan, MAX_HISTORY, MAX_EVENTS


CONFIG = {"model": "gpt-6.1-sol", "effort": "high", "provider": "test", "context_segment": "context-1"}


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class SyntheticLibrary:
    """Explicit test data, never distributed as a real model fingerprint."""
    version = "synthetic-test-only"

    def readiness(self, config):
        return ReferencePlan(self.version, config["model"], config["provider"], config["effort"],
                             1, ("synthetic numeric prompt",), "test-only"), None

    def parse(self, response, plan):
        return response if isinstance(response, int) else None

    def evaluate(self, samples, plan):
        return {"outcome": "close" if samples == [1] else "different", "reason": "Synthetic test result."}


class Sampler:
    def __init__(self, responses=None, block=False, bad_context=False):
        self.responses = [1, 1, 1] if responses is None else responses
        self.calls = []
        self.created = []
        self.finished = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.block, self.bad_context = block, bad_context

    def availability(self, session_id, config):
        return None

    def create_context(self, session_id, config):
        self.created.append(session_id)
        return dict(config, source_session_id="wrong" if self.bad_context else session_id, temporary=True)

    def sample(self, context, prompt, config):
        self.calls.append((context["source_session_id"], dict(config)))
        if self.block and len(self.calls) == 1:
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("test timed out")
        response = self.responses[(len(self.calls) - 1) % len(self.responses)]
        if isinstance(response, Exception):
            raise response
        return response

    def finish_context(self, context):
        self.finished.append(context["source_session_id"])


class OneSampleLibrary(SyntheticLibrary):
    def readiness(self, config):
        if any(not config.get(key) for key in ("model", "provider", "effort")):
            return None, "缺少明确的会话配置记录，无法开始采样。"
        return ReferencePlan(self.version, config["model"], config["provider"], config["effort"],
                             1, ("synthetic numeric prompt",), "test-only",
                             {"retry_on_difference": True}), None

    def evaluate(self, samples, plan):
        return {"outcome": "close" if sum(samples) >= len(samples) else "different",
                "candidate": plan.model if sum(samples) >= len(samples) else "gpt-5.6-sol",
                "sample_candidates": [plan.model if value else "gpt-5.6-sol" for value in samples],
                "reason": "Synthetic test result."}


class ResolvingSampler(Sampler):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.settings_reads = []

    def preparation_availability(self, session_id):
        return None

    def read_settings(self, session_id, hints):
        self.settings_reads.append((session_id, dict(hints)))
        return dict(CONFIG, source_has_turns=False, busy=False)


class InterruptibleSampler(Sampler):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cancel_calls = 0
        self.cancel_entered = threading.Event()
        self.cancel_finish = threading.Event()
        self.cancel_finish.set()

    def cancel_active(self):
        self.cancel_calls += 1
        self.cancel_entered.set()
        self.release.set()
        self.cancel_finish.wait(3)


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.json"
        self.clock = Clock()
        self.sampler = Sampler()
        self.engine = self.make_engine()

    def tearDown(self):
        self.sampler.release.set()
        self.engine.close()
        if self.engine._worker:
            self.engine._worker.join(3)
        self.temporary.cleanup()

    def make_engine(self, **kwargs):
        return AttributionEngine(self.path, adapter=kwargs.pop("adapter", self.sampler),
                                 library=kwargs.pop("library", SyntheticLibrary()), clock=self.clock,
                                 rng=random.Random(1), **kwargs)

    def observe(self, session="A", turn="turn-1", config=None, busy=False, rounds=0, tools=0, **kwargs):
        self.engine.observe(session, turn, config or CONFIG, busy, rounds, tools, True, **kwargs)

    def finish(self):
        self.engine.advance()
        worker = self.engine._worker
        if worker:
            worker.join(3)
            self.assertFalse(worker.is_alive(), "offline worker did not finish")

    def manual(self, session="A"):
        self.assertTrue(self.engine.request_manual(session)["accepted"])
        self.finish()

    def test_manual_off_persists_until_next_normal_turn_and_never_sends_again(self):
        self.observe()
        self.manual()
        result = self.engine.snapshot("A")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["scope"], "current")
        self.assertEqual(result["result"]["outcome"], "close")
        self.clock.value += 100_000
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)
        self.observe(turn="turn-2", busy=True)
        self.assertEqual(self.engine.snapshot("A")["scope"], "previous_turn")
        self.assertEqual(self.engine.snapshot("A")["result"]["label"], "归因匹配")
        self.observe(turn="turn-2", rounds=1)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)

    def test_late_result_stays_in_captured_session(self):
        self.sampler.block = True
        self.observe()
        self.assertTrue(self.engine.request_manual("A")["accepted"])
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        self.engine.suspend("A")
        self.observe(session="B", turn="b-1")
        self.assertIsNone(self.engine.snapshot("B")["result"])
        self.assertFalse(self.engine.snapshot("B")["available"])
        self.sampler.release.set()
        self.engine._worker.join(3)
        self.assertEqual({call[0] for call in self.sampler.calls}, {"A"})
        self.assertIsNone(self.engine.snapshot("B")["result"])
        self.assertEqual(self.engine.snapshot("A")["result"]["session_id"], "A")

    def test_configuration_change_invalidates_old_scope(self):
        self.observe()
        self.manual()
        self.observe(config=dict(CONFIG, effort="medium"))
        self.assertEqual(self.engine.snapshot("A")["scope"], "previous_config")
        self.observe(config=dict(CONFIG, context_segment="context-2"))
        self.assertEqual(self.engine.snapshot("A")["scope_label"], "上一配置的归因")

    def test_auto_uses_tool_checkpoints_not_round_counts_or_wall_clock(self):
        self.observe()
        self.engine.set_automatic("A", True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)
        for index in range(1, 4):
            self.observe(turn=f"turn-{index + 1}", rounds=index)
        self.clock.value += 100_000
        self.observe(turn="turn-4", rounds=3)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)
        threshold = self.engine.snapshot("A")["tool_threshold"]
        self.observe(turn="turn-5", busy=True, rounds=3, tools=threshold)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 2)
        self.assertTrue(self.engine.sessions["A"].busy)

    def test_enable_while_busy_starts_initial_background_check(self):
        self.observe(busy=True)
        self.engine.set_automatic("A", True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertTrue(self.engine.sessions["A"].busy)
        self.observe(rounds=1)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)

    def test_busy_manual_can_cancel_queued_task_without_touching_normal_turn(self):
        self.observe(busy=True)
        self.assertTrue(self.engine.request_manual("A")["accepted"])
        self.assertTrue(self.engine.cancel("A"))
        self.finish()
        self.assertEqual(self.sampler.calls, [])
        self.assertTrue(self.engine.sessions["A"].busy)
        self.manual()
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertTrue(self.engine.sessions["A"].busy)

    def test_every_entry_uses_one_global_queue(self):
        self.observe()
        self.observe(session="B")
        self.assertTrue(self.engine.request_manual("A")["accepted"])
        self.assertFalse(self.engine.request_manual("A")["accepted"])
        self.assertFalse(self.engine.request_manual("B")["accepted"])
        self.engine.set_automatic("B", True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertEqual({call[0] for call in self.sampler.calls}, {"A"})

    def test_tool_threshold_runs_during_current_normal_turn_without_cooldown(self):
        self.observe()
        self.engine.set_automatic("A", True)
        self.finish()
        threshold = self.engine.snapshot("A")["tool_threshold"]
        self.assertGreaterEqual(threshold, 16)
        self.assertLessEqual(threshold, 32)
        self.observe(turn="turn-2", busy=True, tools=threshold - 1)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)
        self.observe(turn="turn-2", busy=True, tools=threshold)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 2)
        self.assertTrue(self.engine.sessions["A"].busy)
        next_threshold = self.engine.snapshot("A")["tool_threshold"]
        self.observe(turn="turn-2", busy=True, tools=threshold + next_threshold)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 3)

    def test_first_observation_does_not_count_history(self):
        self.observe(rounds=300, tools=5000)
        self.assertEqual(self.engine.snapshot("A")["rounds"], 0)
        self.assertEqual(self.engine.snapshot("A")["tools"], 0)
        self.observe(rounds=301, tools=5001)
        self.assertEqual(self.engine.snapshot("A")["rounds"], 1)
        self.assertEqual(self.engine.snapshot("A")["tools"], 1)

    def test_non_normal_attribution_activity_is_ignored(self):
        self.observe()
        self.observe(turn="probe", rounds=50, tools=300, normal=False)
        self.assertEqual(self.engine.snapshot("A")["rounds"], 0)
        self.assertEqual(self.engine.sessions["A"].turn_id, "turn-1")

    def test_disable_stops_unsent_automatic_samples_but_allows_sent_request_to_finish(self):
        self.sampler.block = True
        self.observe()
        self.engine.set_automatic("A", True)
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        self.engine.set_automatic("A", False)
        self.sampler.release.set()
        self.engine._worker.join(3)
        result = self.engine.snapshot("A")["result"]
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(self.engine.snapshot("A")["pending"])

    def test_disable_does_not_cancel_manual(self):
        self.sampler.block = True
        self.observe()
        self.engine.request_manual("A")
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        self.engine.set_automatic("A", False)
        self.sampler.release.set()
        self.engine._worker.join(3)
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertEqual(self.engine.snapshot("A")["result"]["source"], "manual")

    def test_manual_resets_tool_checkpoint_and_each_fresh_enable_checks_immediately(self):
        self.observe()
        self.manual()
        self.observe(rounds=2, tools=10)
        self.manual()
        self.assertEqual(len(self.sampler.calls), 2)
        self.assertEqual(self.engine.snapshot("A")["tools"], 0)
        self.engine.set_automatic("A", True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 3)
        self.engine.set_automatic("A", False)
        self.engine.set_automatic("A", True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 4)

    def test_configuration_change_forces_check_without_waiting_for_normal_completion(self):
        self.observe()
        self.engine.set_automatic("A", True)
        self.finish()
        changed = dict(CONFIG, effort="max")
        self.observe(config=changed, busy=True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 2)
        self.assertEqual(self.sampler.calls[-1][1]["effort"], "max")
        self.observe(turn="new-config", config=changed, rounds=1)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 2)

    def test_config_change_ends_old_confirmation_then_forces_new_segment_check(self):
        self.engine.close()
        self.engine = self.make_engine(library=OneSampleLibrary())
        self.sampler.responses = [0, 1]
        self.sampler.block = True
        self.observe(busy=True)
        self.engine.set_automatic("A", True)
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        changed = dict(CONFIG, model="gpt-6.1-terra", effort="max")
        self.observe(turn="turn-2", config=changed, busy=True)
        self.sampler.release.set()
        self.engine._worker.join(3)
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertTrue(all(config["model"] == CONFIG["model"] for _, config in self.sampler.calls))
        self.assertEqual(self.engine.snapshot("A")["scope"], "previous_config")
        self.assertEqual(self.engine.snapshot("A")["result"]["confirmation"]["status"], "incomplete")
        self.assertTrue(self.engine.snapshot("A")["pending"])
        self.observe(turn="turn-2", config=changed, busy=True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 2)
        self.assertEqual(self.sampler.calls[-1][1]["model"], changed["model"])

    def test_source_turn_advances_while_same_frozen_worker_finishes_all_samples(self):
        self.engine.close()
        self.engine = self.make_engine(library=OneSampleLibrary())
        self.sampler.responses = [0]
        self.sampler.block = True
        self.observe()
        self.engine.request_manual("A")
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        worker = self.engine._worker
        self.observe(turn="turn-2", busy=True)
        self.engine.advance()
        self.assertIs(self.engine._worker, worker)
        self.sampler.release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.sampler.calls), 4)
        state = self.engine.snapshot("A")
        self.assertEqual(state["scope"], "previous_turn")
        self.assertEqual(state["result"]["turn_id"], "turn-1")
        self.assertTrue(self.engine.sessions["A"].busy)
        self.assertEqual(state["result"]["confirmation"]["status"], "confirmed_mismatch")

    def test_integer_context_segments_are_distinct(self):
        self.observe(config=dict(CONFIG, context_segment=0))
        self.manual()
        self.observe(config=dict(CONFIG, context_segment=1))
        self.assertEqual(self.engine.snapshot("A")["scope"], "previous_config")

    def test_idless_normal_turn_completed_between_polls_invalidates_previous_result(self):
        self.observe(turn=None, busy=False, rounds=0)
        self.manual()
        self.assertEqual(self.engine.snapshot("A")["scope"], "current")
        self.observe(turn=None, busy=False, rounds=1)
        self.assertEqual(self.engine.snapshot("A")["scope"], "previous_turn")

    def test_idless_observed_start_and_completion_advance_generation_only_once(self):
        self.observe(turn=None, busy=False)
        self.observe(turn=None, busy=True)
        started = self.engine.sessions["A"].generation
        self.assertTrue(self.engine.request_manual("A")["accepted"])
        self.observe(turn=None, busy=False, rounds=1)
        self.assertEqual(self.engine.sessions["A"].generation, started)
        self.finish()
        self.assertEqual(self.engine.snapshot("A")["scope"], "current")
        self.observe(turn=None, busy=False, rounds=2)
        self.assertEqual(self.engine.snapshot("A")["scope"], "previous_turn")

    def test_initial_busy_manual_uses_same_generation_after_completion(self):
        self.observe(turn=None, busy=True)
        self.assertTrue(self.engine.request_manual("A")["accepted"])
        self.observe(turn=None, busy=False, rounds=1)
        self.finish()
        self.assertEqual(self.engine.snapshot("A")["scope"], "current")

    def test_unchanged_poll_does_not_rewrite_state(self):
        self.observe()
        before = self.path.stat().st_mtime_ns
        self.observe()
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_invalid_samples_and_failed_requests_end_single_checkpoint(self):
        for responses in ([None], [RuntimeError("SECRET-CREDENTIAL-DO-NOT-LOG")]):
            with self.subTest(responses=type(responses[0])):
                self.sampler.responses = responses
                self.sampler.calls.clear()
                self.observe()
                self.manual()
                result = self.engine.snapshot("A")["result"]
                self.assertEqual(len(self.sampler.calls), 1)
                self.assertEqual(result["attempts"], 1)
                self.assertEqual(result["valid_samples"], 0)
                self.assertEqual(result["outcome"], "inconclusive")
                self.assertNotIn("SECRET-CREDENTIAL", self.path.read_text())

    def test_wrong_temporary_context_never_sends(self):
        self.sampler.bad_context = True
        self.observe()
        self.manual()
        self.assertEqual(self.sampler.calls, [])
        self.assertEqual(self.engine.snapshot("A")["result"]["outcome"], "inconclusive")

    def test_missing_reference_and_selected_config_disable_sending(self):
        self.engine = self.make_engine(library=FingerprintLibrary())
        self.observe()
        self.assertFalse(self.engine.request_manual("A")["accepted"])
        self.assertIn("暂无该采样配置", self.engine.snapshot("A")["reference_status"])
        self.assertIn("暂无", self.engine.snapshot("A")["unavailable_reason"])
        self.assertEqual(self.sampler.calls, [])
        self.observe(session="missing-settings", config={"model": "gpt-6.1-sol"})
        self.assertIn("缺少明确", self.engine.snapshot("missing-settings")["unavailable_reason"])
        self.assertFalse(self.engine.request_manual("missing-settings")["accepted"])

    def test_missing_native_runtime_never_substitutes_a_model_sender(self):
        with patch("codex_sampler.codex_executable", return_value=None):
            self.engine = AttributionEngine(None, library=SyntheticLibrary())
            self.observe()
            self.assertIn("未找到", self.engine.snapshot("A")["unavailable_reason"])
            self.assertFalse(self.engine.request_manual("A")["accepted"])

    def test_snapshot_preparation_failure_is_reported_without_infinite_wait(self):
        from codex_sampler import SamplerError
        calls = []
        def unavailable(session, config):
            calls.append(session)
            raise SamplerError("source_busy")
        self.sampler.create_context = unavailable
        self.observe(busy=True)
        self.assertTrue(self.engine.request_manual("A")["accepted"])
        self.finish()
        state = self.engine.snapshot("A")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["result"]["label"], "准备采样失败")
        self.assertEqual(state["result"]["reason_short"], "快照准备失败")
        self.assertEqual(self.sampler.calls, [])
        self.clock.value += 1000
        self.finish()
        self.assertEqual(calls, ["A"])

    def test_production_manifest_is_supported_as_an_explicit_reference_path(self):
        self.engine = AttributionEngine(None, references_path=Path(__file__).resolve().parents[1] / "fingerprints.json",
                                        adapter=self.sampler)
        plan, reason = self.engine.library.readiness(dict(CONFIG, provider="openai"))
        self.assertIsNone(reason)
        self.assertIsNotNone(plan)
        self.assertTrue(plan.data["expected_enrolled"])

    def test_full_model_provider_effort_reference_coverage_is_exact(self):
        reference = Path(self.temporary.name) / "reference.json"
        reference.write_text(json.dumps({"schema_version": 1, "version": "synthetic-only", "entries": [{
            **CONFIG, "algorithm": "test", "required_samples": 3, "prompts": ["test"],
        }]}))
        library = FingerprintLibrary(reference, {"test": object()})
        self.assertIsNotNone(library.readiness(CONFIG)[0])
        for config in (dict(CONFIG, model="gpt-6.1-terra"), dict(CONFIG, provider="other"), dict(CONFIG, effort="max")):
            self.assertIsNone(library.readiness(config)[0])

    def test_restart_restores_global_switch_and_marks_history(self):
        self.observe()
        self.manual()
        self.engine.set_automatic("A", True)
        self.observe(session="B")
        self.engine.close()
        self.engine = self.make_engine()
        self.observe()
        self.assertTrue(self.engine.snapshot("A")["enabled"])
        self.assertEqual(self.engine.snapshot("A")["scope"], "history")
        self.assertTrue(self.engine.snapshot("A")["restored"])
        self.assertTrue(self.engine.snapshot("B")["enabled"])
        self.assertIsNone(self.engine.snapshot("B")["result"])
        self.finish()
        self.assertEqual(len(self.sampler.calls), 2)

    def test_legacy_per_session_opt_in_migrates_to_global_switch(self):
        legacy = {
            "schema_version": 1,
            "sessions": {
                "A": {"enabled": True, "config": CONFIG},
                "B": {"enabled": False, "config": CONFIG},
            },
            "events": [],
        }
        self.engine.close()
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        self.engine = self.make_engine()
        self.assertTrue(self.engine.snapshot("A")["enabled"])
        self.assertTrue(self.engine.snapshot("B")["enabled"])
        self.observe()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(saved["automatic_enabled"])

    def test_switch_keeps_global_auto_on_and_runs_new_session_initial_check(self):
        self.observe()
        self.engine.set_automatic("A", True)
        self.engine.suspend("A")
        self.observe(session="B")
        self.finish()
        self.assertEqual([call[0] for call in self.sampler.calls], ["B"])
        self.assertTrue(self.engine.snapshot("A")["enabled"])
        self.assertTrue(self.engine.snapshot("B")["enabled"])

    def test_disabling_global_auto_from_one_session_turns_it_off_for_all(self):
        self.observe()
        self.observe(session="B")
        self.engine.set_automatic("B", True)
        self.assertTrue(self.engine.snapshot("A")["enabled"])
        self.engine.set_automatic("B", False)
        self.assertFalse(self.engine.snapshot("A")["enabled"])
        self.assertFalse(self.engine.snapshot("B")["enabled"])

    def test_suspend_releases_queued_manual_before_first_request(self):
        self.observe(busy=True)
        self.assertTrue(self.engine.request_manual("A")["accepted"])
        self.engine.suspend("A")
        self.observe(session="B", turn="b-turn")
        self.assertIsNone(self.engine.snapshot("A")["task"])
        self.assertTrue(self.engine.request_manual("B")["accepted"])
        self.finish()
        self.assertEqual({call[0] for call in self.sampler.calls}, {"B"})
        self.assertTrue(self.engine.snapshot("A")["result"]["cancelled"])

    def test_suspend_manual_sent_request_finishes_only_current_sample_in_old_history(self):
        self.sampler.block = True
        self.observe()
        self.assertTrue(self.engine.request_manual("A")["accepted"])
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        self.engine.suspend("A")
        self.observe(session="B", turn="b-turn")
        self.sampler.release.set()
        self.engine._worker.join(3)
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertEqual(self.sampler.calls[0][0], "A")
        result = self.engine.snapshot("A")["result"]
        self.assertEqual(result["source"], "manual")
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["attempts"], 1)
        self.assertIsNone(self.engine.snapshot("B")["result"])
        self.assertTrue(self.engine.snapshot("B")["available"])

    def test_restart_forces_current_saved_configuration_check_while_busy(self):
        self.observe()
        self.engine.set_automatic("A", True)
        self.finish()
        changed = dict(CONFIG, model="gpt-6.1-terra", effort="max")
        self.observe(turn="old-turn", config=changed, busy=True)
        self.engine.close()
        self.engine = self.make_engine()
        self.observe(turn="old-turn", config=changed, busy=True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 2)
        self.assertEqual(self.sampler.calls[-1][1]["model"], changed["model"])
        self.assertTrue(self.engine.sessions["A"].busy)

    def test_restart_detects_selection_changed_while_app_was_closed(self):
        self.observe(turn="old-turn", busy=True)
        self.engine.set_automatic("A", True)
        self.engine.close()
        changed = dict(CONFIG, model="gpt-6.1-terra", effort="max")
        self.engine = self.make_engine()
        self.observe(turn="new-turn", config=changed, busy=True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertEqual(self.sampler.calls[0][1]["model"], changed["model"])

    def test_following_updates_scope_without_automatic_counts_or_requests(self):
        self.observe(turn=None)
        self.manual()
        self.engine.set_automatic("A", True)  # Recent same-config result avoids an initial duplicate.
        self.engine.observe("A", None, CONFIG, False, 1, 40, False,
                            normal=True, turn_config=CONFIG, allow_automatic=False)
        state = self.engine.snapshot("A")
        self.assertEqual(state["scope"], "previous_turn")
        self.assertEqual(state["rounds"], 0)
        self.assertEqual(state["tools"], 0)
        self.assertFalse(self.engine.sessions["A"].active)
        self.assertFalse(state["available"])
        changed = dict(CONFIG, effort="max")
        self.engine.observe("A", None, changed, False, 2, 80, False,
                            normal=True, turn_config=changed, allow_automatic=False)
        self.finish()
        self.assertEqual(self.engine.snapshot("A")["scope"], "previous_config")
        self.assertEqual(self.engine.snapshot("A")["rounds"], 0)
        self.assertEqual(self.engine.snapshot("A")["tools"], 0)
        self.assertEqual(len(self.sampler.calls), 1)

    def test_history_and_task_event_logs_are_bounded(self):
        self.observe()
        for _ in range(MAX_HISTORY + 8):
            self.manual()
        self.assertEqual(len(self.engine.snapshot("A")["history"]), MAX_HISTORY)
        self.assertLessEqual(len(self.engine.events), MAX_EVENTS)
        persisted = json.loads(self.path.read_text())
        self.assertLessEqual(len(persisted["events"]), MAX_EVENTS)
        self.assertNotIn("synthetic numeric prompt", self.path.read_text())

    def test_restore_uses_metadata_allowlist_and_drops_arbitrary_payloads(self):
        self.observe()
        self.manual()
        self.engine.close()
        saved = json.loads(self.path.read_text())
        result = saved["sessions"]["A"]["history"][-1]
        result["response"] = "SENSITIVE-RAW-ANSWER"
        result["credentials"] = {"token": "SENSITIVE-TOKEN"}
        result["reason"] = "SENSITIVE-FREEFORM-REASON"
        saved["events"][-1]["body"] = "SENSITIVE-RAW-BODY"
        saved["events"][-1]["reason"] = "SENSITIVE-ERROR"
        self.path.write_text(json.dumps(saved))
        self.engine = self.make_engine()
        self.observe()
        self.assertNotIn("SENSITIVE", self.path.read_text())
        self.assertTrue(self.engine.snapshot("A")["restored"])

    def test_first_matching_sample_finishes_without_two_extra_requests(self):
        self.engine.close()
        self.engine = self.make_engine(library=OneSampleLibrary())
        self.observe()
        self.manual()
        result = self.engine.snapshot("A")["result"]
        self.assertEqual((len(self.sampler.calls), result["attempts"], result["valid_samples"]), (1, 1, 1))
        self.assertEqual(result["outcome"], "close")

    def test_initial_mismatch_has_three_independent_retries_and_preserves_candidates(self):
        self.engine.close()
        self.sampler.responses = [0, 0, 0]
        self.engine = self.make_engine(library=OneSampleLibrary())
        self.observe()
        self.manual()
        result = self.engine.snapshot("A")["result"]
        self.assertEqual((len(self.sampler.calls), result["attempts"]), (4, 4))
        self.assertEqual(result["outcome"], "different")
        self.assertEqual(result["candidate"], "gpt-5.6-sol")
        self.assertEqual(result["sample_candidates"], ["gpt-5.6-sol"] * 4)
        self.assertEqual(result["confirmation"], {"initial_mismatch": True, "completed": 3, "required": 3, "status": "confirmed_mismatch"})
        self.assertEqual(len(result["decisions"]), 4)
        self.assertEqual(len(self.engine.snapshot("A")["difference_history"]), 1)

    def test_fatal_sampler_error_does_not_send_remaining_requests(self):
        from codex_sampler import SamplerError
        self.engine.close()
        self.sampler.responses = [SamplerError("tool_attempt")]
        self.engine = self.make_engine(library=OneSampleLibrary())
        self.observe()
        self.manual()
        result = self.engine.snapshot("A")["result"]
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertEqual((result["attempts"], result["valid_samples"]), (1, 0))
        self.assertEqual(result["label"], "采样未完成")
        self.assertEqual(self.sampler.finished, ["A"])

    def test_authentication_and_invalid_snapshot_failures_stop_immediately(self):
        from codex_sampler import SamplerError
        for code in ("authentication_required", "snapshot_changed", "snapshot_unavailable"):
            with self.subTest(code=code):
                self.engine.close()
                self.sampler = Sampler(responses=[SamplerError(code)])
                self.engine = self.make_engine(library=OneSampleLibrary())
                self.observe()
                self.manual()
                result = self.engine.snapshot("A")["result"]
                self.assertEqual(len(self.sampler.calls), 1)
                self.assertEqual((result["attempts"], result["valid_samples"]), (1, 0))
                self.assertEqual(result["label"], "采样未完成")
                self.assertEqual(self.sampler.finished, ["A"])

    def test_empty_known_session_resolves_config_before_first_request(self):
        self.engine.close()
        self.sampler = ResolvingSampler()
        self.engine = self.make_engine(library=OneSampleLibrary())
        hints = {"model": None, "provider": None, "effort": None, "context_segment": "empty:0"}
        self.engine.observe("empty", config=hints, context_available=False)
        self.assertTrue(self.engine.snapshot("empty")["manual_available"])
        self.assertTrue(self.engine.request_manual("empty")["accepted"])
        self.assertEqual(self.engine.snapshot("empty")["status"], "preparing")
        self.assertEqual(self.sampler.calls, [])
        self.finish()
        self.assertEqual(self.sampler.settings_reads, [("empty", hints)])
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertEqual(self.sampler.calls[0][1]["model"], CONFIG["model"])
        self.engine.observe("empty", config=hints, context_available=False)
        self.assertEqual(self.engine.snapshot("empty")["scope"], "current")
        self.assertEqual(self.engine.snapshot("empty")["result"]["config"]["context_segment"], "empty:0")

    def test_standalone_sample_isolated_from_monitored_session_and_can_cancel(self):
        self.engine.close()
        self.engine = self.make_engine(library=OneSampleLibrary())
        self.observe()
        response = self.engine.request_new_sample(CONFIG)
        self.assertTrue(response["accepted"])
        target = response["session_id"]
        self.assertTrue(target.startswith("new-sample-"))
        self.assertTrue(self.engine.cancel(target))
        self.finish()
        self.assertEqual(self.sampler.calls, [])
        response = self.engine.request_new_sample(CONFIG)
        target = response["session_id"]
        self.finish()
        result = self.engine.snapshot(target)["result"]
        self.assertEqual(result["sample_scope"], "new_session")
        self.assertFalse(self.engine.snapshot(target)["auto_available"])
        self.assertIsNone(self.engine.snapshot("A")["result"])
        self.assertEqual(self.sampler.created, [target])

    def test_empty_named_session_auto_resolves_config_even_when_source_is_busy(self):
        self.engine.close()
        self.sampler = ResolvingSampler()
        read = self.sampler.read_settings
        self.sampler.read_settings = lambda session, hints: dict(read(session, hints), busy=True)
        self.engine = self.make_engine(library=OneSampleLibrary())
        hints = {"model": None, "provider": None, "effort": None, "context_segment": "empty:0"}
        self.engine.observe("empty", config=hints, busy=True, context_available=False)
        self.engine.set_automatic("empty", True)
        self.finish()
        result = self.engine.snapshot("empty")["result"]
        self.assertEqual(result["source"], "automatic")
        self.assertEqual((result["attempts"], result["valid_samples"]), (1, 1))
        self.assertTrue(self.engine.sessions["empty"].busy)

    def test_captured_native_turn_is_retained_when_source_changes_during_preparation(self):
        create = self.sampler.create_context
        self.sampler.create_context = lambda session, config: dict(create(session, config), source_turn_id="frozen-turn")
        self.observe(turn="source-newer-turn", busy=True)
        self.manual()
        state = self.engine.snapshot("A")
        self.assertEqual(state["result"]["turn_id"], "frozen-turn")
        self.assertEqual(state["scope"], "previous_turn")

    def test_configuration_change_replaces_only_unsent_automatic_check(self):
        self.observe(busy=True)
        self.engine.set_automatic("A", True)
        original_task = self.engine.snapshot("A")["task"]["id"]
        changed = dict(CONFIG, effort="ultra")
        self.observe(config=changed, busy=True)
        self.assertNotEqual(original_task, self.engine.snapshot("A")["task"]["id"])
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)
        self.assertTrue(all(config["effort"] == "ultra" for _, config in self.sampler.calls))

    def test_unchanged_poll_during_long_turn_does_not_create_repeated_checks(self):
        self.observe(busy=True)
        self.engine.set_automatic("A", True)
        self.finish()
        for index in range(10):
            self.clock.value += 500
            self.observe(busy=True)
            self.finish()
        self.assertEqual(len(self.sampler.calls), 1)

    def test_metadata_resolution_cannot_overwrite_a_newer_observed_selection(self):
        self.engine.close()
        self.sampler = ResolvingSampler()
        self.engine = self.make_engine(library=OneSampleLibrary())
        hints = {"model": None, "provider": None, "effort": None, "context_segment": "before:0"}
        changed = dict(CONFIG, effort="ultra", context_segment="after:1")
        def resolve_while_source_advances(session, config):
            self.engine.observe(session, turn_id="later-turn", config=changed, busy=True,
                                context_available=True)
            return dict(CONFIG, busy=True, source_has_turns=True)
        self.sampler.read_settings = resolve_while_source_advances
        self.engine.observe("empty", config=hints, context_available=False)
        self.engine.request_manual("empty")
        self.finish()
        state = self.engine.snapshot("empty")
        self.assertEqual(self.engine.sessions["empty"].config["effort"], "ultra")
        self.assertEqual(self.engine.sessions["empty"].config["context_segment"], "after:1")
        self.assertTrue(state["result"]["cancelled"])
        self.assertEqual(self.sampler.calls, [])

    def use_confirmation_fixture(self, responses, evaluate=None):
        self.engine.close()
        self.sampler = Sampler(responses=responses)
        library = OneSampleLibrary()
        if evaluate is not None:
            library.evaluate = evaluate
        self.engine = self.make_engine(library=library)
        self.observe()

    def test_matching_retry_does_not_end_three_retries_or_erase_initial_difference(self):
        self.use_confirmation_fixture([0, 1, 0, 1])
        self.manual()
        state = self.engine.snapshot("A")
        result = state["result"]
        self.assertEqual((result["attempts"], result["valid_samples"]), (4, 4))
        self.assertEqual(result["confirmation"]["status"], "not_confirmed")
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["label"], "复测未持续异常")
        self.assertEqual([row["mismatch"] for row in result["decisions"]], [True, False, True, False])
        self.assertEqual(state["difference_history"][0]["decision"]["candidate"], "gpt-5.6-sol")
        self.assertEqual(result["initial_difference"], state["difference_history"][0])
        self.assertEqual(self.sampler.created, ["A"])

    def test_each_retry_is_independently_scored_and_alternative_candidates_can_differ(self):
        evaluations = []
        def evaluate(samples, plan):
            evaluations.append(list(samples))
            self.assertEqual(len(samples), 1)
            return {"outcome": "different", "candidate": "gpt-alt-" + str(samples[0]),
                    "mismatch": True, "classification": "difference_signal"}
        self.use_confirmation_fixture([0, 2, 3, 2], evaluate)
        self.manual()
        result = self.engine.snapshot("A")["result"]
        self.assertEqual(evaluations, [[0], [2], [3], [2]])
        self.assertEqual(result["confirmation"]["status"], "confirmed_mismatch")
        self.assertEqual(result["sample_candidates"], ["gpt-alt-0", "gpt-alt-2", "gpt-alt-3", "gpt-alt-2"])
        self.assertEqual(result["decisions"][-1]["classification"], "repeated_difference")

    def test_weak_ranking_mismatch_also_gets_three_independent_retries(self):
        def evaluate(samples, plan):
            return {"outcome": "inconclusive", "candidate": "gpt-alt", "mismatch": True,
                    "classification": "inconclusive", "closed_set_weight": 0.3, "expected_weight": 0.2}
        self.use_confirmation_fixture([0], evaluate)
        self.manual()
        result = self.engine.snapshot("A")["result"]
        self.assertEqual(result["attempts"], 4)
        self.assertEqual(result["confirmation"]["status"], "confirmed_mismatch")
        self.assertEqual(result["outcome"], "different")
        self.assertEqual(result["decisions"][0]["expected_weight"], 0.2)

    def test_unenrolled_expected_and_inconclusive_checkpoint_do_not_start_confirmation(self):
        for classification in ("unknown_expected_model", "inconclusive"):
            with self.subTest(classification=classification):
                self.use_confirmation_fixture([0], lambda samples, plan: {
                    "outcome": "inconclusive", "candidate": "gpt-alt" if classification == "unknown_expected_model" else None,
                    "classification": classification, "mismatch": False})
                self.manual()
                result = self.engine.snapshot("A")["result"]
                self.assertEqual(result["attempts"], 1)
                self.assertIsNone(result["confirmation"])
                self.assertEqual(result["outcome"], "inconclusive")

    def test_invalid_or_failed_retry_keeps_initial_difference_without_confirming(self):
        from codex_sampler import SamplerError
        for failed in (None, SamplerError("snapshot_changed"), SamplerError("authentication_required")):
            with self.subTest(failed=type(failed).__name__):
                self.use_confirmation_fixture([0, failed])
                self.manual()
                result = self.engine.snapshot("A")["result"]
                self.assertEqual((result["attempts"], result["valid_samples"]), (2, 1))
                self.assertEqual(result["confirmation"]["status"], "incomplete")
                self.assertEqual(result["outcome"], "inconclusive")
                self.assertEqual(result["label"], "复测未完成")
                self.assertTrue(result["initial_difference"]["decision"]["mismatch"])

    def test_initial_difference_is_visible_and_persisted_before_retries_finish(self):
        self.use_confirmation_fixture([0])
        sample = self.sampler.sample
        entered, release = threading.Event(), threading.Event()
        def block_second(context, prompt, config):
            response = sample(context, prompt, config)
            if len(self.sampler.calls) == 2:
                entered.set()
                release.wait(3)
            return response
        self.sampler.sample = block_second
        try:
            self.engine.request_manual("A")
            self.engine.advance()
            self.assertTrue(entered.wait(2))
            state = self.engine.snapshot("A")
            self.assertEqual(state["confirmation"], {"initial_mismatch": True, "completed": 0, "required": 3, "status": "active"})
            self.assertIsNone(state["preview_label"])
            self.assertIn("复测 1/3", state["progress"])
            self.assertEqual(state["current_candidate"], "gpt-5.6-sol")
            self.assertEqual(len(json.loads(self.path.read_text())["sessions"]["A"]["difference_history"]), 1)
            self.engine.cancel("A")
        finally:
            release.set()
            self.engine._worker.join(3)
        result = self.engine.snapshot("A")["result"]
        self.assertEqual(result["confirmation"]["status"], "incomplete")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["outcome"], "inconclusive")

    def test_confirmation_decisions_and_initial_history_survive_restart(self):
        self.use_confirmation_fixture([0, 1, 0, 1])
        self.manual()
        self.engine.close()
        self.engine = self.make_engine(library=OneSampleLibrary())
        self.observe()
        state = self.engine.snapshot("A")
        self.assertEqual(state["confirmation"]["status"], "not_confirmed")
        self.assertEqual(state["result"]["label"], "复测未持续异常")
        self.assertEqual(len(state["result"]["decisions"]), 4)
        self.assertEqual(len(state["result"]["sample_candidates"]), 4)
        self.assertEqual(len(state["difference_history"]), 1)

    def test_returning_to_enabled_binding_forces_one_resume_check(self):
        self.observe()
        self.engine.set_automatic("A", True)
        self.finish()
        self.engine.suspend("A")
        self.engine.observe("A", "turn-1", CONFIG, True, 0, 0, False, allow_automatic=False)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 1)
        self.observe(busy=True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 2)
        self.observe(busy=True)
        self.finish()
        self.assertEqual(len(self.sampler.calls), 2)

    def test_each_configuration_segment_field_cancels_remaining_manual_retries(self):
        for field, value in (("model", "gpt-6.1-terra"), ("provider", "other"),
                             ("effort", "ultra"), ("context_segment", "next-segment")):
            with self.subTest(field=field):
                self.use_confirmation_fixture([0])
                sample = self.sampler.sample
                entered, release = threading.Event(), threading.Event()
                def block_second(context, prompt, config):
                    response = sample(context, prompt, config)
                    if len(self.sampler.calls) == 2:
                        entered.set()
                        release.wait(3)
                    return response
                self.sampler.sample = block_second
                try:
                    self.engine.request_manual("A")
                    self.engine.advance()
                    self.assertTrue(entered.wait(2))
                    self.observe(config=dict(CONFIG, **{field: value}), busy=True)
                finally:
                    release.set()
                    self.engine._worker.join(3)
                state = self.engine.snapshot("A")
                self.assertEqual(len(self.sampler.calls), 2)
                self.assertEqual(state["result"]["confirmation"]["status"], "incomplete")
                self.assertEqual(state["scope"], "previous_config")
                self.assertEqual(state["result"]["config"], CONFIG)

    def test_temporary_missing_settings_on_normal_turn_do_not_cancel_frozen_confirmation(self):
        self.use_confirmation_fixture([0])
        sample = self.sampler.sample
        entered, release = threading.Event(), threading.Event()
        def block_second(context, prompt, config):
            response = sample(context, prompt, config)
            if len(self.sampler.calls) == 2:
                entered.set()
                release.wait(3)
            return response
        self.sampler.sample = block_second
        try:
            self.engine.request_manual("A")
            self.engine.advance()
            self.assertTrue(entered.wait(2))
            missing = {"model": None, "provider": None, "effort": None, "context_segment": "context-1"}
            self.engine.observe("A", "next-normal-turn", missing, True, 0, 0, True)
            self.assertFalse(self.engine._task.cancel.is_set())
            self.assertEqual(self.engine.sessions["A"].config, CONFIG)
        finally:
            release.set()
            self.engine._worker.join(3)
        state = self.engine.snapshot("A")
        self.assertEqual(len(self.sampler.calls), 4)
        self.assertEqual(state["confirmation"]["status"], "confirmed_mismatch")
        self.assertEqual(state["scope"], "previous_turn")

    def test_zero_request_preparation_failures_preserve_stage_and_short_reason(self):
        from codex_sampler import SamplerError
        for stage, short in (("configuration", "配置读取失败"), ("snapshot", "快照准备失败"), ("branch", "分支准备失败")):
            with self.subTest(stage=stage):
                self.use_confirmation_fixture([1])
                def unavailable(session, config):
                    error = SamplerError("source_busy")
                    error.stage = stage
                    raise error
                self.sampler.create_context = unavailable
                self.manual()
                result = self.engine.snapshot("A")["result"]
                self.assertEqual((result["attempts"], result["valid_samples"]), (0, 0))
                self.assertEqual(result["label"], "准备采样失败")
                self.assertEqual(result["reason_short"], short)
                self.assertEqual(result["failure_stage"], stage)
                self.assertEqual(self.sampler.calls, [])
                self.engine.close()
                self.engine = self.make_engine(library=OneSampleLibrary())
                self.observe()
                restored = self.engine.snapshot("A")["result"]
                self.assertEqual((restored["label"], restored["reason_short"]), ("准备采样失败", short))

    def test_sampling_failures_keep_specific_safe_short_reason(self):
        from codex_sampler import SamplerError
        for code, short in (("timeout", "请求超时"), ("turn_failed", "模型回合未完成")):
            with self.subTest(code=code):
                self.use_confirmation_fixture([1])
                def fail_sample(context, prompt, config):
                    raise SamplerError(code)
                self.sampler.sample = fail_sample
                self.manual()
                result = self.engine.snapshot("A")["result"]
                self.assertEqual((result["attempts"], result["valid_samples"]), (1, 0))
                self.assertEqual((result["label"], result["reason_short"]), ("采样未完成", short))
                self.engine.close()
                self.engine = self.make_engine(library=OneSampleLibrary())
                self.observe()
                restored = self.engine.snapshot("A")["result"]
                self.assertEqual((restored["label"], restored["reason_short"]), ("采样未完成", short))

    def use_interruptible_fixture(self):
        self.engine.close()
        self.sampler = InterruptibleSampler(block=True)
        self.engine = self.make_engine(library=OneSampleLibrary())
        self.observe()

    def test_active_cancel_is_immediate_and_holds_queue_until_abort_finishes(self):
        self.use_interruptible_fixture()
        self.sampler.cancel_finish.clear()
        self.engine.request_manual("A")
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        try:
            self.assertTrue(self.engine.cancel("A"))
            self.assertTrue(self.sampler.cancel_entered.wait(2))
            state = self.engine.snapshot("A")
            self.assertEqual(state["status"], "cancelling")
            self.assertEqual(state["progress"], "正在取消归因")
            self.assertFalse(state["task"]["cancellable"])
            self.assertFalse(state["manual_available"])
            self.assertFalse(self.engine.request_manual("A")["accepted"])
            self.assertTrue(self.engine.cancel("A"))
            self.assertEqual(self.sampler.cancel_calls, 1)
        finally:
            self.sampler.cancel_finish.set()
            self.engine._worker.join(3)
        state = self.engine.snapshot("A")
        self.assertTrue(state["result"]["cancelled"])
        self.assertEqual(state["result"]["label"], "已取消归因")
        self.assertTrue(state["manual_available"])
        self.manual()
        self.assertEqual(self.engine.snapshot("A")["result"]["outcome"], "close")
        self.assertFalse(self.engine.snapshot("A")["result"]["cancelled"])
        self.assertEqual(self.sampler.cancel_calls, 1)

    def test_cancel_during_preparation_aborts_without_sending_and_can_restart(self):
        self.use_interruptible_fixture()
        create = self.sampler.create_context
        def preparing(session, config):
            self.sampler.entered.set()
            self.sampler.release.wait(3)
            return create(session, config)
        self.sampler.create_context = preparing
        self.engine.request_manual("A")
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        self.engine.cancel("A")
        self.engine._worker.join(3)
        result = self.engine.snapshot("A")["result"]
        self.assertTrue(result["cancelled"])
        self.assertEqual((result["label"], result["attempts"]), ("已取消归因", 0))
        self.assertEqual(self.sampler.calls, [])
        self.manual()
        self.assertEqual(self.engine.snapshot("A")["result"]["outcome"], "close")

    def test_cancelled_flag_and_label_survive_restart_even_with_initial_difference(self):
        self.use_interruptible_fixture()
        self.sampler.responses = [0]
        self.engine.request_manual("A")
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        self.engine.cancel("A")
        self.engine._worker.join(3)
        self.engine.close()
        self.engine = self.make_engine(library=OneSampleLibrary())
        self.observe()
        result = self.engine.snapshot("A")["result"]
        self.assertEqual((result["cancelled"], result["label"], result["reason_short"]), (True, "已取消归因", "用户取消"))
        self.assertEqual(result["confirmation"]["status"], "incomplete")
        self.assertTrue(result["initial_difference"]["decision"]["mismatch"])

    def test_turning_auto_off_aborts_only_automatic_task(self):
        self.use_interruptible_fixture()
        self.engine.set_automatic("A", True)
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        self.engine.set_automatic("A", False)
        self.engine._worker.join(3)
        self.assertEqual(self.sampler.cancel_calls, 1)
        self.assertTrue(self.engine.snapshot("A")["result"]["cancelled"])
        self.sampler.calls.clear()
        self.sampler.entered.clear()
        self.sampler.release.clear()
        self.engine.request_manual("A")
        self.engine.advance()
        self.assertTrue(self.sampler.entered.wait(2))
        self.engine.set_automatic("A", False)
        self.assertEqual(self.sampler.cancel_calls, 1)
        self.sampler.release.set()
        self.engine._worker.join(3)
        self.assertFalse(self.engine.snapshot("A")["result"]["cancelled"])

    def test_stopping_worker_preserves_a_new_initial_check_and_its_counters(self):
        for trigger in ("return", "reenable", "configuration_roundtrip"):
            with self.subTest(trigger=trigger):
                self.use_interruptible_fixture()
                self.sampler.cancel_finish.clear()
                self.engine.set_automatic("A", True)
                self.engine.advance()
                self.assertTrue(self.sampler.entered.wait(2))
                old_worker = self.engine._worker
                try:
                    if trigger == "return":
                        self.engine.suspend("A")
                        self.observe(session="B")
                        self.engine.suspend("B")
                        self.observe()
                    elif trigger == "reenable":
                        self.engine.set_automatic(None, False)
                        self.engine.set_automatic("A", True)
                    else:
                        self.observe(config=dict(CONFIG, model="gpt-6-sol"))
                        self.observe()
                    self.assertTrue(self.sampler.cancel_entered.wait(2))
                    self.observe(rounds=1, tools=7)
                    before = self.engine.snapshot("A")
                    self.assertTrue(before["pending"])
                    self.assertEqual(before["tools"], 7)
                finally:
                    self.sampler.cancel_finish.set()
                    old_worker.join(3)
                self.assertFalse(old_worker.is_alive())
                stopped = self.engine.snapshot("A")
                self.assertTrue(stopped["pending"])
                self.assertEqual((stopped["tools"], stopped["tool_threshold"]),
                                 (7, before["tool_threshold"]))
                self.observe(rounds=1, tools=7)
                self.finish()
                self.assertEqual(len(self.sampler.calls), 2)
                self.assertFalse(self.engine.snapshot("A")["result"]["cancelled"])
                self.observe(rounds=1, tools=7)
                self.finish()
                self.assertEqual(len(self.sampler.calls), 2)

    def test_user_cancelled_automatic_check_does_not_immediately_requeue(self):
        for started in (False, True):
            with self.subTest(started=started):
                self.use_interruptible_fixture()
                self.engine.set_automatic("A", True)
                if started:
                    self.engine.advance()
                    self.assertTrue(self.sampler.entered.wait(2))
                # While this task owns the queue, normal tools can already pass
                # the next threshold. Cancelling still ends this checkpoint.
                threshold = self.engine.snapshot("A")["tool_threshold"]
                self.observe(tools=threshold)
                self.assertTrue(self.engine.cancel("A"))
                if started:
                    self.engine._worker.join(3)
                self.observe(tools=threshold)
                self.finish()
                state = self.engine.snapshot("A")
                self.assertTrue(state["enabled"])
                self.assertFalse(state["pending"])
                self.assertIsNone(state["task"])
                self.assertEqual(len(self.sampler.calls), int(started))

    def test_global_auto_can_be_seen_and_disabled_without_a_session(self):
        self.observe()
        self.engine.set_automatic("A", True)
        self.assertTrue(self.engine.snapshot(None)["enabled"])
        self.assertTrue(self.engine.set_automatic(None, False)["accepted"])
        self.assertFalse(self.engine.snapshot(None)["enabled"])
        self.assertFalse(self.engine.snapshot("A")["enabled"])
        self.assertFalse(self.engine.set_automatic(None, True)["accepted"])
        self.assertFalse(self.engine.snapshot(None)["enabled"])

    def test_switch_and_configuration_change_abort_only_old_task_then_allow_new_work(self):
        for change in ("session", "configuration"):
            with self.subTest(change=change):
                self.use_interruptible_fixture()
                self.engine.set_automatic("A", True)
                self.engine.advance()
                self.assertTrue(self.sampler.entered.wait(2))
                if change == "session":
                    self.engine.suspend("A")
                    self.observe(session="B")
                else:
                    self.observe(config=dict(CONFIG, model="gpt-6-sol"))
                self.engine._worker.join(3)
                self.assertEqual(self.sampler.cancel_calls, 1)
                self.assertTrue(self.engine.snapshot("A")["result"]["cancelled"])
                if change == "session":
                    self.manual("B")
                    self.assertEqual(self.engine.snapshot("B")["result"]["outcome"], "close")
                else:
                    self.observe(config=dict(CONFIG, model="gpt-6-sol"))
                    self.finish()
                    self.assertEqual(self.engine.snapshot("A")["result"]["model"], "gpt-6-sol")
                    self.assertFalse(self.engine.snapshot("A")["result"]["cancelled"])


if __name__ == "__main__":
    unittest.main()
