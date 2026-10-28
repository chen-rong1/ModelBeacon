"""Local controller for the passive reader and the isolated attribution queue.

The controller accepts JSON commands, never prompts a model itself, and never
uses the most active conversation as permission to sample that conversation.
"""
from __future__ import annotations

import json
from pathlib import Path
import queue
import re
import sys
import threading
from typing import Any

from attribution import AttributionEngine

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class MonitorController:
    def __init__(self, monitor: Any, engine: AttributionEngine | None = None) -> None:
        self.monitor = monitor
        self.engine = engine or AttributionEngine(None)
        self.current: dict[str, Any] = {}
        self._active_session: str | None = None
        self.command_error: str | None = None
        self._new_sample_id: str | None = None

    def command(self, command: Any) -> None:
        self.command_error = None
        if not isinstance(command, dict):
            self.command_error = "控制请求必须是 JSON 对象。"
            return
        action = command.get("action")
        try:
            if action == "bind":
                session = command.get("session_id")
                if not isinstance(session, str) or not IDENTIFIER.fullmatch(session):
                    self.command_error = "会话 ID 无效，未切换监测对象。"
                    return
                if self._active_session and self._active_session != session:
                    self.engine.suspend(self._active_session)
                self.monitor.bind(session)
                if self._new_sample_id and not self.engine.snapshot(self._new_sample_id).get("task"):
                    self._new_sample_id = None
                self.current = {}
                return
            if action == "follow_recent":
                if self._active_session:
                    self.engine.suspend(self._active_session)
                self.monitor.follow_recent()
                self.current = {}
                return
            if action == "sample_new":
                result = self.engine.request_new_sample({key: command.get(key) for key in ("model", "provider", "effort")})
                if result.get("accepted"):
                    self._new_sample_id = result["session_id"]
                else:
                    self.command_error = result.get("reason") or "新会话采样未能启动。"
                return
            if action == "set_auto" and command.get("enabled") is False:
                # The switch is global, so turning it off needs no readable
                # conversation. Do this before any poll can advance pending
                # work and send a request during the closing action itself.
                result = self.engine.set_automatic(None, False)
                if not result.get("accepted"):
                    self.command_error = result.get("reason") or "无法关闭自动数字归因。"
                return
            if action == "cancel_attribution" and self._new_sample_id:
                pending = self.engine.snapshot(self._new_sample_id).get("task") or {}
                if pending and command.get("task_id") == pending.get("id"):
                    self.engine.cancel(self._new_sample_id)
                    return
            session = self.current.get("session_id")
            # Every UI action carries the viewed session, so a queued click
            # from A cannot operate on B after an intervening bind command.
            target = command.get("session_id")
            if not session or target != session:
                self.command_error = "监测会话已变化，请对当前显示的会话重新操作。"
                return
            if action == "attribute" and self.current.get("status") == "error":
                self.command_error = "本地会话日志读取失败，暂不能核对采样配置。"
                return
            if action == "attribute":
                if self.monitor.session != session:
                    self.monitor.bind(session)
                self.poll()
                result = self.engine.request_manual(session)
                if result.get("accepted"):
                    self._new_sample_id = None
                else:
                    self.command_error = result.get("reason") or "本次归因未能排队。"
            elif action == "set_auto" and isinstance(command.get("enabled"), bool):
                if self.monitor.session != session:
                    self.monitor.bind(session)
                self.poll()
                result = self.engine.set_automatic(session, command["enabled"])
                if not result.get("accepted"):
                    self.command_error = result.get("reason") or "无法变更自动数字归因。"
            elif action == "cancel_attribution":
                pending = self.engine.snapshot(session).get("task") or {}
                if command.get("task_id") is None or command.get("task_id") == pending.get("id"):
                    self.engine.cancel(session)
            else:
                self.command_error = "无法识别控制操作。"
        except (OSError, ValueError) as error:
            self.command_error = "本地状态无法保存或读取，请检查应用数据目录权限（" + type(error).__name__ + "）。"

    def poll(self) -> dict[str, Any]:
        owned_ids = getattr(getattr(self.engine, "adapter", None), "owned_snapshot_ids", None)
        exclude = getattr(self.monitor, "exclude_sessions", None)
        if callable(owned_ids) and callable(exclude):
            exclude(owned_ids())
        state = self.monitor.poll()
        session = state.get("session_id")
        old_path, new_path = self.current.get("source_file"), state.get("source_file")
        if (session and self.current.get("session_id") == session and
                isinstance(old_path, str) and old_path and isinstance(new_path, str) and new_path != old_path):
            # Codex may resume one conversation into another rollout segment.
            # Treat the switch as a runtime resume, without counting the new
            # segment's historical tool outputs as fresh activity.
            self.engine.suspend(session)
        if self._active_session and self._active_session != session:
            self.engine.suspend(self._active_session)
        self._active_session = session
        bound = bool(session and self.monitor.session == session)
        if session and state.get("status") == "error":
            self.engine.suspend(session)
        if session:
            self.engine.observe(
                session, turn_id=state.get("turn_id"),
                # The native sampler verifies these saved settings before capture.
                config={"model": state.get("selected_model") or state.get("configuration_model"),
                        "effort": state.get("selected_effort") or state.get("configuration_effort"),
                        "provider": state.get("selected_provider") or state.get("configuration_provider") or state.get("session_provider"),
                        "context_segment": state.get("context_segment") or f"{session}:0"},
                turn_config=state.get("last_completed_turn_config") or {},
                busy=bool(state.get("busy")),
                completed_turns=state.get("normal_completed_turns", 0),
                tool_calls=state.get("normal_tool_calls", 0),
                context_available=bound and state.get("status") != "error" and bool(state.get("context_available")),
                normal=state.get("status") != "error", allow_automatic=bound,
            )
        self.engine.advance()
        current_attribution = self.engine.snapshot(session)
        if current_attribution and current_attribution.get("task"):
            # A completed independent result cannot hide a new live task or
            # its cancellation controls for the monitored conversation.
            self._new_sample_id = None
        sampling_session = self._new_sample_id or session
        attribution = self.engine.snapshot(sampling_session) if sampling_session else dict(current_attribution)
        attribution["enabled"] = current_attribution.get("enabled", False)
        if not sampling_session:
            attribution.update(available=False, manual_available=True, auto_available=False, requires_new_sample=True)
        elif self._new_sample_id and current_attribution:
            # The independent sample has its own result scope; automatic
            # monitoring always belongs to the selected conversation.
            attribution["enabled"] = current_attribution.get("enabled", False)
            attribution["auto_available"] = bool(session and state.get("status") != "error")
        elif not self._new_sample_id:
            attribution["auto_available"] = bool(session and state.get("status") != "error")
        if state.get("status") == "error" and not self._new_sample_id:
            attribution.update(available=False, manual_available=False, auto_available=False,
                               unavailable_reason="本地会话日志读取失败，暂不能核对采样配置。")
        automatic = current_attribution or {}
        attribution["auto_progress"] = {
            "tools": automatic.get("tools"), "threshold": automatic.get("tool_threshold"),
            "pending": automatic.get("pending") is True,
            "last_completed_at": automatic.get("completed_at"),
        }
        state["attribution"] = attribution
        state["command_error"] = self.command_error
        self.current = state
        return state

    def close(self) -> None:
        self.engine.close()


