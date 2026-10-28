"""Text-only attribution in explicitly configured, disposable Codex forks.

Uses the installed Codex runtime and its native account/provider transport.  No
account files, transcripts, API keys or model response bodies are logged here.
``thread/read`` is metadata-only; only an explicit ``sample`` starts inference.

The snapshot/fork checks follow ModelTrace's MIT-licensed native-fork design.
Copyright (c) 2026 xqy2006.  See packaging/licenses/ModelTrace-LICENSE.txt.
This implementation uses Python's standard library and does not require Node.
"""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Optional


EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
NEW_SAMPLE_ID = re.compile(r"^new-sample-(?:[a-f0-9]{32}|[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12})$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
MAX_PROTOCOL_LINE = 4 * 1024 * 1024
MAX_ANSWER = 64 * 1024
MAX_NOTIFICATIONS = 1024
SAFE_ITEMS = {"userMessage", "agentMessage", "reasoning", "contextCompaction"}
CLEANUP_SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
                        "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]
SNAPSHOT_PURPOSE = "model-beacon-frozen-snapshot-v1"

REASONS = {
    "runtime_missing": "未找到可用的 Codex 运行程序，请先安装 Codex 或指定运行路径。",
    "runtime_invalid": "指定的 Codex 运行路径无效，请使用可执行文件的绝对路径。",
    "invalid_target": "目标会话采样配置不完整，未发送数字题。",
    "source_changed": "目标会话配置已变化，请刷新后重新归因。",
    "source_unavailable": "无法读取指定会话的完整配置，未回退到默认模型。",
    "source_busy": "当前 Codex 无法冻结运行中的会话，请更新运行程序后重试。",
    "snapshot_unavailable": "当前 Codex 未能冻结会话快照，请更新运行程序后重试。",
    "snapshot_changed": "采样快照已变化，已停止本次归因。",
    "snapshot_cleanup": "临时快照尚未清理，已保留归属记录以便下次重试。",
    "goal_active": "目标会话有正在运行的持续目标，请先暂停该目标后再归因；不会修改原目标。",
    "goal_unknown": "无法确认目标会话的持续目标状态，未创建采样分支。",
    "boundary_missing": "目标会话尚无可固定的已保存对话轮次，未发送数字题。",
    "fork_mismatch": "临时分支的目标或配置不一致，已停止采样。",
    "sandbox_mismatch": "临时分支未采用只读和禁止审批配置，已停止采样。",
    "unexpected_turn": "临时分支尝试了未请求的续轮，已停止采样。",
    "tool_attempt": "临时分支尝试调用工具，已停止采样且未接受结果。",
    "context_changed": "临时分支发生上下文压缩，已停止采样且未接受结果。",
    "timeout": "Codex 临时采样超时，已停止本次请求。",
    "transport_closed": "Codex 临时采样连接已关闭，未接受结果。",
    "protocol_error": "Codex 临时采样协议不可用，请检查运行版本。",
    "request_failed": "Codex 临时采样请求失败，未接受结果。",
    "authentication_required": "Codex 账户连接不可用，请先在 Codex 中完成登录。",
    "model_unavailable": "当前账户或提供方无法使用所选模型，未回退到其他模型。",
    "turn_failed": "Codex 临时采样未正常完成，未接受结果。",
    "missing_answer": "Codex 临时采样未返回单一文本答案。",
    "output_limit": "Codex 临时采样输出超过限制，未接受结果。",
    "context_closed": "临时采样上下文已关闭，请重新归因。",
    "cancelled": "已取消本次数字归因。",
    "request_limit": "本次归因已达到初检加 3 次复测的上限。",
}
SAFE_SAMPLER_REASONS = frozenset(REASONS.values())


class SamplerError(RuntimeError):
    """A fixed public reason, never a raw provider/auth/protocol message."""

    def __init__(self, code: str):
        self.code = code if code in REASONS else "request_failed"
        self.safe_reason = REASONS[self.code]
        super().__init__(self.safe_reason)


def codex_executable(env: Optional[dict[str, str]] = None) -> Optional[str]:
    env = dict(os.environ if env is None else env)
    explicit = env.get("MODEL_BEACON_CODEX_PATH")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise SamplerError("runtime_invalid")
        return str(path)
    # A desktop launch may have an empty PATH.  Use the app's bundled native
    # runtime before searching PATH; no shell command or package installation.
    candidates = (
        "/Applications/Codex.app/Contents/Resources/codex",
        "/Applications/Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
    )
    for name in candidates:
        if Path(name).is_file() and os.access(name, os.X_OK):
            return name
    return shutil.which("codex", path=env.get("PATH", ""))


def _config(config: Any) -> dict[str, str]:
    if (not isinstance(config, dict) or not isinstance(config.get("model"), str)
            or not NAME.fullmatch(config["model"]) or not isinstance(config.get("provider"), str)
            or not NAME.fullmatch(config["provider"]) or config.get("effort") not in EFFORTS):
        raise SamplerError("invalid_target")
    return {key: config[key] for key in ("model", "provider", "effort")}


def _command(executable: str, config: dict[str, str]) -> list[str]:
    # These settings apply to this disposable process only.  CLI overrides
    # replace tool integrations, not the user's provider/account configuration.
    overrides = [
        "features.shell_tool=false", "features.apps=false", "features.plugins=false",
        "features.memories=false", "features.request_permissions_tool=false",
        "features.hooks=false", "agents.enabled=false", "skills.include_instructions=false",
        "skills.bundled.enabled=false", "mcp_servers={}", 'web_search="disabled"',
        'approval_policy="never"', 'sandbox_mode="read-only"',
        # Do not override model_providers.* to change transport retries: a
        # partial entry can replace a built-in provider and remove its native
        # account configuration. The limit is an initial turn plus three checks.
        # Native transport retries remain the runtime's responsibility.
    ]
    command = [executable, "app-server", "--listen", "stdio://"]
    for value in overrides:
        command.extend(["-c", value])
    return command


class AppServerClient:
    """Bounded stdio JSON-RPC transport with no implicit model-request retries."""

    def __init__(self, command: list[str], env: dict[str, str], timeout: float = 30) -> None:
        self.timeout = timeout
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=env, bufsize=0)
        self._write_lock = threading.Lock()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._pending: dict[int, queue.Queue] = {}
        self._next_id = 0
        self._events: deque[dict[str, Any]] = deque()
        self._allowed_turns: set[str] = set()
        self._turn_threads: set[str] = set()
        self._failure: Optional[SamplerError] = None
        self._closed = False
        self._close_lock = threading.Lock()
        self._shutdown_complete = False
        self._reader = threading.Thread(target=self._read, name="model-beacon-codex", daemon=True)
        self._reader.start()

    def _fail(self, code: str) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = SamplerError(code)
            for waiting in self._pending.values():
                try:
                    waiting.put_nowait(self._failure)
                except queue.Full:
                    pass
            self._condition.notify_all()
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    def _write(self, message: dict[str, Any]) -> None:
        try:
            with self._write_lock:
                if self._closed or self.process.poll() is not None:
                    raise SamplerError("transport_closed")
                assert self.process.stdin is not None
                raw = memoryview((json.dumps(message, separators=(",", ":")) + "\n").encode())
                while raw:
                    sent = self.process.stdin.write(raw)
                    if not sent:
                        raise SamplerError("transport_closed")
                    raw = raw[sent:]
                self.process.stdin.flush()
        except (OSError, ValueError):
            raise SamplerError("transport_closed") from None

    def _read(self) -> None:
        try:
            assert self.process.stdout is not None
            while not self._closed:
                raw = self.process.stdout.readline(MAX_PROTOCOL_LINE + 1)
                if not raw:
                    break
                if len(raw) > MAX_PROTOCOL_LINE or not raw.endswith(b"\n"):
                    self._fail("output_limit")
                    return
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeError):
                    self._fail("protocol_error")
                    return
                if not isinstance(message, dict):
                    self._fail("protocol_error")
                    return
                method, ident = message.get("method"), message.get("id")
                if not method:
                    with self._lock:
                        waiting = self._pending.get(ident) if isinstance(ident, int) else None
                    if waiting:
                        try:
                            waiting.put_nowait(message)
                        except queue.Full:
                            self._fail("protocol_error")
                            return
                    continue
                if ident is not None:
                    # Every server request (tool, approval, elicitation) is
                    # rejected before a sample can be accepted.
                    self._write({"id": ident, "error": {"code": -32601,
                                 "message": "Model Beacon samples do not use tools or grant permissions"}})
                    self._fail("tool_attempt")
                    return
                params = message.get("params")
                params = params if isinstance(params, dict) else {}
                if method == "turn/started":
                    thread = params.get("threadId")
                    with self._lock:
                        if thread not in self._allowed_turns:
                            self._fail("unexpected_turn")
                            return
                        self._allowed_turns.remove(thread)
                        self._turn_threads.add(thread)
                if method in ("item/started", "item/completed"):
                    item = params.get("item") or {}
                    if params.get("threadId") in self._turn_threads and item.get("type") not in SAFE_ITEMS:
                        self._fail("tool_attempt")
                        return
                    if item.get("type") == "contextCompaction":
                        # Native compaction prepares this disposable branch.
                        # Its summary can contain the entire user's context;
                        # retain only lifecycle metadata, never its text.
                        clean_item = {"type": "contextCompaction"}
                        if isinstance(item.get("id"), str) and IDENTIFIER.fullmatch(item["id"]):
                            clean_item["id"] = item["id"]
                        message = {"method": method, "params": {
                            key: params[key] for key in ("threadId", "turnId") if key in params
                        }}
                        message["params"]["item"] = clean_item
                # Keep only lifecycle and complete-message events.  Deltas and
                # unrelated telemetry can be large and are never needed here.
                if method not in ("turn/started", "turn/completed", "item/started", "item/completed", "error"):
                    continue
                with self._condition:
                    if len(self._events) >= MAX_NOTIFICATIONS:
                        self._fail("output_limit")
                        return
                    # Stamp arrival here: notifications may precede the
                    # turn/start response and be consumed substantially later.
                    message["_received_monotonic"] = time.monotonic()
                    self._events.append(message)
                    self._condition.notify_all()
        except (OSError, ValueError, TypeError, SamplerError):
            if not self._closed:
                self._fail("protocol_error")
        finally:
            if not self._closed:
                self._fail("transport_closed")

    def request(self, method: str, params: Optional[dict[str, Any]] = None,
                timeout: Optional[float] = None) -> dict[str, Any]:
        params = params or {}
        with self._lock:
            if self._failure:
                raise self._failure
            if self._closed:
                raise SamplerError("transport_closed")
            self._next_id += 1
            ident = self._next_id
            waiting: queue.Queue = queue.Queue(maxsize=1)
            self._pending[ident] = waiting
            if method == "turn/start":
                self._allowed_turns.add(params["threadId"])
        try:
            self._write({"id": ident, "method": method, "params": params})
            try:
                response = waiting.get(timeout=self.timeout if timeout is None else timeout)
            except queue.Empty:
                self._fail("timeout")
                raise SamplerError("timeout") from None
            if isinstance(response, Exception):
                raise response
            if "error" in response:
                # Runtime errors can contain auth/provider details.  Never
                # return raw error.message through the monitor UI or journal.
                error = response.get("error")
                message = str(error.get("message", ""))[:4096].casefold() if isinstance(error, dict) else ""
                if method == "thread/fork" and "in-progress turn" in message:
                    raise SamplerError("source_busy")
                if any(word in message for word in ("unauthorized", "not authenticated", "authentication required", "not logged in")):
                    raise SamplerError("authentication_required")
                if "model" in message and any(word in message for word in ("not available", "not supported", "not found", "access", "permission")):
                    raise SamplerError("model_unavailable")
                raise SamplerError("request_failed")
            result = response.get("result")
            if not isinstance(result, dict):
                raise SamplerError("protocol_error")
            return result
        finally:
            with self._lock:
                self._pending.pop(ident, None)

    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        self._write({"method": method, "params": params or {}})

    def next_event(self, deadline: float) -> dict[str, Any]:
        with self._condition:
            while not self._events:
                if self._closed:
                    raise SamplerError("context_closed")
                if self._failure:
                    raise self._failure
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SamplerError("timeout")
                self._condition.wait(min(remaining, .25))
            if self._closed:
                raise SamplerError("context_closed")
            if self._failure:
                raise self._failure
            return self._events.popleft()

    def cancel_pending(self) -> None:
        """Synchronously wake protocol waiters before process shutdown waits."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            # Wake a caller waiting for initialize/metadata too.  A close
            # during preflight must not leave it blocked until the timeout.
            for waiting in self._pending.values():
                try:
                    waiting.put_nowait(SamplerError("context_closed"))
                except queue.Full:
                    pass
            self._condition.notify_all()

    def close(self) -> None:
        self.cancel_pending()
        with self._close_lock:
            if self._shutdown_complete:
                return
            try:
                # Terminate the owned child before closing its pipes. A pipe
                # writer may itself be blocked if a stalled child stopped
                # reading; closing stdin first can otherwise wait forever.
                if self.process.poll() is None:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
            except (OSError, ProcessLookupError):
                pass
            finally:
                if self.process.stdin:
                    self.process.stdin.close()
                if self.process.stdout:
                    self.process.stdout.close()
                self._shutdown_complete = True


class CodexSampler:
    """An initial check plus three confirmations, each in an ephemeral fork."""

    def __init__(self, *, env: Optional[dict[str, str]] = None, timeout: float = 180,
                 client_factory: Callable[..., Any] = AppServerClient,
                 ownership_dir: Optional[Path] = None) -> None:
        self.env = dict(os.environ if env is None else env)
        self.timeout = max(1, min(600, timeout))
        self.client_factory = client_factory
        self._contexts: dict[int, dict[str, Any]] = {}
        self._clients: dict[int, Any] = {}
        self._contexts_lock = threading.RLock()
        self._closed = False
        self._cancel_generation = 0
        self.phase = "idle"
        self.ownership_dir = ownership_dir or Path.home() / "Library/Application Support/Model Beacon/sampler-snapshots"
        self.runtime_scope = hashlib.sha256(json.dumps({
            "codex_home": self.env.get("CODEX_HOME", str(Path.home() / ".codex")),
            "executable": self.env.get("MODEL_BEACON_CODEX_PATH", "auto"),
        }, sort_keys=True).encode()).hexdigest()

    def _phase(self, context: Optional[dict[str, Any]], phase: str) -> None:
        # The parent can show this while create_context has not returned yet.
        # No message text or provider error is ever exposed through progress.
        self.phase = phase
        if context is not None:
            context["phase"] = phase

    @staticmethod
    def _seconds(start: float, end: Optional[float] = None) -> float:
        return round(max(0.0, (time.monotonic() if end is None else end) - start), 6)

    def availability(self, session_id: str, config: dict[str, Any]) -> Optional[str]:
        reason = self.preparation_availability(session_id)
        if reason:
            return reason
        try:
            _config(config)
            return None
        except SamplerError as error:
            return str(error)

    def preparation_availability(self, session_id: str) -> Optional[str]:
        """A cheap local preflight, including before target settings are read."""
        try:
            if self._closed:
                raise SamplerError("context_closed")
            if (not isinstance(session_id, str) or not IDENTIFIER.fullmatch(session_id)
                    or (session_id.startswith("new-sample-") and not NEW_SAMPLE_ID.fullmatch(session_id))):
                raise SamplerError("invalid_target")
            if not codex_executable(self.env):
                raise SamplerError("runtime_missing")
            return None
        except SamplerError as error:
            return str(error)

    def _generation(self) -> int:
        with self._contexts_lock:
            if self._closed:
                raise SamplerError("context_closed")
            return self._cancel_generation

    def _check_cancelled(self, generation: int, context: Optional[dict[str, Any]] = None) -> None:
        with self._contexts_lock:
            if self._closed:
                raise SamplerError("context_closed")
            if generation != self._cancel_generation or (context is not None and context.get("_cancelled")):
                raise SamplerError("cancelled")

    def _open(self, settings: dict[str, str], *, generation: Optional[int] = None) -> Any:
        generation = self._generation() if generation is None else generation
        self._check_cancelled(generation)
        if self._closed:
            raise SamplerError("context_closed")
        executable = codex_executable(self.env)
        if not executable:
            raise SamplerError("runtime_missing")
        env = dict(self.env, MODEL_BEACON_PROBE_PROCESS="1")
        # The target is an explicit protocol parameter, never the invoking
        # agent's inherited thread environment.
        env.pop("CODEX_THREAD_ID", None)
        # Register ownership before initialization or source/fork checks.  The
        # application may quit while any of these operations is still waiting.
        with self._contexts_lock:
            self._check_cancelled(generation)
        client = self.client_factory(_command(executable, settings), env, timeout=min(30, self.timeout))
        try:
            with self._contexts_lock:
                self._check_cancelled(generation)
                self._clients[id(client)] = client
            client.request("initialize", {"clientInfo": {"name": "model_beacon", "version": "1.2.3"},
                                          "capabilities": {"experimentalApi": True}})
            client.notify("initialized")
            self._check_cancelled(generation)
            return client
        except Exception:
            self._close_client(client)
            self._check_cancelled(generation)
            raise

    def _close_client(self, client: Any) -> None:
        try:
            client.close()
        finally:
            with self._contexts_lock:
                self._clients.pop(id(client), None)

    @staticmethod
    def _turns(client: Any, session_id: str) -> list[dict[str, Any]]:
        response = client.request("thread/turns/list", {"threadId": session_id, "limit": 1,
                                  "sortDirection": "desc", "itemsView": "notLoaded"})
        rows = response.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SamplerError("protocol_error")
        return rows

    @staticmethod
    def _metadata(client: Any, session_id: str) -> dict[str, Any]:
        response = client.request("thread/read", {"threadId": session_id, "includeTurns": False})
        thread = response.get("thread")
        if (not isinstance(thread, dict) or thread.get("id") != session_id or thread.get("ephemeral") is not False
                or not isinstance(thread.get("cwd"), str) or not Path(thread["cwd"]).is_absolute()
                or any(thread.get(field) is None for field in ("model", "modelProvider", "reasoningEffort"))):
            raise SamplerError("source_unavailable")
        try:
            _config({"model": thread["model"], "provider": thread["modelProvider"], "effort": thread["reasoningEffort"]})
        except SamplerError:
            raise SamplerError("source_unavailable") from None
        path = thread.get("path")
        if path is None:
            # A newly-created named thread may have no rollout until its first
            # normal turn. Only an actually empty history permits no path.
            if CodexSampler._turns(client, session_id):
                raise SamplerError("source_unavailable")
        elif not isinstance(path, str) or not Path(path).is_absolute():
            raise SamplerError("source_unavailable")
        return thread

    @staticmethod
    def _source(client: Any, session_id: str, config: dict[str, str]) -> dict[str, Any]:
        thread = CodexSampler._metadata(client, session_id)
        if (thread["model"], thread["modelProvider"], thread["reasoningEffort"]) != (
                config["model"], config["provider"], config["effort"]):
            raise SamplerError("source_changed")
        # An ephemeral fork does not support deferGoalContinuation on every
        # runtime.  Do not create it at all when an inherited /goal might start
        # inference by itself.  Never pause/clear/change the source goal.
        try:
            goal_response = client.request("thread/goal/get", {"threadId": session_id})
        except SamplerError:
            raise SamplerError("goal_unknown") from None
        if "goal" not in goal_response:
            raise SamplerError("goal_unknown")
        goal = goal_response["goal"]
        if goal is not None:
            if (not isinstance(goal, dict) or goal.get("threadId") != session_id
                    or goal.get("status") not in ("active", "paused", "blocked", "usageLimited", "budgetLimited", "complete")):
                raise SamplerError("goal_unknown")
            if goal["status"] == "active":
                raise SamplerError("goal_active")
        return thread

    def read_settings(self, session_id: str, hints: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Resolve this exact thread's saved settings without creating a turn."""
        if not isinstance(session_id, str) or not IDENTIFIER.fullmatch(session_id) or session_id.startswith("new-sample-"):
            raise SamplerError("invalid_target")
        generation = self._generation()
        client = self._open({}, generation=generation)
        try:
            thread = self._metadata(client, session_id)
            self._check_cancelled(generation)
            settings = {"model": thread["model"], "provider": thread["modelProvider"],
                        "effort": thread["reasoningEffort"], "cwd": thread["cwd"]}
            if isinstance(hints, dict) and any(hints.get(key) is not None and hints[key] != settings[key]
                                               for key in ("model", "provider", "effort")):
                raise SamplerError("source_changed")
            rows = self._turns(client, session_id)
            self._check_cancelled(generation)
            return dict(settings, source_has_turns=bool(rows),
                        busy=(isinstance(thread.get("status"), dict) and thread["status"].get("type") == "active")
                             or bool(rows and rows[0].get("status") == "inProgress"))
        finally:
            self._close_client(client)

    @staticmethod
    def _boundary(client: Any, snapshot_id: str) -> dict[str, Any]:
        """Hash only the immutable ordered turn IDs/statuses of our own base."""
        digest = hashlib.sha256(b"model-beacon-snapshot-boundaries-v1\n")
        cursor = None
        seen_cursors: set[str] = set()
        seen_turns: set[str] = set()
        last = None
        for _ in range(20_000):
            params = {"threadId": snapshot_id, "limit": 50, "itemsView": "notLoaded", "sortDirection": "asc"}
            if cursor:
                params["cursor"] = cursor
            response = client.request("thread/turns/list", params)
            rows = response.get("data")
            if not isinstance(rows, list):
                raise SamplerError("snapshot_changed")
            for turn in rows:
                if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str)
                        or not IDENTIFIER.fullmatch(turn["id"]) or turn["id"] in seen_turns
                        or turn.get("status") not in ("completed", "interrupted", "failed")):
                    raise SamplerError("snapshot_changed")
                seen_turns.add(turn["id"])
                digest.update((json.dumps([turn["id"], turn["status"]], separators=(",", ":")) + "\n").encode())
                last = turn
            cursor = response.get("nextCursor")
            if not cursor:
                return {"sha256": digest.hexdigest(), "turn_count": len(seen_turns),
                        "turn_id": last["id"] if last else None, "turn_status": last["status"] if last else None}
            if not isinstance(cursor, str) or cursor in seen_cursors or not rows:
                raise SamplerError("snapshot_changed")
            seen_cursors.add(cursor)
        raise SamplerError("snapshot_changed")

    def _ledger_path(self, snapshot_id: str) -> Path:
        if not isinstance(snapshot_id, str) or not IDENTIFIER.fullmatch(snapshot_id):
            raise SamplerError("snapshot_changed")
        return self.ownership_dir / (snapshot_id + ".json")

    def owned_snapshot_ids(self) -> set[str]:
        """Hide this application's temporary bases from conversation choices."""
        identifiers: set[str] = set()
        if not self.ownership_dir.is_dir():
            return identifiers
        for path in list(self.ownership_dir.glob("*.json"))[:128]:
            try:
                if path.is_symlink() or path.stat().st_size > 16_384:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                snapshot = record.get("snapshot", {})
                identifier = snapshot.get("id")
                if (record.get("purpose") == SNAPSHOT_PURPOSE and record.get("runtime_scope") == self.runtime_scope
                        and isinstance(identifier, str) and IDENTIFIER.fullmatch(identifier)
                        and identifier != snapshot.get("source_session_id") and self._ledger_path(identifier) == path):
                    identifiers.add(identifier)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return identifiers

    def _save_ownership(self, snapshot: dict[str, Any], status: str) -> None:
        self.ownership_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._ledger_path(snapshot["id"])
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        record = {"purpose": SNAPSHOT_PURPOSE, "runtime_scope": self.runtime_scope,
                  "owner_pid": os.getpid(), "status": status, "snapshot": snapshot}
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_snapshot(client: Any, snapshot: dict[str, Any]) -> None:
        ident, source = snapshot.get("id"), snapshot.get("source_session_id")
        if (not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident) or not isinstance(source, str)
                or not IDENTIFIER.fullmatch(source) or ident == source or not snapshot.get("boundary")):
            raise SamplerError("snapshot_changed")
        thread = CodexSampler._metadata(client, ident)
        if (thread.get("forkedFromId") != source or thread.get("path") != snapshot.get("path")
                or thread.get("cwd") != snapshot.get("cwd")
                or any(thread.get(field) != snapshot.get(key) for field, key in (
                    ("model", "model"), ("modelProvider", "provider"), ("reasoningEffort", "effort")))
                or (thread.get("status") or {}).get("type") == "active"
                or CodexSampler._boundary(client, ident) != snapshot["boundary"]):
            raise SamplerError("snapshot_changed")

    def _cleanup_snapshot(self, client: Any, snapshot: dict[str, Any]) -> bool:
        """Delete only an unchanged owned base with no persisted descendants."""
        try:
            self._save_ownership(snapshot, "pending")
            self._verify_snapshot(client, snapshot)
            for archived in (False, True):
                descendants = client.request("thread/list", {
                    "ancestorThreadId": snapshot["id"], "limit": 1, "archived": archived,
                    "sourceKinds": CLEANUP_SOURCE_KINDS, "modelProviders": [], "useStateDbOnly": True,
                })
                if not isinstance(descendants.get("data"), list) or descendants["data"] or descendants.get("nextCursor"):
                    raise SamplerError("snapshot_changed")
            self._verify_snapshot(client, snapshot)
            client.request("thread/delete", {"threadId": snapshot["id"]}, timeout=30)
            self._ledger_path(snapshot["id"]).unlink(missing_ok=True)
            return True
        except (SamplerError, OSError, ValueError, TypeError, AttributeError):
            return False

    def _recover_snapshots(self, client: Any) -> None:
        """Retry abandoned owned-base cleanup only during explicit preparation."""
        if not self.ownership_dir.is_dir():
            return
        for path in list(self.ownership_dir.glob("*.json"))[:32]:
            try:
                if path.is_symlink() or path.stat().st_size > 16_384:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                snapshot = record.get("snapshot")
                if (record.get("purpose") != SNAPSHOT_PURPOSE or record.get("runtime_scope") != self.runtime_scope
                        or not isinstance(snapshot, dict) or self._ledger_path(snapshot.get("id")) != path):
                    continue
                pid = record.get("owner_pid")
                if record.get("status") == "active" and type(pid) is int:
                    try:
                        os.kill(pid, 0)
                        continue
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        continue
                self._cleanup_snapshot(client, snapshot)
            except (OSError, ValueError, TypeError, AttributeError, SamplerError):
                continue

    def _freeze_snapshot(self, client: Any, session_id: str, settings: dict[str, str], cwd: str) -> dict[str, Any]:
        result = client.request("thread/fork", {
            "threadId": session_id, "excludeTurns": True, "deferGoalContinuation": True,
            "model": settings["model"], "modelProvider": settings["provider"], "cwd": cwd,
            "approvalPolicy": "never", "sandbox": "read-only",
            "config": {"model_reasoning_effort": settings["effort"]},
        })
        thread = result.get("thread")
        if (not isinstance(thread, dict) or thread.get("ephemeral") is not False
                or not isinstance(thread.get("id"), str) or not IDENTIFIER.fullmatch(thread["id"])
                or thread["id"] == session_id or thread.get("forkedFromId") != session_id
                or not isinstance(thread.get("path"), str) or not Path(thread["path"]).is_absolute()
                or thread.get("cwd") != cwd):
            raise SamplerError("snapshot_unavailable")
        snapshot = dict(settings, id=thread["id"], source_session_id=session_id, cwd=cwd,
                        path=thread["path"], captured_at=time.time())
        self._save_ownership(snapshot, "active")
        try:
            snapshot["boundary"] = self._boundary(client, snapshot["id"])
            self._save_ownership(snapshot, "active")
            self._verify_snapshot(client, snapshot)
            if (result.get("model"), result.get("modelProvider"), result.get("reasoningEffort"), result.get("cwd")) != (
                    settings["model"], settings["provider"], settings["effort"], cwd):
                raise SamplerError("fork_mismatch")
            if result.get("approvalPolicy") != "never" or (result.get("sandbox") or {}).get("type") != "readOnly":
                raise SamplerError("sandbox_mismatch")
            return snapshot
        except Exception:
            self._cleanup_snapshot(client, snapshot)
            raise

    @staticmethod
    def _fork(context: dict[str, Any]) -> dict[str, Any]:
        client, config = context["_client"], context["_config"]
        parent = context["snapshot_id"]
        # The owned base is already a complete, immutable frozen history.
        # Some native runtimes expose an interrupted persisted boundary in
        # turns/list but reject the same ID as in-progress in lastTurnId.
        # Fork the whole verified base instead; never select a different
        # source boundary or resume the user's running conversation.
        CodexSampler._verify_snapshot(client, context["_snapshot"])
        params = {
            "threadId": parent,
            "ephemeral": True, "excludeTurns": True,
            "model": config["model"], "modelProvider": config["provider"], "cwd": context["cwd"],
            "approvalPolicy": "never", "sandbox": "read-only",
            "config": {"model_reasoning_effort": config["effort"]},
        }
        result = client.request("thread/fork", params)
        thread = result.get("thread")
        if (not isinstance(thread, dict) or thread.get("ephemeral") is not True
                or not isinstance(thread.get("id"), str) or not IDENTIFIER.fullmatch(thread["id"])
                or thread["id"] in (context["source_session_id"], parent) or thread.get("path")
                or thread.get("forkedFromId") != parent
                or thread.get("cwd") != context["cwd"] or result.get("cwd") != context["cwd"]
                or (result.get("model"), result.get("modelProvider"), result.get("reasoningEffort")) != (
                    config["model"], config["provider"], config["effort"])):
            raise SamplerError("fork_mismatch")
        if result.get("approvalPolicy") != "never" or (result.get("sandbox") or {}).get("type") != "readOnly":
            raise SamplerError("sandbox_mismatch")
        CodexSampler._verify_snapshot(client, context["_snapshot"])
        return thread

    @staticmethod
    def _fresh_thread(context: dict[str, Any]) -> dict[str, Any]:
        """Create a standalone sample explicitly requested through the UI."""
        settings = context["_config"]
        result = context["_client"].request("thread/start", {
            "ephemeral": True,
            "model": settings["model"], "modelProvider": settings["provider"], "cwd": context["cwd"],
            "approvalPolicy": "never", "sandbox": "read-only",
            "config": {"model_reasoning_effort": settings["effort"]},
            "developerInstructions": "This is a standalone numeric sample. Answer only the supplied request. Do not use tools or continue another task.",
        })
        thread = result.get("thread")
        if (not isinstance(thread, dict) or thread.get("ephemeral") is not True
                or not isinstance(thread.get("id"), str) or not IDENTIFIER.fullmatch(thread["id"])
                or thread["id"] == context["source_session_id"] or thread.get("path")
                or thread.get("forkedFromId") is not None
                or thread.get("cwd") != context["cwd"] or result.get("cwd") != context["cwd"]
                or (result.get("model"), result.get("modelProvider"), result.get("reasoningEffort")) != (
                    settings["model"], settings["provider"], settings["effort"])):
            raise SamplerError("fork_mismatch")
        if result.get("approvalPolicy") != "never" or (result.get("sandbox") or {}).get("type") != "readOnly":
            raise SamplerError("sandbox_mismatch")
        return thread

    def _create_standalone_context(self, session_id: str, settings: dict[str, str], started: float,
                                   generation: int) -> dict[str, Any]:
        workspace = tempfile.TemporaryDirectory(prefix="model-beacon-sample-")
        client = None
        failure_stage = "configuration"
        try:
            stage = time.monotonic()
            client = self._open(settings, generation=generation)
            startup = self._seconds(stage)
            context = dict(settings, temporary=True, source_session_id=session_id,
                           source_turn_id=None, source_turn_status=None, source_has_turns=False,
                           sample_scope="new_session", cwd=str(Path(workspace.name).resolve()),
                           compaction_count=0, compacted=False, _workspace=workspace,
                           _client=client, _config=settings, _attempts=0, _closed=False,
                           _cancel_generation=generation, _cancelled=False)
            self._phase(context, "preparing")
            stage = time.monotonic()
            failure_stage = "branch"
            context["_prepared"] = self._fresh_thread(context)
            self._check_cancelled(generation, context)
            context["timings"] = {
                "startup_seconds": startup, "metadata_seconds": 0.0,
                "fork_seconds": self._seconds(stage), "total_seconds": self._seconds(started),
            }
            context["_preparation_timing_pending"] = True
            with self._contexts_lock:
                self._check_cancelled(generation, context)
                self._contexts[id(context)] = context
            return context
        except Exception as error:
            if isinstance(error, SamplerError):
                error.stage = failure_stage
            if client is not None:
                self._close_client(client)
            workspace.cleanup()
            raise

    def create_context(self, session_id: str, config: dict[str, Any]) -> dict[str, Any]:
        generation = self._generation()
        started = time.monotonic()
        self._phase(None, "preparing")
        reason = self.availability(session_id, config)
        if reason:
            raise SamplerError(next((code for code, text in REASONS.items() if text == reason), "invalid_target"))
        settings = _config(config)
        if NEW_SAMPLE_ID.fullmatch(session_id):
            return self._create_standalone_context(session_id, settings, started, generation)
        stage = time.monotonic()
        client = self._open(settings, generation=generation)
        startup = self._seconds(stage)
        snapshot = None
        failure_stage = "configuration"
        try:
            stage = time.monotonic()
            self._recover_snapshots(client)
            self._check_cancelled(generation)
            thread = self._source(client, session_id, settings)
            self._check_cancelled(generation)
            metadata = self._seconds(stage)
            stage = time.monotonic()
            failure_stage = "snapshot"
            snapshot = self._freeze_snapshot(client, session_id, settings, thread["cwd"])
            self._check_cancelled(generation)
            boundary = snapshot["boundary"]
            context = dict(settings, temporary=True, source_session_id=session_id,
                           source_turn_id=boundary["turn_id"], source_turn_status=boundary["turn_status"],
                           source_has_turns=boundary["turn_count"] > 0, cwd=thread["cwd"],
                           snapshot_id=snapshot["id"], captured_at=snapshot["captured_at"],
                           source_snapshot_scope="frozen_snapshot", compaction_count=0, compacted=False,
                           sample_scope="conversation",
                           _client=client, _config=settings, _attempts=0, _closed=False, _snapshot=snapshot,
                           _cancel_generation=generation, _cancelled=False)
            self._phase(context, "preparing")
            # The preflight is a real native fork and checks effective settings
            # before the engine can call this a verified temporary context.
            failure_stage = "branch"
            context["_prepared"] = self._fork(context)
            self._check_cancelled(generation, context)
            context["timings"] = {
                "startup_seconds": startup, "metadata_seconds": metadata,
                "fork_seconds": self._seconds(stage), "total_seconds": self._seconds(started),
            }
            context["_preparation_timing_pending"] = True
            with self._contexts_lock:
                self._check_cancelled(generation, context)
                self._contexts[id(context)] = context
            return context
        except Exception as error:
            if isinstance(error, SamplerError):
                error.stage = failure_stage
            if snapshot is not None:
                self._cleanup_snapshot(client, snapshot)
            self._close_client(client)
            raise

    def sample(self, context: dict[str, Any], prompt: str, config: dict[str, Any]) -> str:
        generation = context.get("_cancel_generation", self._generation())
        self._check_cancelled(generation, context)
        if context.get("_closed"):
            raise SamplerError("context_closed")
        if _config(config) != context.get("_config"):
            raise SamplerError("source_changed")
        if context.get("_attempts", 0) >= 4:
            raise SamplerError("request_limit")
        if not isinstance(prompt, str) or not prompt or len(prompt) > 16_384:
            raise SamplerError("invalid_target")
        started = time.monotonic()
        preparation = context.get("timings", {}) if context.pop("_preparation_timing_pending", False) else {}
        timing: dict[str, Any] = {
            "startup_seconds": preparation.get("startup_seconds", 0.0),
            "metadata_seconds": preparation.get("metadata_seconds", 0.0),
            "fork_seconds": preparation.get("fork_seconds", 0.0),
            "model_seconds": 0.0, "total_seconds": 0.0, "compaction_count": 0,
            "model_completed": False, "first_reasoning_seconds": None, "first_answer_seconds": None,
        }
        compactions_before = context.get("compaction_count", 0)
        model_started: Optional[float] = None
        model_ended: Optional[float] = None
        self._phase(context, "preparing")
        client = context.get("_client")
        fork_id = None
        turn_id = None
        completed_ok = False
        try:
            if client is None:
                stage = time.monotonic()
                try:
                    client = self._open(context["_config"], generation=generation)
                    context["_client"] = client
                finally:
                    timing["startup_seconds"] += self._seconds(stage)
            # The original conversation may continue while we sample. Only
            # our frozen base is checked; new source turns/settings cannot
            # change the explicitly captured context of this task.
            if context.get("sample_scope") != "new_session":
                stage = time.monotonic()
                try:
                    self._verify_snapshot(client, context["_snapshot"])
                    self._check_cancelled(generation, context)
                finally:
                    timing["metadata_seconds"] += self._seconds(stage)
            fork = context.pop("_prepared", None)
            if fork is None:
                stage = time.monotonic()
                try:
                    fork = self._fresh_thread(context) if context.get("sample_scope") == "new_session" else self._fork(context)
                finally:
                    timing["fork_seconds"] += self._seconds(stage)
            fork_id = fork["id"]
            self._check_cancelled(generation, context)
            context["_attempts"] += 1
            deadline = time.monotonic() + self.timeout
            self._phase(context, "model")
            model_started = time.monotonic()
            result = client.request("turn/start", {
                "threadId": fork_id, "input": [{"type": "text", "text": prompt}],
                "model": config["model"], "effort": config["effort"], "cwd": context["cwd"],
                "approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            }, timeout=min(30, self.timeout))
            turn_id = (result.get("turn") or {}).get("id")
            self._check_cancelled(generation, context)
            if not isinstance(turn_id, str):
                raise SamplerError("protocol_error")
            final: list[str] = []
            unphased: list[str] = []
            compactions: set[str] = set()
            anonymous_compaction = False
            while True:
                self._check_cancelled(generation, context)
                message = client.next_event(deadline)
                self._check_cancelled(generation, context)
                params = message.get("params") or {}
                if params.get("threadId") != fork_id:
                    continue
                received = message.get("_received_monotonic")
                observed_at = (received if type(received) in (float, int)
                               and model_started <= received <= time.monotonic() else time.monotonic())
                method = message.get("method")
                if params.get("turnId") and params["turnId"] != turn_id:
                    raise SamplerError("unexpected_turn")
                if method == "error" and not params.get("willRetry"):
                    raise SamplerError("turn_failed")
                if method in ("item/started", "item/completed") and (params.get("item") or {}).get("type") == "contextCompaction":
                    self._phase(context, "compacting" if method == "item/started" else "model")
                    ident = params["item"].get("id")
                    if ident is not None and ident not in compactions:
                        compactions.add(ident)
                        context["compaction_count"] += 1
                    elif ident is None and (method == "item/started" or not anonymous_compaction):
                        context["compaction_count"] += 1
                        anonymous_compaction = method == "item/started"
                    if ident is None and method == "item/completed":
                        anonymous_compaction = False
                    context["compacted"] = True
                    continue
                if method in ("item/started", "item/completed"):
                    item_type = (params.get("item") or {}).get("type")
                    if item_type in ("reasoning", "agentMessage"):
                        self._phase(context, "model")
                        key = "first_reasoning_seconds" if item_type == "reasoning" else "first_answer_seconds"
                        if timing[key] is None:
                            timing[key] = self._seconds(model_started, observed_at)
                if method == "item/completed" and (params.get("item") or {}).get("type") == "agentMessage":
                    item = params["item"]
                    value = item.get("text")
                    if not isinstance(value, str):
                        raise SamplerError("missing_answer")
                    if len(value) > MAX_ANSWER:
                        raise SamplerError("output_limit")
                    if item.get("phase") in ("final_answer", "final"):
                        final.append(value)
                    elif not item.get("phase"):
                        unphased.append(value)
                if method == "turn/completed":
                    completed = params.get("turn") or {}
                    if completed.get("id") == turn_id:
                        model_ended = observed_at
                    if completed.get("id") != turn_id or completed.get("status") != "completed":
                        raise SamplerError("turn_failed")
                    answers = final or unphased
                    if len(answers) != 1:
                        raise SamplerError("missing_answer")
                    completed_ok = True
                    timing["model_completed"] = True
                    return answers[0]
        except SamplerError:
            self._check_cancelled(generation, context)
            raise
        finally:
            # This is the requested native turn's elapsed time, including
            # queuing and native compaction; it is not pure inference time.
            failed_at = time.monotonic()
            if model_started is not None:
                timing["model_seconds"] = self._seconds(model_started, model_ended or failed_at)
            timing["compaction_count"] = context.get("compaction_count", 0) - compactions_before
            if turn_id and fork_id and not completed_ok:
                try:
                    client.request("turn/interrupt", {"threadId": fork_id, "turnId": turn_id}, timeout=1)
                except (SamplerError, OSError):
                    pass
            # Do not leave an inherited goal or another continuation alive
            # between samples.  Every later attempt gets a new native process
            # and a fresh ephemeral fork of the same original boundary.
            if client is not None:
                self._close_client(client)
            context["_client"] = None
            timing["total_seconds"] = round(self._seconds(started) + preparation.get("total_seconds", 0.0), 6)
            for key in ("startup_seconds", "metadata_seconds", "fork_seconds"):
                timing[key] = round(timing[key], 6)
            context["last_sample_timing"] = timing

    def finish_context(self, context: dict[str, Any]) -> None:
        if not isinstance(context, dict):
            return
        context["_closed"] = True
        client = context.get("_client")
        snapshot = context.pop("_snapshot", None)
        try:
            if snapshot is not None:
                self._save_ownership(snapshot, "pending")
                if client is None and not self._closed:
                    client = self._open(context["_config"])
                if client is not None:
                    context["snapshot_cleanup_complete"] = self._cleanup_snapshot(client, snapshot)
        except (SamplerError, OSError):
            context["snapshot_cleanup_complete"] = False
        finally:
            if client:
                self._close_client(client)
            context["_client"] = None
        with self._contexts_lock:
            self._contexts.pop(id(context), None)
        workspace = context.pop("_workspace", None)
        if isinstance(workspace, tempfile.TemporaryDirectory):
            workspace.cleanup()
        # Ephemeral sample threads disappear with the process. Only our own
        # unchanged persistent base may be deleted, after descendant checks.

    def cancel_active(self) -> None:
        """Cancel only currently owned work; leave this sampler reusable."""
        with self._contexts_lock:
            self._cancel_generation += 1
            clients = list(self._clients.values())
            self.phase = "cancelling"
            for context in self._contexts.values():
                context["_cancelled"] = True
                context["phase"] = "cancelling"
                context["_client"] = None
            # Stop protocol submission and wake all request/event waiters
            # synchronously, before waiting for any owned process to exit.
            for client in clients:
                cancel_pending = getattr(client, "cancel_pending", None)
                if callable(cancel_pending):
                    cancel_pending()
        for client in clients:
            self._close_client(client)

    def close(self) -> None:
        """Application shutdown closes owned runtimes, including an active turn."""
        with self._contexts_lock:
            self._closed = True
            contexts = list(self._contexts.values())
            clients = list(self._clients.values())
            self._contexts.clear()
            self._clients.clear()
            for context in contexts:
                context["_closed"] = True
        for client in clients:
            self._close_client(client)
        for context in contexts:
            self.finish_context(context)

    def doctor(self, session_id: str, config: dict[str, Any]) -> dict[str, Any]:
        context = self.create_context(session_id, config)
        try:
            return {"ready": True, "inference_requests": 0, "explicit_turn_start_requests": 0, "temporary": True,
                    "source_session_id": session_id, "source_turn_id": context["source_turn_id"],
                    "source_turn_status": context["source_turn_status"],
                    **context["_config"], "cleanup": "ephemeral_process_closed"}
        finally:
            self.finish_context(context)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查或显式执行目标会话的临时数字采样。")
    parser.add_argument("--session", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--effort", required=True, choices=sorted(EFFORTS))
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--doctor", action="store_true", help="创建并核对临时分支，不发送模型请求")
    operation.add_argument("--prompt-file", type=Path, help="显式发送此文件的一次数字题；会消耗模型额度")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    sampler = CodexSampler(timeout=args.timeout)
    config = {"model": args.model, "provider": args.provider, "effort": args.effort}
    context = None
    try:
        if args.doctor:
            result = sampler.doctor(args.session, config)
        else:
            if args.prompt_file.stat().st_size > 16_384:
                raise SamplerError("invalid_target")
            context = sampler.create_context(args.session, config)
            answer = sampler.sample(context, args.prompt_file.read_text(encoding="utf-8"), config)
            result = {"ready": True, "inference_requests": 1, "source_session_id": args.session,
                      "source_turn_id": context["source_turn_id"], **config, "text": answer}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, SamplerError) as error:
        print(json.dumps({"ready": False, "reason": str(error) if isinstance(error, SamplerError)
                          else REASONS["request_failed"], "code": getattr(error, "code", "request_failed")},
                         ensure_ascii=False))
        return 1
    finally:
        if context:
            sampler.finish_context(context)


if __name__ == "__main__":
    raise SystemExit(main())
