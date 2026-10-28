"""Session-scoped, opt-in attribution using isolated Codex sampling branches.

Platform identity evidence stays separate from statistical reference results.
Ordinary monitoring never sends; only a manual request or the global automatic
switch can start the bounded queue for the currently bound conversation. Tests
inject offline adapters and never infer models.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

MAX_REQUESTS = 4
CONFIRMATION_REQUESTS = 3
MAX_HISTORY = 32
MAX_SESSIONS = 128
MAX_EVENTS = 256
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
SCOPE_LABELS = {
    "current": "仅代表本次采样", "previous_turn": "上次归因，不代表本轮",
    "previous_config": "上一配置的归因", "history": "历史记录，非刚完成的检测",
}
SAFE_REASONS = {
    "采样返回无效，未计入有效样本。", "采样请求失败，已计入最多 4 次请求的上限。",
    "有效样本不足，未得出结论。", "已停止尚未发送的后续样本；已发送的请求已收尾。",
    "无法建立并验证目标会话的临时采样上下文，未得出结论。",
    "适用参考算法给出接近所选模型的指纹参考；仅代表本次采样。",
    "适用参考算法给出与所选模型不符的指纹参考；仅代表本次采样。",
    "适用参考算法未得出结论。", "参考算法未返回有效结论。",
    "所选配置已改变，已取消尚未发送的采样。", "自动数字归因已关闭。",
    "用户取消尚未发送的归因任务。", "归因任务已取消。", "已取消尚未发送的归因任务。",
    "监测对象已切换，取消旧会话尚未发送的自动样本。",
    "监测对象已切换，取消旧会话尚未发送的归因任务或后续样本。",
    "目标会话尚无可用的正常对话上下文。",
    "缺少明确的会话配置记录，无法开始采样。",
    "初次指纹差异已记录，正在进行三次独立复测。",
    "三次独立复测均与预期不一致。",
    "首次差异已保留，三次复测未全部与预期不一致。",
    "首次差异已保留，复测未完成。",
    "数字指纹参考库不可用。", "暂无该采样配置的数字指纹参考。",
    "暂无该型号的数字指纹参考。", "该参考库尚无经过验证的数字指纹算法，暂不支持发送采样。",
    "参考算法的有效样本要求无法在最多 4 次请求内满足。",
    "临时采样分支配置尚未完成验证，暂不支持数字归因。",
}


def _safe_reason(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) and value in SAFE_REASONS else "归因任务无法执行，请检查配置、参考库或临时采样入口。"


def _identifier(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) else None


def _name(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", value) else None


def _config(value: Optional[dict[str, Any]]) -> dict[str, Optional[str]]:
    value = value if isinstance(value, dict) else {}
    segment = value.get("context_segment")
    if isinstance(segment, int) and not isinstance(segment, bool) and segment >= 0:
        segment = str(segment)
    return {
        "model": _name(value.get("model")), "provider": _name(value.get("provider")),
        "effort": value.get("effort") if value.get("effort") in EFFORTS else None,
        "context_segment": _identifier(segment),
    }


def _count(value: Any) -> int:
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class ReferencePlan:
    """An exact model/provider/effort calibration and its sample requirements."""

    version: str
    model: str
    provider: str
    effort: str
    required_samples: int
    prompts: tuple[str, ...]
    algorithm: str
    data: dict[str, Any] = field(default_factory=dict)


class FingerprintLibrary:
    """Load exact coverage; algorithms must be independently implemented/reviewed.

    No uncalibrated nearest-model heuristic is supplied.  An algorithm registry
    may be injected by tests or a future reviewed integration.  Algorithm objects
    implement ``parse(response, plan)`` and ``evaluate(samples, plan)``.  Evaluate
    returns {outcome: close|different|inconclusive, reason: str}; no probability
    or accuracy is inferred from a raw distance.
    """

    def __init__(self, path: Optional[Path] = None, algorithms: Optional[dict[str, Any]] = None) -> None:
        self.version = "unavailable"
        self.entries: list[dict[str, Any]] = []
        self.algorithms = algorithms or {}
        self.error: Optional[str] = None
        try:
            source = path or Path(__file__).with_name("fingerprints.json")
            if source.stat().st_size > 1024 * 1024:
                raise ValueError("reference library too large")
            raw = json.loads(source.read_text(encoding="utf-8"))
            if raw.get("schema_version") != 1 or not isinstance(raw.get("entries"), list):
                raise ValueError("invalid reference schema")
            self.version = str(raw.get("version", "unavailable"))[:128]
            self.entries = [row for row in raw["entries"][:512] if isinstance(row, dict)]
        except (OSError, ValueError, TypeError):
            self.error = "数字指纹参考库不可用。"

    def readiness(self, config: dict[str, Any]) -> tuple[Optional[ReferencePlan], Optional[str]]:
        if self.error:
            return None, self.error
        row = next((entry for entry in self.entries if all(
            entry.get(key) == config.get(key) for key in ("model", "provider", "effort")
        )), None)
        if row is None:
            return None, "暂无该采样配置的数字指纹参考。"
        algorithm = row.get("algorithm")
        if algorithm not in self.algorithms:
            return None, "该参考库尚无经过验证的数字指纹算法，暂不支持发送采样。"
        required = row.get("required_samples")
        prompts = row.get("prompts")
        if (not isinstance(required, int) or isinstance(required, bool) or not 1 <= required <= MAX_REQUESTS or
                not isinstance(prompts, list) or not prompts or
                any(not isinstance(prompt, str) or not prompt or len(prompt) > 16_384 for prompt in prompts)):
            return None, "参考算法的有效样本要求无法在最多 4 次请求内满足。"
        plan = ReferencePlan(self.version, config["model"], config["provider"], config["effort"],
                             required, tuple(prompts[:MAX_REQUESTS]), algorithm, row.get("data", {}))
        return plan, None

    def parse(self, response: Any, plan: ReferencePlan) -> Any:
        return self.algorithms[plan.algorithm].parse(response, plan)

    def evaluate(self, samples: list[Any], plan: ReferencePlan) -> dict[str, str]:
        result = self.algorithms[plan.algorithm].evaluate(samples, plan)
        if not isinstance(result, dict) or result.get("outcome") not in ("close", "different", "inconclusive"):
            return {"outcome": "inconclusive", "reason": "参考算法未返回有效结论。"}
        return {"outcome": result["outcome"], "reason": str(result.get("reason", ""))[:512]}



def _clean_decision(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    output = {"outcome": raw.get("outcome") if raw.get("outcome") in ("close", "different", "inconclusive") else "inconclusive",
              "candidate": _name(raw.get("candidate")), "mismatch": raw.get("mismatch") is True,
              "classification": _identifier(raw.get("classification")),
              "reason_code": _identifier(raw.get("reason_code"))}
    for key in ("closed_set_weight", "expected_weight"):
        value = raw.get(key)
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1:
            output[key] = value
    return output


def _clean_confirmation(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("initial_mismatch") is not True:
        return None
    status = raw.get("status")
    if status not in ("active", "confirmed_mismatch", "not_confirmed", "incomplete"):
        return None
    return {"initial_mismatch": True, "completed": min(CONFIRMATION_REQUESTS, _count(raw.get("completed"))),
            "required": CONFIRMATION_REQUESTS, "status": status}


def _clean_difference(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict) or not _identifier(raw.get("id")) or not _identifier(raw.get("session_id")):
        return None
    output = {"id": raw["id"], "session_id": raw["session_id"], "turn_id": _identifier(raw.get("turn_id")),
              "config": _config(raw.get("config")), "decision": _clean_decision(raw.get("decision")),
              "snapshot_id": _identifier(raw.get("snapshot_id"))}
    value = raw.get("at")
    output["at"] = value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    return output

@dataclass
class _Session:
    enabled: bool = False
    config: dict[str, Any] = field(default_factory=lambda: _config(None))
    turn_id: Optional[str] = None
    busy: bool = False
    context_available: bool = False
    active: bool = True
    observed: bool = False
    restored_config: bool = False
    completed_base: int = 0
    tools_base: int = 0
    rounds: int = 0
    tools: int = 0
    tool_threshold: int = 16
    pending: bool = False
    # A new binding/configuration/enable request starts a distinct checkpoint.
    # An older worker must not consume a newer request while it is stopping.
    checkpoint_generation: int = 0
    last_completed_at: Optional[float] = None
    history: list[dict[str, Any]] = field(default_factory=list)
    difference_history: list[dict[str, Any]] = field(default_factory=list)
    # A normal-start generation distinguishes the same/next turn even if IDs
    # are absent in an older log format.
    generation: int = 0
    saw_turn_start: bool = False
    resolved_config: Optional[dict[str, Any]] = None


@dataclass
class _Task:
    id: str
    session_id: str
    source: str
    config: dict[str, Any]
    turn_id: Optional[str]
    generation: int
    plan: Optional[ReferencePlan]
    status: str = "queued"
    attempts: int = 0
    valid_samples: int = 0
    started: bool = False
    started_at: Optional[float] = None
    sample_started_at: Optional[float] = None
    context: Any = None
    preview: Optional[dict[str, Any]] = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    confirmation: Optional[dict[str, Any]] = None
    initial_difference: Optional[dict[str, Any]] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    user_cancelled: bool = False
    abort_pending: bool = False
    checkpoint_generation: int = 0


class AttributionEngine:
    def __init__(self, state_path: Optional[Path], references_path: Optional[Path] = None,
                 adapter: Any = None, clock: Callable[[], float] = time.time,
                 rng: Any = None, library: Any = None) -> None:
        self.state_path = Path(state_path) if state_path else None
        self.clock, self.rng = clock, rng or random.SystemRandom()
        # Imports are local because the reference implementation uses the plan
        # type declared above. Both modules are ordinary frozen-runtime inputs.
        from codex_sampler import CodexSampler, SAFE_SAMPLER_REASONS
        from fingerprint_reference import FingerprintReferenceLibrary, SAFE_REFERENCE_REASONS
        SAFE_REASONS.update(SAFE_SAMPLER_REASONS)
        SAFE_REASONS.update(SAFE_REFERENCE_REASONS)
        self.adapter = adapter if adapter is not None else CodexSampler()
        if library is not None:
            self.library = library
        elif references_path is not None:
            try:
                manifest = json.loads(Path(references_path).read_text(encoding="utf-8")) if Path(references_path).stat().st_size <= 1024 * 1024 else {}
            except (OSError, ValueError):
                manifest = {}
            self.library = FingerprintReferenceLibrary(references_path) if isinstance(manifest, dict) and manifest.get("algorithm") else FingerprintLibrary(references_path)
        else:
            reference_override = os.environ.get("MODEL_BEACON_REFERENCE")
            self.library = FingerprintReferenceLibrary(Path(reference_override) if reference_override else None)
        self.automatic_enabled = False
        self.sessions: dict[str, _Session] = {}
        self.events: list[dict[str, Any]] = []
        self._task: Optional[_Task] = None
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._cancel_condition = threading.Condition(self._lock)
        self._closed = False
        self._last_saved: Optional[str] = None
        self.persistence_error: Optional[str] = None
        self._load()

    def _session(self, session_id: str) -> _Session:
        if not _identifier(session_id):
            raise ValueError("invalid session ID")
        if session_id not in self.sessions:
            if len(self.sessions) >= MAX_SESSIONS:
                protected = self._task.session_id if self._task else None
                oldest = next((key for key in self.sessions if key != protected), None)
                if oldest:
                    self.sessions.pop(oldest)
            self.sessions[session_id] = _Session(enabled=self.automatic_enabled,
                                                 tool_threshold=self.rng.randint(16, 32))
        return self.sessions[session_id]

    def _log(self, session_id: str, action: str, **fields: Any) -> None:
        # Only metadata is accepted here: never response text, prompts, or credentials.
        allowed = {key: value for key, value in fields.items() if key in (
            "task_id", "turn_id", "config", "source", "outcome", "reason", "attempts", "valid_samples"
        )}
        if "reason" in allowed:
            allowed["reason"] = _safe_reason(allowed["reason"])
        self.events.append({"session_id": session_id, "time": self.clock(), "event": action, **allowed})
        self.events = self.events[-MAX_EVENTS:]

    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            if self.state_path.stat().st_size > 4 * 1024 * 1024:
                return
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != 1 or not isinstance(raw.get("sessions"), dict):
                return
            saved_sessions = list(raw["sessions"].items())[-MAX_SESSIONS:]
            stored_global = raw.get("automatic_enabled")
            if isinstance(stored_global, bool):
                self.automatic_enabled = stored_global
            else:
                # Schema-1 state originally stored the switch per session. On
                # upgrade, preserve any explicit opt-in as the new app-wide
                # preference instead of silently turning monitoring off.
                self.automatic_enabled = any(
                    isinstance(saved, dict) and saved.get("enabled") is True
                    for _, saved in saved_sessions
                )
            for identifier, saved in saved_sessions:
                if not _identifier(identifier) or not isinstance(saved, dict):
                    continue
                session = self._session(identifier)
                session.enabled = self.automatic_enabled
                session.rounds, session.tools = _count(saved.get("rounds")), _count(saved.get("tools"))
                threshold = _count(saved.get("tool_threshold"))
                session.tool_threshold = threshold if 16 <= threshold <= 32 else self.rng.randint(16, 32)
                session.pending = self.automatic_enabled and saved.get("pending") is True
                session.config = _config(saved.get("config"))
                session.restored_config = True
                completed = saved.get("last_completed_at")
                session.last_completed_at = float(completed) if isinstance(completed, (float, int)) and math.isfinite(completed) and completed >= 0 else None
                session.difference_history = [row for item in saved.get("difference_history", [])[-MAX_HISTORY:]
                                              if (row := _clean_difference(item)) and row["session_id"] == identifier]
                session.history = [result for item in saved.get("history", [])[-MAX_HISTORY:]
                                   if (result := self._restore_result(identifier, item)) is not None]
            # Task logs are diagnostic metadata, with an explicit allowlist.
            # Do not restore arbitrary dictionaries, text, prompts or bodies.
            for row in raw.get("events", [])[-MAX_EVENTS:]:
                if not isinstance(row, dict) or not _identifier(row.get("session_id")):
                    continue
                if row.get("event") not in ("queued", "sample_sent", "completed", "cancelled", "rejected"):
                    continue
                event = {"session_id": row["session_id"], "event": row["event"]}
                for key in ("task_id", "turn_id"):
                    event[key] = _identifier(row.get(key))
                event["config"] = _config(row.get("config"))
                event["source"] = row.get("source") if row.get("source") in ("manual", "automatic") else None
                event["outcome"] = row.get("outcome") if row.get("outcome") in ("close", "different", "inconclusive") else None
                event["attempts"], event["valid_samples"] = min(MAX_REQUESTS, _count(row.get("attempts"))), min(MAX_REQUESTS, _count(row.get("valid_samples")))
                timestamp = row.get("time")
                event["time"] = timestamp if isinstance(timestamp, (float, int)) and math.isfinite(timestamp) and timestamp >= 0 else None
                # Loaded text is never trusted as a diagnostic instruction or
                # retained payload. Result metadata is sufficient for history.
                event["reason"] = _safe_reason(row.get("reason"))
                self.events.append(event)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            self.persistence_error = "归因历史读取失败；未恢复损坏的记录。"

    @staticmethod
    def _restore_result(session_id: str, raw: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict) or raw.get("session_id") != session_id:
            return None
        outcome = raw.get("outcome")
        if outcome not in ("close", "different", "inconclusive"):
            return None
        completed = raw.get("completed_at")
        if not isinstance(completed, (float, int)) or not math.isfinite(completed) or completed < 0:
            return None
        config = _config(raw.get("config"))
        metadata = AttributionEngine._reference_metadata(raw)
        label = {"close": "归因匹配", "different": "指纹存在差异", "inconclusive": "未得出结论"}[outcome]
        if outcome == "inconclusive" and metadata.get("candidate"):
            label = "参考倾向 " + metadata["candidate"]
        if outcome == "inconclusive" and not _count(raw.get("valid_samples")):
            label = "采样未完成" if _count(raw.get("attempts")) else "准备采样失败"
        confirmation = metadata.get("confirmation")
        if confirmation:
            label = {"confirmed_mismatch": "三次复测均有差异", "not_confirmed": "复测未持续异常",
                     "incomplete": "复测未完成", "active": "复测未完成"}[confirmation["status"]]
            if confirmation["status"] == "active":
                confirmation["status"] = "incomplete"
        if raw.get("cancelled") is True:
            label = "已取消归因"
        return {
            "id": _identifier(raw.get("id")), "session_id": session_id,
            "turn_id": _identifier(raw.get("turn_id")), "config": config,
            **config, "generation": _count(raw.get("generation")), "outcome": outcome,
            "label": label,
            "reason": _safe_reason(raw.get("reason")),
            "completed_at": completed,
            "source": raw.get("source") if raw.get("source") in ("manual", "automatic") else None,
            "valid_samples": min(MAX_REQUESTS, _count(raw.get("valid_samples"))), "attempts": min(MAX_REQUESTS, _count(raw.get("attempts"))),
            "reference_version": _name(raw.get("reference_version")), "restored": True,
            **metadata,
            "reason_short": "用户取消" if raw.get("cancelled") is True else AttributionEngine._short_reason(raw.get("reason"), stage=metadata.get("failure_stage"),
                                                             preparing=not _count(raw.get("attempts")))
                            if not _count(raw.get("valid_samples")) else metadata.get("reason_code"),
        }

    @staticmethod
    def _reference_metadata(raw: dict[str, Any]) -> dict[str, Any]:
        """Keep candidate identifiers, never arbitrary numeric output or text."""
        timings = []
        for row in raw.get("sample_timings", [])[:MAX_REQUESTS] if isinstance(raw.get("sample_timings"), list) else []:
            if not isinstance(row, dict):
                continue
            timings.append({key: round(value, 3) for key in ("attempt", "startup_seconds", "metadata_seconds", "fork_seconds", "model_seconds", "total_seconds", "first_reasoning_seconds", "first_answer_seconds", "compaction_count")
                            if isinstance((value := row.get(key)), (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value < 86400})
        return {
            "candidate": _name(raw.get("candidate")),
            **{key: value for key, value in _clean_decision(raw).items()
               if key in ("classification", "mismatch", "closed_set_weight", "expected_weight")},
            "candidates": [value for item in raw.get("candidates", [])[:3]
                           if (value := _name(item))] if isinstance(raw.get("candidates"), list) else [],
            "reference_supported": raw.get("reference_supported") is True,
            "reference_source": str(raw.get("reference_source", ""))[:160] if isinstance(raw.get("reference_source"), str) else None,
            "reference_scope": str(raw.get("reference_scope", ""))[:160] if isinstance(raw.get("reference_scope"), str) else None,
            "reference_sample_count": min(1000, _count(raw.get("reference_sample_count"))),
            "sample_candidates": [value for item in raw.get("sample_candidates", [])[:MAX_REQUESTS] if (value := _name(item))]
                                 if isinstance(raw.get("sample_candidates"), list) else [],
            "stability": raw.get("stability") if raw.get("stability") in ("consistent", "mixed", "low_margin", "close", "different", "boundary") else None,
            "reason_code": _identifier(raw.get("reason_code")),
            "sample_scope": "new_session" if raw.get("sample_scope") == "new_session" or str(raw.get("session_id", "")).startswith("new-sample-") else "conversation",
            "sample_timings": timings,
            "decisions": [_clean_decision(row) for row in raw.get("decisions", [])[:MAX_REQUESTS]] if isinstance(raw.get("decisions"), list) else [],
            "confirmation": _clean_confirmation(raw.get("confirmation")),
            "initial_difference": _clean_difference(raw.get("initial_difference")),
            "difference_observed": raw.get("difference_observed") is True,
            "failure_stage": raw.get("failure_stage") if raw.get("failure_stage") in ("configuration", "snapshot", "branch", "sampling") else None,
            "cancelled": raw.get("cancelled") is True,
            "compaction_count": min(100, _count(raw.get("compaction_count"))),
            **{key: round(value, 3) for key in ("elapsed_seconds", "candidate_margin")
               if isinstance((value := raw.get(key)), (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and 0 <= value < 86400},
        }

    def _save(self) -> None:
        if not self.state_path:
            return
        data = {"schema_version": 1, "automatic_enabled": self.automatic_enabled,
                "events": self.events[-MAX_EVENTS:], "sessions": {identifier: {
            "enabled": session.enabled, "rounds": session.rounds, "tools": session.tools,
            "tool_threshold": session.tool_threshold, "pending": session.pending,
            "config": session.config, "last_completed_at": session.last_completed_at,
            "history": session.history[-MAX_HISTORY:], "difference_history": session.difference_history[-MAX_HISTORY:],
        } for identifier, session in self.sessions.items()}}
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if serialized == self._last_saved:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(self.state_path.name + ".tmp")
            temporary.write_text(serialized, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.state_path)
            self._last_saved = serialized
            self.persistence_error = None
        except OSError:
            self.persistence_error = "归因设置或历史保存失败，请检查本地目录权限。"

    def _readiness(self, session_id: str, session: _Session, *, require_context: bool = True) -> tuple[Optional[ReferencePlan], Optional[str]]:
        if not session.observed or (require_context and not session.context_available):
            return None, "目标会话尚无可用的正常对话上下文。"
        if any(not session.config[key] for key in ("model", "provider", "effort")):
            return None, "缺少明确的会话配置记录，无法开始采样。"
        plan, reason = self.library.readiness(session.config)
        if reason or plan is None:
            return None, reason or "暂无该型号的数字指纹参考。"
        reason = self.adapter.availability(session_id, dict(session.config))
        return (None, reason) if reason else (plan, None)

    def observe(self, session_id: str, turn_id: Optional[str] = None, config: Optional[dict[str, Any]] = None,
                busy: bool = False, completed_turns: int = 0, tool_calls: int = 0,
                context_available: bool = False, normal: bool = True,
                turn_config: Optional[dict[str, Any]] = None,
                allow_automatic: bool = True) -> None:
        if not normal:
            return
        with self._lock:
            session = self._session(session_id)
            session.enabled = self.automatic_enabled
            current = _config(config)
            if session.observed or session.restored_config:
                # A task-start event can temporarily omit saved settings. Keep
                # only this session's prior known values for engine comparison;
                # it is not a new user selection or a global-default fallback.
                # Native metadata must still verify the effective target before
                # any model request is sent.
                current = {key: value if value is not None else session.config.get(key)
                           for key, value in current.items()}
            if session.resolved_config:
                if all(current[key] is None or current[key] == session.resolved_config[key]
                       for key in ("model", "provider", "effort")):
                    current = {key: current[key] or session.resolved_config.get(key)
                               for key in current}
                else:
                    session.resolved_config = None
            completed, tools = _count(completed_turns), _count(tool_calls)
            first = not session.observed
            resumed = not session.active
            changed = (not first or session.restored_config) and current != session.config
            if changed or ((first or resumed) and session.enabled and allow_automatic):
                session.checkpoint_generation += 1
            next_turn = not first and ((turn_id and turn_id != session.turn_id) or (busy and not session.busy))
            delta_completed = max(0, completed - session.completed_base) if not first and not resumed and allow_automatic else 0
            scope_completed = max(0, completed - session.completed_base) if not first else 0
            delta_tools = max(0, tools - session.tools_base) if not first and not resumed and allow_automatic else 0
            session.observed, session.active, session.restored_config = True, bool(allow_automatic), False
            session.completed_base, session.tools_base = completed, tools
            session.turn_id, session.busy, session.context_available = _identifier(turn_id), bool(busy), bool(context_available)
            session.config = current
            if next_turn:
                session.generation += 1
                session.saw_turn_start = True
            elif first and busy:
                # The initial snapshot can begin inside an already-running
                # normal turn. Its later completion is the same generation.
                session.saw_turn_start = True
            if scope_completed:
                # Older logs can omit turn IDs, and a complete start/end pair
                # can occur between two polls. A completion without an observed
                # start still invalidates the preceding sampling scope. Consume
                # an observed start once rather than advancing twice for it.
                session.generation += max(0, scope_completed - (1 if session.saw_turn_start else 0))
                session.saw_turn_start = False
            if changed:
                session.rounds = session.tools = 0
                if session.enabled:
                    session.pending = True
                # A new comparison segment invalidates remaining confirmation
                # samples. An already-sent request may settle in its old scope;
                # an ordinary new turn with the same configuration does not
                # cancel the immutable snapshot.
                if self._task and self._task.session_id == session_id:
                    self._cancel_task_locked(self._task, "所选配置已改变，已取消尚未发送的采样。")
            session.rounds += delta_completed
            session.tools += delta_tools
            if (first or resumed) and session.enabled and allow_automatic:
                # Mirrors a trusted runtime-resume checkpoint without counting
                # the historical tool calls that preceded this reader.
                session.pending = True
            if session.enabled and session.tools >= session.tool_threshold:
                session.pending = True
            if session.enabled and session.pending:
                self._queue_auto(session_id, session)
            self._save()

    def set_automatic(self, session_id: Optional[str], enabled: bool) -> dict[str, Any]:
        with self._lock:
            if enabled and session_id is None:
                return {"accepted": False, "reason": "请先按名称选择并绑定一个明确会话。"}
            session = self._session(session_id) if session_id is not None else None
            was_enabled = self.automatic_enabled
            self.automatic_enabled = bool(enabled)
            for known in self.sessions.values():
                known.enabled = self.automatic_enabled
                if not self.automatic_enabled:
                    known.pending = False
            if not self.automatic_enabled:
                if self._task and self._task.source == "automatic":
                    self._cancel_task_locked(self._task, "自动数字归因已关闭。")
            elif not was_enabled:
                assert session is not None
                session.checkpoint_generation += 1
                session.pending = True
                self._queue_auto(session_id, session)
            self._save()
            return {"accepted": True, "reason": None}

    def _queue_auto(self, session_id: str, session: _Session) -> None:
        if (not session.enabled or not session.active or not session.pending or self._task is not None):
            return
        plan, reason = self._readiness(session_id, session, require_context=False)
        prepare = False
        # Empty named threads can resolve their own exact saved configuration;
        # no fabricated normal turn is required before their initial check.
        if (reason or plan is None) and callable(getattr(self.adapter, "read_settings", None)):
            preflight = getattr(self.adapter, "preparation_availability", None)
            if callable(preflight) and preflight(session_id) is None:
                prepare, plan, reason = True, None, None
        if reason or (plan is None and not prepare):
            return
        self._task = _Task(uuid.uuid4().hex, session_id, "automatic", dict(session.config), session.turn_id, session.generation, plan,
                           checkpoint_generation=session.checkpoint_generation)
        session.pending = False
        self._log(session_id, "queued", task_id=self._task.id, turn_id=session.turn_id, config=dict(session.config), source="automatic")

    def request_manual(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._session(session_id)
            if self._task:
                return {"accepted": False, "reason": "已有归因任务正在执行或等待，请先完成或取消。"}
            plan, reason = self._readiness(session_id, session, require_context=False)
            prepare = False
            if (reason or plan is None) and callable(getattr(self.adapter, "read_settings", None)):
                preflight = getattr(self.adapter, "preparation_availability", None)
                if callable(preflight):
                    preparation_reason = preflight(session_id)
                    if preparation_reason is None:
                        prepare, plan, reason = True, None, None
                    else:
                        reason = preparation_reason
            if reason or (plan is None and not prepare):
                self._log(session_id, "rejected", source="manual", config=dict(session.config), reason=reason)
                return {"accepted": False, "reason": reason}
            self._task = _Task(uuid.uuid4().hex, session_id, "manual", dict(session.config), session.turn_id, session.generation,
                               plan, "preparing" if prepare else "queued",
                               checkpoint_generation=session.checkpoint_generation)
            self._log(session_id, "queued", task_id=self._task.id, turn_id=session.turn_id, config=dict(session.config), source="manual")
            return {"accepted": True, "reason": None}

    def request_new_sample(self, config: dict[str, Any]) -> dict[str, Any]:
        target = _config(config)
        if any(target[key] is None for key in ("model", "provider", "effort")):
            return {"accepted": False, "reason": "请选择明确的型号和提供方。"}
        identifier = "new-sample-" + uuid.uuid4().hex
        self.observe(identifier, config=target, context_available=False, allow_automatic=False)
        result = self.request_manual(identifier)
        return dict(result, session_id=identifier)

    def _complete_checkpoint(self, task: _Task, session: _Session) -> None:
        # Source turns may continue while a fixed sample is running, so turn
        # generation is deliberately separate from this scheduling generation.
        if (session.config == task.config and
                session.checkpoint_generation == task.checkpoint_generation):
            session.rounds = session.tools = 0
            session.tool_threshold = self.rng.randint(16, 32)
            session.pending = False

    def _finish_cancelled(self, task: _Task, reason: str) -> None:
        self._log(task.session_id, "cancelled", task_id=task.id, source=task.source, turn_id=task.turn_id,
                  config=task.config, attempts=task.attempts, valid_samples=task.valid_samples, reason=reason)
        if task.user_cancelled:
            completed_at = self.clock()
            result = {
                "id": task.id, "session_id": task.session_id, "turn_id": task.turn_id,
                "config": dict(task.config), **task.config, "generation": task.generation,
                "outcome": "inconclusive", "label": "已取消归因", "reason": "归因任务已取消。",
                "reason_short": "用户取消", "cancelled": True,
                "completed_at": completed_at, "source": task.source,
                "valid_samples": task.valid_samples, "attempts": task.attempts,
                "reference_version": task.plan.version if task.plan else self.library.version,
                "sample_scope": "new_session" if task.session_id.startswith("new-sample-") else "conversation",
                "restored": False,
            }
            session = self.sessions[task.session_id]
            session.history.append(result)
            session.history = session.history[-MAX_HISTORY:]
            self._complete_checkpoint(task, session)
        if self._task is task:
            self._task = None
        self._save()

    def _cancel_task_locked(self, task: _Task, reason: str) -> None:
        """Mark one matching task immediately; abort its owned clients off-lock."""
        if task.user_cancelled:
            return
        task.user_cancelled = True
        task.cancel.set()
        task.status = "cancelling"
        if not task.started:
            self._finish_cancelled(task, reason)
            return
        abort = getattr(self.adapter, "cancel_active", None)
        if callable(abort):
            task.abort_pending = True
            def abort_owned_sampling() -> None:
                try:
                    abort()
                except Exception:
                    pass
                finally:
                    # No later task may own the queue until this abort has
                    # captured/closed the old task's clients. Native sampler
                    # generations also reject a client racing preparation.
                    with self._cancel_condition:
                        task.abort_pending = False
                        self._cancel_condition.notify_all()
            threading.Thread(target=abort_owned_sampling, name="model-beacon-cancel", daemon=True).start()
        self._save()

    def cancel(self, session_id: str) -> bool:
        with self._lock:
            if not self._task or self._task.session_id != session_id:
                return False
            self._cancel_task_locked(self._task, "用户取消尚未发送的归因任务。")
            return True

    def suspend(self, session_id: str) -> None:
        """Abort only the selected binding's owned sampling work.

        This is independent of the automatic toggle, whose off action cancels
        automatic tasks only. The source conversation is never interrupted.
        """
        with self._lock:
            session = self._session(session_id)
            session.active = False
            if self._task and self._task.session_id == session_id:
                self._cancel_task_locked(self._task, "监测对象已切换，取消旧会话尚未发送的归因任务或后续样本。")

    def advance(self) -> None:
        """Start an existing queue item only; elapsed time cannot enqueue a probe."""
        with self._lock:
            task = self._task
            if self._closed or not task or task.started:
                return
            session = self.sessions[task.session_id]
            if task.cancel.is_set():
                self._finish_cancelled(task, "归因任务已取消。")
                return
            if task.source == "automatic" and not session.active:
                return
            if session.config != task.config and task.plan is not None:
                self._finish_cancelled(task, "所选配置已改变，已取消尚未发送的采样。")
                return
            # Capture the currently observed scope before the adapter freezes
            # an independent native snapshot. The normal turn keeps running.
            task.turn_id, task.generation = session.turn_id, session.generation
            if task.plan is None:
                task.config = dict(session.config)
            task.status = "preparing"
            task.started_at = task.started_at or self.clock()
            task.started = True
            self._worker = threading.Thread(target=self._run, args=(task,), name="model-beacon-attribution", daemon=True)
            self._worker.start()

    def _run(self, task: _Task) -> None:
        samples: list[Any] = []
        reason: Optional[str] = None
        outcome = "inconclusive"
        decision: dict[str, Any] = {}
        context = None
        timings: list[dict[str, Any]] = []
        failure_stage: Optional[str] = None
        preparation_stage = "configuration" if task.plan is None else "branch"
        try:
            if task.plan is None:
                settings = self.adapter.read_settings(task.session_id, dict(task.config))
                resolved = _config(dict(settings, context_segment=task.config.get("context_segment")))
                plan, readiness_reason = self.library.readiness(resolved)
                if plan is None or readiness_reason:
                    raise ValueError(readiness_reason or "reference_unavailable")
                with self._lock:
                    if task.cancel.is_set() or self._closed:
                        raise InterruptedError()
                    session = self.sessions[task.session_id]
                    if all(session.config[key] is None or session.config[key] == resolved[key]
                           for key in ("model", "provider", "effort")):
                        session.resolved_config = resolved
                        # A normal source turn can advance while metadata is
                        # resolving. Fill gaps without rolling back its newer
                        # model selection or local context segment.
                        session.config = {key: session.config[key] or resolved.get(key)
                                          for key in session.config}
                    task.config, task.plan = resolved, plan
            preparation_stage = "branch"
            context = self.adapter.create_context(task.session_id, dict(task.config))
            task.context = context
            # Every adapter must return an explicitly verified temporary context.
            # These checks prevent a provider/model fallback from being hidden.
            if not isinstance(context, dict) or context.get("temporary") is not True or context.get("source_session_id") != task.session_id or any(
                context.get(key) != task.config[key] for key in ("model", "provider", "effort")
            ):
                raise ValueError("临时采样上下文未验证目标会话或完整配置。")
            captured_turn = _identifier(context.get("source_turn_id"))
            if captured_turn:
                with self._lock:
                    task.turn_id = captured_turn
                    session = self.sessions[task.session_id]
                    if session.turn_id == captured_turn and session.config == task.config:
                        task.generation = session.generation
            for attempt in range(MAX_REQUESTS):
                with self._lock:
                    if task.cancel.is_set() or self._closed:
                        reason = "已停止尚未发送的后续样本；已发送的请求已收尾。"
                        break
                    task.status = "running"
                    task.attempts += 1
                    task.sample_started_at = self.clock()
                    self._log(task.session_id, "sample_sent", task_id=task.id, source=task.source,
                              turn_id=task.turn_id, config=task.config, attempts=task.attempts)
                # Confirmation repeats the original challenge in fresh branches
                # of the same immutable native snapshot, never prior answers.
                prompt = task.plan.prompts[0]
                failed = False
                try:
                    response = self.adapter.sample(context, prompt, dict(task.config))
                    parsed = self.library.parse(response, task.plan)
                    if parsed is None:
                        reason = "采样返回无效，未计入有效样本。"
                        failure_stage = "sampling"
                        failed = True
                    else:
                        samples.append(parsed)
                        current = dict(self.library.evaluate([parsed], task.plan))
                        current["mismatch"] = current.get("mismatch", current.get("outcome") == "different") is True
                        # The original repeated-difference label examines only
                        # the last two physical checkpoints of this segment.
                        with self._lock:
                            session = self.sessions[task.session_id]
                            previous = [row for record in session.history if record.get("config") == task.config
                                        for row in record.get("decisions", [])] + task.decisions
                            if current.get("classification") == "difference_signal" and any(
                                    row.get("candidate") == current.get("candidate") and row.get("classification") in ("difference_signal", "repeated_difference")
                                    for row in previous[-2:]):
                                current["classification"] = "repeated_difference"
                            task.decisions.append(_clean_decision(current))
                            task.valid_samples = len(samples)
                            if len(task.decisions) == 1 and current["mismatch"]:
                                task.confirmation = {"initial_mismatch": True, "completed": 0, "required": CONFIRMATION_REQUESTS, "status": "active"}
                                task.initial_difference = _clean_difference({
                                    "id": task.id + ":initial", "session_id": task.session_id, "turn_id": task.turn_id,
                                    "at": self.clock(), "config": task.config, "decision": current,
                                    "snapshot_id": context.get("snapshot_id"),
                                })
                                session.difference_history.append(task.initial_difference)
                                session.difference_history = session.difference_history[-MAX_HISTORY:]
                            elif task.confirmation:
                                task.confirmation["completed"] = len(task.decisions) - 1
                            task.preview = dict(current, display_label=(
                                f"首次指纹差异 · 复测 {task.confirmation['completed']}/{CONFIRMATION_REQUESTS}"
                                if task.confirmation else current.get("display_label") or "已取得数字参考"))
                            self._save()
                        decision = current
                except Exception as error:
                    safe = getattr(error, "safe_reason", None)
                    reason = safe if safe in SAFE_REASONS else "采样请求失败，已计入最多 4 次请求的上限。"
                    failure_stage = "sampling"
                    # Each checkpoint is a single independent sample. Failed
                    # or invalid requests end coverage; they never stand in
                    # for a completed confirmation or silently get replaced.
                    failed = True
                finally:
                    elapsed = max(0.0, self.clock() - (task.sample_started_at or self.clock()))
                    timing = {"attempt": task.attempts, "total_seconds": round(elapsed, 3)}
                    measured = context.get("last_sample_timing", {}) if isinstance(context, dict) else {}
                    for key in ("startup_seconds", "metadata_seconds", "fork_seconds", "model_seconds", "total_seconds", "first_reasoning_seconds", "first_answer_seconds", "compaction_count"):
                        value = measured.get(key)
                        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                            timing[key] = round(value, 3)
                    timings.append(timing)
                    with self._lock:
                        task.sample_started_at = None
                        self._save()
                if failed:
                    break
                if task.confirmation is None:
                    break
                if task.confirmation["completed"] >= CONFIRMATION_REQUESTS:
                    break
            if task.confirmation:
                complete = (not task.cancel.is_set() and not self._closed and
                            task.confirmation["completed"] == CONFIRMATION_REQUESTS)
                if complete:
                    all_mismatch = all(row["mismatch"] for row in task.decisions[1:])
                    task.confirmation["status"] = "confirmed_mismatch" if all_mismatch else "not_confirmed"
                    outcome = "different" if all_mismatch else "inconclusive"
                    reason = "三次独立复测均与预期不一致。" if all_mismatch else "首次差异已保留，三次复测未全部与预期不一致。"
                else:
                    task.confirmation["status"] = "incomplete"
                    outcome = "inconclusive"
                    reason = reason or "首次差异已保留，复测未完成。"
            elif task.decisions and not task.cancel.is_set() and not self._closed:
                outcome = decision.get("outcome", "inconclusive")
                default_reason = {"close": "适用参考算法给出接近所选模型的指纹参考；仅代表本次采样。",
                                  "different": "适用参考算法给出与所选模型不符的指纹参考；仅代表本次采样。",
                                  "inconclusive": "适用参考算法未得出结论。"}[outcome]
                reason = decision.get("reason") if decision.get("reason") in SAFE_REASONS else default_reason
            elif not reason:
                reason = "有效样本不足，未得出结论。"
        except Exception as error:
            safe = getattr(error, "safe_reason", None)
            reason = safe if safe in SAFE_REASONS else "无法建立并验证目标会话的临时采样上下文，未得出结论。"
            stage = getattr(error, "failure_stage", None) or getattr(error, "stage", None)
            if stage in ("configuration", "snapshot", "branch"):
                failure_stage = stage
            elif getattr(error, "code", None) in ("source_busy", "snapshot_unavailable", "snapshot_changed", "boundary_missing"):
                failure_stage = "snapshot"
            elif getattr(error, "code", None) in ("source_unavailable", "source_changed", "invalid_target"):
                failure_stage = "configuration"
            else:
                failure_stage = preparation_stage
        finally:
            if context is not None:
                try:
                    self.adapter.finish_context(context)
                except Exception:
                    pass
        with self._cancel_condition:
            while task.abort_pending:
                self._cancel_condition.wait()
            completed_at = self.clock()
            session = self.sessions[task.session_id]
            if task.cancel.is_set() and task.attempts == 0 and not task.user_cancelled:
                self._finish_cancelled(task, "已取消尚未发送的归因任务。")
                self._save()
                return
            label = {"close": "归因匹配", "different": "指纹存在差异", "inconclusive": "未得出结论"}[outcome]
            if outcome == "inconclusive" and isinstance(decision.get("display_label"), str):
                candidate = _name(decision.get("candidate"))
                label = "参考倾向 " + candidate if candidate else label
            if task.confirmation:
                label = {"confirmed_mismatch": "三次复测均有差异", "not_confirmed": "复测未持续异常",
                         "incomplete": "复测未完成", "active": "复测未完成"}[task.confirmation["status"]]
            if not samples:
                label = "采样未完成" if task.attempts else "准备采样失败"
            if task.user_cancelled:
                outcome, label, reason = "inconclusive", "已取消归因", "归因任务已取消。"
            result = {
                "id": task.id, "session_id": task.session_id, "turn_id": task.turn_id,
                "config": task.config, "model": task.config["model"], "provider": task.config["provider"],
                "effort": task.config["effort"], "context_segment": task.config["context_segment"],
                "generation": task.generation, "outcome": outcome, "label": label,
                "reason": reason, "completed_at": completed_at, "source": task.source,
                "valid_samples": len(samples), "attempts": task.attempts,
                "reference_version": task.plan.version if task.plan else self.library.version, "restored": False,
                "sample_scope": "new_session" if task.session_id.startswith("new-sample-") else "conversation",
                "compaction_count": _count(context.get("compaction_count")) if isinstance(context, dict) else 0,
                "sample_timings": timings,
                "elapsed_seconds": round(max(0.0, completed_at - (task.started_at or completed_at)), 3),
                "reason_short": self._short_reason(reason, stage=failure_stage, preparing=task.attempts == 0) if not samples else decision.get("reason_code"),
                **self._reference_metadata(dict(decision, decisions=task.decisions, confirmation=task.confirmation,
                                               cancelled=task.user_cancelled,
                                               failure_stage=failure_stage,
                                               initial_difference=task.initial_difference, difference_observed=bool(task.initial_difference),
                                               sample_candidates=[row["candidate"] for row in task.decisions if row.get("candidate")], sample_scope="new_session" if task.session_id.startswith("new-sample-") else "conversation",
                                               sample_timings=timings, compaction_count=context.get("compaction_count", 0) if isinstance(context, dict) else 0)),
            }
            if task.user_cancelled:
                result["reason_short"] = "用户取消"
            session.history.append(result)
            session.history = session.history[-MAX_HISTORY:]
            session.last_completed_at = completed_at
            self._complete_checkpoint(task, session)
            self._log(task.session_id, "completed", task_id=task.id, source=task.source, turn_id=task.turn_id,
                      config=task.config, outcome=outcome, reason=reason, attempts=task.attempts, valid_samples=len(samples))
            if self._task is task:
                self._task = None
            self._save()

    @staticmethod
    def _short_reason(reason: Any, *, stage: Optional[str] = None, preparing: bool = False) -> str:
        reason = _safe_reason(reason) or ""
        if preparing:
            if stage == "snapshot" or (stage is None and any(marker in reason for marker in ("快照", "冻结", "可固定"))):
                return "快照准备失败"
            if stage == "configuration" or (stage is None and "配置" in reason and "分支" not in reason):
                return "配置读取失败"
            return "分支准备失败"
        for marker, label in (("上下文压缩", "上下文压缩"), ("超时", "请求超时"),
                              ("未正常完成", "模型回合未完成"),
                              ("配置", "配置核对未完成"), ("工具", "分支尝试工具"),
                              ("样本", "有效样本不足"), ("登录", "需要登录 Codex")):
            if marker in reason:
                return label
        return "采样未完成"

    def snapshot(self, session_id: Optional[str]) -> dict[str, Any]:
        with self._lock:
            base = {
                "enabled": self.automatic_enabled, "available": False, "unavailable_reason": "请先按名称选择并绑定一个明确会话。",
                "task": None, "status": "unavailable", "progress": None, "result": None,
                "scope": None, "scope_label": None, "completed_at": None, "source": None,
                "valid_samples": 0, "model": None, "effort": None, "provider": None,
                "reference_version": self.library.version, "session_id": session_id, "restored": False,
                "history": [], "difference_history": [], "confirmation": None, "pending": False, "rounds": 0, "tools": 0, "tool_threshold": None,
                "persistence_error": self.persistence_error,
                "reference_status": "等待会话配置记录。",
                "sampler_status": "选择并绑定会话后检查本机 Codex 运行入口。",
                "quota_notice": "数字归因会额外消耗模型额度；一次最多 4 次模型请求，仅代表这些采样请求的指纹参考。",
            }
            if not session_id or not _identifier(session_id):
                return base
            session = self._session(session_id)
            _, reason = self._readiness(session_id, session)
            reference, reference_reason = self.library.readiness(session.config)
            sampler_reason = self.adapter.availability(session_id, dict(session.config))
            base["reference_status"] = (reference.data.get("reference_status") or f"已载入数字指纹参考 · {reference.version}" if reference else
                                        reference_reason or base["reference_status"])
            base["reference_supported"] = bool(reference and reference.data.get("expected_enrolled", True))
            base["reference_warning"] = reference.data.get("warning") if reference else None
            base["sampler_status"] = sampler_reason or "本机 Codex 采样入口可用；发送前逐次核对目标与配置。"
            task = self._task if self._task and self._task.session_id == session_id else None
            if self._task and task is None:
                reason = "另一会话已有归因任务，请等待完成或回到该会话取消。"
            result = dict(session.history[-1]) if session.history else None
            scope = None
            if result:
                if result.get("config") != session.config:
                    scope = "previous_config"
                elif result.get("restored"):
                    scope = "history"
                elif result.get("generation") != session.generation or result.get("turn_id") != session.turn_id:
                    scope = "previous_turn"
                else:
                    scope = "current"
            elapsed = max(0, int(self.clock() - task.sample_started_at)) if task and task.sample_started_at is not None else 0
            phase = (task.context or {}).get("phase") if task and isinstance(task.context, dict) else None
            progress = ("正在取消归因" if task.status == "cancelling" else
                        "正在冻结会话快照" if task.status == "preparing" else
                        f"处理临时上下文 · {elapsed}秒" if phase == "compacting" else
                        f"复测 {min(CONFIRMATION_REQUESTS, max(1, task.attempts - 1))}/{CONFIRMATION_REQUESTS} · {elapsed}秒" if task.status == "running" and task.confirmation else
                        f"初检中 · {elapsed}秒" if task.status == "running" else
                        "等待执行") if task else None
            _, manual_reason = self._readiness(session_id, session, require_context=False)
            if manual_reason and callable(getattr(self.adapter, "preparation_availability", None)):
                manual_reason = self.adapter.preparation_availability(session_id)
            base.update(confirmation=_clean_confirmation(task.confirmation) if task else _clean_confirmation((result or {}).get("confirmation")),
                        difference_history=[dict(row) for row in session.difference_history[-MAX_HISTORY:]],
                        enabled=session.enabled, available=reason is None and self._task is None,
                        manual_available=manual_reason is None and self._task is None,
                        auto_available=not session_id.startswith("new-sample-"),
                        sample_scope="new_session" if session_id.startswith("new-sample-") else "conversation",
                        elapsed_seconds=elapsed, phase=phase,
                        current_candidate=_name((task.preview or {}).get("candidate")) if task else None,
                        preview_label=(task.preview or {}).get("display_label") if task and not task.confirmation else None,
                        unavailable_reason=reason, task={
                            "id": task.id, "status": task.status, "progress": progress, "source": task.source,
                            "cancellable": task.status != "cancelling", "session_id": task.session_id, "attempts": task.attempts,
                            "max_requests": MAX_REQUESTS,
                            "sample_scope": "new_session" if session_id.startswith("new-sample-") else "conversation",
                        } if task else None, status=task.status if task else "completed" if result else "unavailable" if reason else "idle",
                        progress=progress, result=result, scope=scope, scope_label=SCOPE_LABELS.get(scope),
                        pending=session.pending, rounds=session.rounds, tools=session.tools,
                        tool_threshold=session.tool_threshold, history=[dict(row) for row in session.history[-MAX_HISTORY:]])
            if result:
                for key in ("completed_at", "source", "valid_samples", "model", "effort", "provider", "reference_version", "restored"):
                    base[key] = result.get(key)
            return base

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._task:
                self._task.cancel.set()
            self._save()
        close = getattr(self.adapter, "close", None)
        if callable(close):
            close()