def run_monitor(monitor: Any, *, watch: bool, interval: float, control_stdin: bool,
                state_path: Path | None, stopping: Any) -> int:
    engine_path = state_path.with_name("attribution-state.json") if state_path else None
    controller = MonitorController(monitor, AttributionEngine(engine_path))
    commands: queue.Queue[Any] = queue.Queue(maxsize=64)

    def read_commands() -> None:
        while True:
            line = sys.stdin.buffer.readline(8193)
            if not line:
                return
            try:
                command = json.loads(line) if len(line) <= 8192 and line.endswith(b"\n") else None
            except (ValueError, UnicodeError):
                command = None
            try:
                commands.put(command, timeout=1)
            except queue.Full:
                return

    if watch and control_stdin:
        threading.Thread(target=read_commands, name="model-beacon-controls", daemon=True).start()
    previous: str | None = None
    try:
        while not stopping():
            state = json.dumps(controller.poll(), ensure_ascii=False, separators=(",", ":"))
            if state != previous:
                print(state, flush=True)
                previous = state
            if not watch:
                break
            try:
                controller.command(commands.get(timeout=interval))
                while not commands.empty():
                    controller.command(commands.get_nowait())
            except queue.Empty:
                pass
    except BrokenPipeError:
        return 0
    finally:
        controller.close()
    return 0
