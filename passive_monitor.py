#!/usr/bin/env python3
"""Read bounded local session metadata for digital attribution and scheduling.

Only explicit settings, conversation identity and normal lifecycle counters are
retained. Responses are never interpreted as platform model identity evidence.
"""

from __future__ import annotations

import argparse
import codecs
import heapq
import json
import os
import re
import signal
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 2
MAX_CANDIDATES = 8
MAX_SESSION_CHOICES = 512
MAX_LINE_BYTES = 256 * 1024
WARM_START_BYTES = 2 * 1024 * 1024
BOOTSTRAP_SCAN_BYTES = 16 * 1024 * 1024
BOOTSTRAP_CHUNK_BYTES = 512 * 1024
DISCOVERY_SECONDS = 15.0
FULL_DISCOVERY_SECONDS = 60.0
MAX_TITLE_INDEX_BYTES = 4 * 1024 * 1024
MAX_TITLE_ENTRIES = 2048
MAX_TITLE_LENGTH = 160
MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
ROLLOUT_RUN_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
EFFORTS = frozenset(("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"))


class _ToolCompletionMetadata:
    """Validate a large JSON record while retaining only bounded tool metadata.

    This is used only after a line exceeds the normal JSON size limit. String
    bodies are scanned in-place, never accumulated; object paths determine
    which small fields may be retained. A complete valid record is required
    before any counter changes, including when metadata follows a large body.
    """

    _identities = {"thread_id", "session_id", "threadId", "sessionId"}
    _keys = {
        (): {"type", "timestamp", "payload"} | _identities,
        ("payload",): {"type", "call_id", "id", "request", "response"} | _identities,
        ("payload", "request"): _identities,
        ("payload", "response"): _identities,
    }
    _string_special = re.compile(rb'["\\\x00-\x1f]')
    _number_states = {
        "start": {"-": "sign", "0": "zero", **{str(i): "int" for i in range(1, 10)}},
        "sign": {"0": "zero", **{str(i): "int" for i in range(1, 10)}},
        "zero": {".": "dot", "e": "exponent", "E": "exponent"},
        "int": {**{str(i): "int" for i in range(10)}, ".": "dot", "e": "exponent", "E": "exponent"},
        "dot": {str(i): "fraction" for i in range(10)},
        "fraction": {**{str(i): "fraction" for i in range(10)}, "e": "exponent", "E": "exponent"},
        "exponent": {"+": "exponent_sign", "-": "exponent_sign", **{str(i): "exponent_digits" for i in range(10)}},
        "exponent_sign": {str(i): "exponent_digits" for i in range(10)},
        "exponent_digits": {str(i): "exponent_digits" for i in range(10)},
    }

    def __init__(self) -> None:
        self.invalid = False
        self.root: Any = None
        self.started = False
        self.stack: list[dict[str, Any]] = []
        self.token: Optional[str] = None
        self.capture: Optional[bytearray] = None
        self.target: Optional[tuple[dict[str, Any], str]] = None
        self.escape = False
        self.unicode_remaining = 0
        self.number_state = "start"
        self.literal = b""
        self.literal_index = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")()

    def _append(self, data: bytes) -> None:
        if self.capture is not None:
            if len(self.capture) + len(data) <= 1024:
                self.capture.extend(data)
            else:
                self.capture = None

    def _string_finished(self) -> None:
        value = json.loads(self.capture) if self.capture is not None else None
        if self.token == "key":
            self.stack[-1]["key"] = value
            self.stack[-1]["state"] = "colon"
        elif self.target is not None:
            parent, key = self.target
            parent[key] = value
        self.token, self.capture, self.target = None, None, None

    def _begin_value(self, byte: int) -> None:
        path: Optional[tuple[str, ...]] = None
        target = None
        if not self.stack:
            if self.started:
                self.invalid = True
                return
            self.started, path = True, ()
        else:
            parent = self.stack[-1]
            if parent["state"] not in ("value", "value_or_end"):
                self.invalid = True
                return
            if parent["kind"] == "object" and parent["path"] is not None:
                key = parent["key"]
                if key in self._keys.get(parent["path"], ()):
                    path = parent["path"] + (key,)
                    target = (parent["value"], key)
            parent["state"] = "comma_or_end"
        value: Any = {} if byte == 123 and path in self._keys else None
        if target is not None:
            target[0][target[1]] = value
        elif path == ():
            self.root = value
        if byte in (123, 91):
            if len(self.stack) >= 64:
                self.invalid = True
                return
            self.stack.append({"kind": "object" if byte == 123 else "array",
                               "path": path if value is not None else None, "value": value,
                               "state": "key_or_end" if byte == 123 else "value_or_end", "key": None})
        elif byte == 34:
            self.token, self.target = "string", target
            self.capture = bytearray(b'"') if target is not None else None
        elif byte in b"-0123456789":
            self.token, self.number_state = "number", self._number_states["start"][chr(byte)]
        elif byte in (116, 102, 110):
            self.token = "literal"
            self.literal = {116: b"true", 102: b"false", 110: b"null"}[byte]
            self.literal_index = 1
        else:
            self.invalid = True

    def feed(self, data: bytes) -> None:
        if self.invalid:
            return
        try:
            self.decoder.decode(data)
        except UnicodeError:
            self.invalid = True
            return
        index = 0
        while index < len(data) and not self.invalid:
            byte = data[index]
            if self.token in ("string", "key"):
                if self.unicode_remaining:
                    self._append(data[index:index + 1])
                    if byte not in b"0123456789abcdefABCDEF":
                        self.invalid = True
                    self.unicode_remaining -= 1
                elif self.escape:
                    self._append(data[index:index + 1])
                    self.escape = False
                    if byte == 117:
                        self.unicode_remaining = 4
                    elif byte not in b'"\\/bfnrt':
                        self.invalid = True
                else:
                    special = self._string_special.search(data, index)
                    end = special.start() if special else len(data)
                    self._append(data[index:end])
                    index = end
                    if not special:
                        continue
                    byte = data[index]
                    self._append(data[index:index + 1])
                    if byte == 34:
                        try:
                            self._string_finished()
                        except (ValueError, UnicodeError):
                            self.invalid = True
                    elif byte == 92:
                        self.escape = True
                    else:
                        self.invalid = True
                index += 1
                continue
            if self.token == "number":
                if byte in b"0123456789.eE+-":
                    self.number_state = self._number_states[self.number_state].get(chr(byte), "invalid")
                    self.invalid = self.number_state == "invalid"
                    index += 1
                    continue
                self.invalid = self.number_state not in ("zero", "int", "fraction", "exponent_digits")
                self.token = None
                continue
            if self.token == "literal":
                if byte != self.literal[self.literal_index]:
                    self.invalid = True
                self.literal_index += 1
                if self.literal_index == len(self.literal):
                    self.token = None
                index += 1
                continue
            if byte in b" \t\r":
                index += 1
                continue
            if self.stack:
                frame = self.stack[-1]
                state = frame["state"]
                if byte == 34 and state in ("key", "key_or_end"):
                    self.token, self.capture = "key", bytearray(b'"')
                elif byte == 58 and state == "colon":
                    frame["state"] = "value"
                elif byte == 44 and state == "comma_or_end":
                    frame["state"] = "key" if frame["kind"] == "object" else "value"
                elif byte == 125 and frame["kind"] == "object" and state in ("key_or_end", "comma_or_end"):
                    self.stack.pop()
                elif byte == 93 and frame["kind"] == "array" and state in ("value_or_end", "comma_or_end"):
                    self.stack.pop()
                else:
                    self._begin_value(byte)
            else:
                self._begin_value(byte)
            index += 1

    def finish(self) -> Optional[dict[str, Any]]:
        try:
            self.decoder.decode(b"", final=True)
        except UnicodeError:
            self.invalid = True
        if self.token == "number" and self.number_state in ("zero", "int", "fraction", "exponent_digits"):
            self.token = None
        if self.invalid or self.token or self.stack or not self.started or not isinstance(self.root, dict):
            return None
        payload = self.root.get("payload")
        if (self.root.get("type") == "response_item" and isinstance(payload, dict) and
                payload.get("type") in ("function_call_output", "custom_tool_call_output", "local_shell_call_output")):
            return self.root
        return None


def clean_effort(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in EFFORTS else None


def load_model_config(path: Optional[Path] = None) -> dict[str, Any]:
    config_path = path or Path(__file__).with_name("models.json")
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("schema_version") != 1 or not isinstance(config.get("families"), list):
        raise ValueError("Invalid model configuration")
    aliases = config.get("aliases", {})
    if not isinstance(aliases, dict):
        raise ValueError("Invalid model aliases")
    return config


def normalize_model(value: Any, config: dict[str, Any]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if not MODEL_PATTERN.fullmatch(value):
        return None
    # Only exact, explicitly configured aliases are folded. Suffixes such as
    # sol/terra/luna remain separate model IDs.
    alias = config.get("aliases", {}).get(value, value)
    return alias if isinstance(alias, str) and MODEL_PATTERN.fullmatch(alias) else None


def model_family(model: Optional[str], config: dict[str, Any]) -> Optional[str]:
    if not model:
        return None
    for entry in config["families"]:
        prefix = entry.get("prefix", "")
        if prefix and (model == prefix or model.startswith(prefix + "-")):
            return prefix
    return model


def safe_timestamp(value: Any) -> tuple[float, Optional[str]]:
    if not isinstance(value, str) or len(value) > 64:
        return 0.0, None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp(), parsed.isoformat()
    except (ValueError, OverflowError):
        return 0.0, None


def clean_identifier(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9._:-]+", value) else None


def rollout_matches_session(path: Path, session_id: str) -> bool:
    """Accept native original and resumed-run names, then verify the header."""
    name = path.name
    if not name.startswith("rollout-") or not name.endswith(".jsonl"):
        return False
    stem = name[:-6]
    suffix = "-" + session_id
    if stem.endswith(suffix):
        return True
    prefix, separator, run_id = stem.rpartition("_")
    return bool(separator and prefix.endswith(suffix) and ROLLOUT_RUN_ID.fullmatch(run_id))


def clean_title(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > MAX_LINE_BYTES:
        return None
    # Keep a single display line. Control characters and invisible direction
    # overrides must not change the surrounding native interface or terminal.
    visible = "".join(" " if char.isspace() else char for char in value if char.isspace() or not unicodedata.category(char).startswith("C"))
    title = " ".join(visible.split())
    if not title:
        return None
    return title if len(title) <= MAX_TITLE_LENGTH else title[:MAX_TITLE_LENGTH - 1] + "…"


class SessionTitleIndex:
    """Cache the optional local title index; never inspect conversation text."""

    def __init__(self, codex_home: Optional[Path]) -> None:
        self.path = codex_home / "session_index.jsonl" if codex_home else None
        self._signature: Optional[tuple[int, int, int, int]] = None
        # None is an explicit name reset, not a missing index entry. Retaining
        # that tombstone prevents an older rollout title from reappearing.
        self._titles: dict[str, Optional[str]] = {}

    def _refresh(self) -> None:
        if self.path is None:
            return
        try:
            stat = self.path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if signature == self._signature:
                return
            with self.path.open("rb") as handle:
                start = max(0, stat.st_size - MAX_TITLE_INDEX_BYTES)
                handle.seek(start)
                data = handle.read(MAX_TITLE_INDEX_BYTES)
            lines = data.split(b"\n")
            if start:
                lines = lines[1:]  # The bounded tail may start inside a title.
            titles: dict[str, Optional[str]] = {}
            for line in lines:
                if not line or len(line) > MAX_LINE_BYTES:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not isinstance(row, dict):
                    continue
                identifier = clean_identifier(row.get("id"))
                title = clean_title(row.get("thread_name"))
                if identifier and "thread_name" in row:
                    # Later append-only rows replace earlier names for this ID.
                    titles.pop(identifier, None)
                    if len(titles) >= MAX_TITLE_ENTRIES:
                        titles.pop(next(iter(titles)))
                    titles[identifier] = title
            self._titles, self._signature = titles, signature
        except OSError:
            # Titles are optional UI metadata; access problems must not stop
            # the model monitor or create/modify any Codex files.
            self._titles, self._signature = {}, None

    def resolve(self, session_id: Optional[str], fallback: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        if not session_id:
            return None, None
        self._refresh()
        if session_id in self._titles:
            return self._titles[session_id] or "未命名会话", "session_index"
        title = clean_title(fallback)
        return (title, "session_meta") if title else ("未命名会话", None)


def background_source(source: Any) -> bool:
    names = {"subagent", "internal", "memory_consolidation", "guardian", "guardian_review",
             "model_beacon_attribution", "beacon_attribution"}
    if isinstance(source, str):
        return source.lower() in names
    return isinstance(source, dict) and any(str(key).lower() in names for key in source)


@dataclass
class Conversation:
    path: Path
    config: dict[str, Any]
    session_id: Optional[str] = None
    session_title: Optional[str] = None
    turn_id: Optional[str] = None
    selected_model: Optional[str] = None
    selected_model_source: Optional[str] = None
    selected_effort: Optional[str] = None
    selected_effort_source: Optional[str] = None
    selected_provider: Optional[str] = None
    session_provider: Optional[str] = None
    selected_at: Optional[str] = None
    selected_model_at: float = 0.0
    selected_effort_at: float = 0.0
    observed_model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    configuration_provider: Optional[str] = None
    turn_started_at: float = 0.0
    busy: bool = False
    normal_completed_turns: int = 0
    normal_tool_calls: int = 0
    last_completed_turn_id: Optional[str] = None
    last_completed_turn_config: Optional[dict[str, Optional[str]]] = None
    context_segment: int = 0
    explicit_context_segment: Optional[str] = None
    has_normal_activity: bool = False
    _turn_serial: int = 0
    _completed_turns: set[str] = field(default_factory=set)
    _tool_calls: set[str] = field(default_factory=set)
    updated_at: Optional[str] = None
    activity: float = 0.0
    subagent: bool = False
    has_activity: bool = False
    _started_without_context: bool = False
    _counter_epoch: object = field(default_factory=object, repr=False)

    def reset_turn(self, turn_id: Optional[str] = None) -> None:
        self._turn_serial += 1
        self.turn_id = turn_id
        self.observed_model = None
        self.reasoning_effort = None
        self.configuration_provider = None

    def _matches_session_identifiers(self, event: dict[str, Any], payload: dict[str, Any]) -> bool:
        sources = [event, payload]
        sources.extend(payload[key] for key in ("request", "response") if isinstance(payload.get(key), dict))
        return all(
            self.session_id is not None and clean_identifier(source[key]) == self.session_id
            for source in sources for key in ("thread_id", "session_id", "threadId", "sessionId") if key in source
        )

    def consume(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        payload = event.get("payload", {})
        if not isinstance(kind, str) or not isinstance(payload, dict):
            return
        subtype = payload.get("type")
        if subtype is not None and not isinstance(subtype, str):
            return
        if kind == "session_meta":
            identifier = clean_identifier(payload.get("id")) or clean_identifier(payload.get("session_id"))
            if self.session_id != identifier:
                self.selected_model = self.selected_model_source = None
                self.selected_effort = self.selected_effort_source = self.selected_provider = self.selected_at = None
                self.selected_model_at = self.selected_effort_at = self.turn_started_at = 0.0
                self.reset_turn()
                self.busy = self.has_normal_activity = False
                self.normal_completed_turns = self.normal_tool_calls = self.context_segment = 0
                self.last_completed_turn_id = None
                self.last_completed_turn_config = None
                self.explicit_context_segment = None
                self._completed_turns.clear()
                self._tool_calls.clear()
                self._started_without_context = False
                self.activity, self.updated_at, self.has_activity = 0.0, None, False
            self.session_id = identifier
            self.session_provider = clean_identifier(payload.get("model_provider")) or clean_identifier(payload.get("provider"))
            self.session_title = next((title for key in ("thread_name", "title", "name") if (title := clean_title(payload.get(key)))), None)
            source = payload.get("source")
            self.subagent = (background_source(source) or background_source(payload.get("thread_source")) or
                             payload.get("model_beacon_attribution") is True)
            return
        if self.subagent:
            return

        identity_metadata = kind == "turn_context" or (
            kind == "event_msg" and subtype in {
                "thread_settings_applied", "task_started", "task_complete", "turn_aborted",
            }
        )
        if identity_metadata:
            # A local file scopes metadata to one thread. When a record also
            # names a thread/session, every supplied identifier must agree;
            # even a correlated request ID cannot override a contradictory ID.
            if not self._matches_session_identifiers(event, payload):
                return

        if kind == "event_msg" and subtype == "thread_settings_applied":
            # Saved choices may change while the preceding turn is finishing.
            if not self.session_id or clean_identifier(payload.get("thread_id")) != self.session_id:
                return
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                choice_time, self.selected_at = safe_timestamp(event.get("timestamp"))
                if "model" in settings:
                    self.selected_model = normalize_model(settings["model"], self.config)
                    self.selected_model_source = "thread_settings_applied" if self.selected_model else None
                    self.selected_model_at = choice_time
                effort_key = next((key for key in ("reasoning_effort", "effort", "model_reasoning_effort") if key in settings), None)
                if effort_key:
                    self.selected_effort = clean_effort(settings[effort_key])
                    self.selected_effort_source = "thread_settings_applied" if self.selected_effort else None
                    self.selected_effort_at = choice_time
                provider_key = next((key for key in ("model_provider", "provider") if key in settings), None)
                if provider_key:
                    self.selected_provider = clean_identifier(settings[provider_key])
            return

        if kind == "compacted" or (kind == "event_msg" and subtype in ("context_compacted", "context_reset")):
            if self._matches_session_identifiers(event, payload):
                self.context_segment += 1
                self.explicit_context_segment = None
            return

        if kind == "response_item" and subtype in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            if not self.session_id or not self._matches_session_identifiers(event, payload):
                return
            # Native tool-completion records belong to the validated real
            # session even when a very long turn's start lies outside the
            # bounded bootstrap window. Message bodies are never searched.
            self.has_normal_activity = self.has_activity = True
            timestamp, display_time = safe_timestamp(event.get("timestamp"))
            if timestamp >= self.activity and display_time:
                self.activity, self.updated_at = timestamp, display_time
            call_id = clean_identifier(payload.get("call_id")) or clean_identifier(payload.get("id"))
            if not call_id or call_id not in self._tool_calls:
                self.normal_tool_calls += 1
                if call_id:
                    if len(self._tool_calls) >= 512:
                        self._tool_calls.pop()
                    self._tool_calls.add(call_id)
            return

        timestamp, display_time = safe_timestamp(event.get("timestamp"))
        # Only real turn/conversation lifecycle records influence selection.
        relevant = kind == "turn_context" or (
            kind == "event_msg" and subtype in {"task_started", "task_complete", "turn_aborted"}
        ) or (kind == "response_item" and subtype == "message" and payload.get("role") in ("user", "assistant"))
        if not relevant:
            return
        self.has_activity = True
        if timestamp >= self.activity and display_time:
            self.activity = timestamp
            self.updated_at = display_time

        event_turn = clean_identifier(payload.get("turn_id"))
        if kind == "response_item" and subtype == "message" and payload.get("role") == "user":
            # Some clients append the user message before task_started.
            if not self.busy:
                self.reset_turn(event_turn)
                self.turn_started_at = timestamp
                self._started_without_context = True
                self.busy = self.has_normal_activity = True
            return
        if kind == "event_msg" and subtype == "task_started":
            self.reset_turn(event_turn)
            self.turn_started_at = timestamp
            self.busy = self.has_normal_activity = True
            self._started_without_context = True
            return
        if kind == "turn_context":
            # Keep each turn's sampling configuration separate.
            if not self._started_without_context or (event_turn and self.turn_id and event_turn != self.turn_id):
                self.reset_turn(event_turn)
                self.turn_started_at = timestamp
            self.busy = self.has_normal_activity = True
            self._started_without_context = False
            self.turn_id = event_turn or self.turn_id
            self.observed_model = normalize_model(payload.get("model"), self.config)
            effort = payload.get("effort", payload.get("reasoning_effort"))
            self.reasoning_effort = clean_effort(effort)
            self.configuration_provider = clean_identifier(payload.get("model_provider")) or clean_identifier(payload.get("provider")) or self.session_provider
            segment = clean_identifier(payload.get("context_segment_id")) or clean_identifier(payload.get("context_id"))
            if segment:
                self.explicit_context_segment = segment
            return

        if kind == "event_msg" and subtype in ("task_complete", "turn_aborted"):
            if event_turn and self.turn_id and event_turn != self.turn_id:
                return
            self.busy = False
            if subtype == "task_complete" and self.has_normal_activity:
                completion_key = self.turn_id or f"legacy:{self._turn_serial}"
                if completion_key not in self._completed_turns:
                    self.normal_completed_turns += 1
                    # Capture the configuration at the completion event. A
                    # later context (including an aborted turn in this same
                    # read batch) must not relabel this completion.
                    self.last_completed_turn_id = self.turn_id
                    self.last_completed_turn_config = {
                        "model": self.observed_model, "effort": self.reasoning_effort,
                        "provider": self.configuration_provider,
                        "context_segment": self.explicit_context_segment or f"{self.session_id or 'unknown'}:{self.context_segment}",
                    }
                    if len(self._completed_turns) >= 512:
                        self._completed_turns.pop()
                    self._completed_turns.add(completion_key)
            return

    def snapshot(self) -> dict[str, Any]:
        model = self.selected_model or self.observed_model
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ready" if self.has_activity or model else "idle",
            "label": "会话已就绪" if self.has_activity or model else "等待会话",
            "selected_model": self.selected_model, "selected_model_source": self.selected_model_source,
            "selected_effort": self.selected_effort, "selected_effort_source": self.selected_effort_source,
            "selected_provider": self.selected_provider, "selected_at": self.selected_at,
            "session_provider": self.session_provider,
            "configuration_model": self.observed_model, "configuration_effort": self.reasoning_effort,
            "configuration_provider": self.configuration_provider,
            "observed_model": self.observed_model, "model_family": model_family(model, self.config),
            "reasoning_effort": self.reasoning_effort, "session_id": self.session_id,
            "turn_id": self.turn_id, "busy": self.busy, "turn_running": self.busy,
            "normal_completed_turns": self.normal_completed_turns, "completed_turn_count": self.normal_completed_turns,
            "last_completed_turn_id": self.last_completed_turn_id,
            "last_completed_turn_config": dict(self.last_completed_turn_config) if self.last_completed_turn_config else None,
            "normal_tool_calls": self.normal_tool_calls, "tool_call_count": self.normal_tool_calls,
            "context_segment": self.explicit_context_segment or f"{self.session_id or 'unknown'}:{self.context_segment}",
            "context_available": bool(self.has_normal_activity and self.observed_model),
            "session_title": self.session_title, "session_title_source": "session_meta" if self.session_title else None,
            "source_file": str(self.path), "updated_at": self.updated_at,
            "detail": "已读取会话配置与正常工具计数。",
        }


@dataclass
class FileCursor:
    path: Path
    config: dict[str, Any]
    conversation: Conversation = field(init=False)
    identity: Optional[tuple[int, int]] = None
    offset: int = 0
    pending: bytes = b""
    discarding: bool = False
    initialized: bool = False
    signature: Optional[tuple[int, int]] = None
    caught_up: bool = False
    _oversized: Optional[_ToolCompletionMetadata] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.conversation = Conversation(self.path, self.config)

    def _consume_line(self, line: bytes) -> None:
        if len(line) > MAX_LINE_BYTES:
            return
        try:
            self.conversation.consume(json.loads(line))
        except (ValueError, UnicodeError, RecursionError):
            pass

    def _feed(self, chunk: bytes) -> None:
        parts = chunk.split(b"\n")
        for piece in parts[:-1]:
            self._feed_piece(piece, complete=True)
        self._feed_piece(parts[-1], complete=False)

    def _feed_piece(self, piece: bytes, *, complete: bool) -> None:
        if self.discarding:
            # A warm start may begin inside a record. Its prefix was never
            # validated, so skip to the newline rather than count a suffix.
            if complete:
                self.discarding = False
            self.pending = b""
            return
        if self._oversized is None and len(self.pending) + len(piece) > MAX_LINE_BYTES:
            self._oversized = _ToolCompletionMetadata()
            self._oversized.feed(self.pending)
            self.pending = b""
        if self._oversized is not None:
            self._oversized.feed(piece)
            if complete:
                metadata = self._oversized.finish()
                if metadata is not None:
                    self.conversation.consume(metadata)
                self._oversized = None
        elif complete:
            self._consume_line(self.pending + piece)
            self.pending = b""
        else:
            self.pending += piece

    def _bootstrap_turn(self, handle: Any, warm_start: int, file_size: int) -> None:
        if not warm_start:
            return
        # A long-running turn may have written its context before the regular
        # 2 MiB tail. Search a fixed-size earlier region in bounded chunks. The
        # small overlap completes a metadata line straddling the tail boundary;
        # the entire bootstrap read still cannot exceed 16 MiB.
        end = min(file_size, warm_start + MAX_LINE_BYTES)
        start = max(0, end - BOOTSTRAP_SCAN_BYTES)
        handle.seek(start)
        remaining = end - start
        pending = b""
        discarding = start > 0
        latest: Optional[dict[str, Any]] = None
        latest_settings: Optional[dict[str, Any]] = None

        def metadata(line: bytes) -> None:
            nonlocal latest, latest_settings
            if len(line) > MAX_LINE_BYTES or not any(marker in line for marker in (b'"turn_context"', b'"task_started"', b'"thread_settings_applied"')):
                return
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                return
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                return
            payload = record["payload"]
            if not self.conversation._matches_session_identifiers(record, payload):
                return
            if record.get("type") == "turn_context":
                # Bootstrap retains only explicit local sampling settings.
                sanitized = {key: payload.get(key) for key in ("turn_id", "model", "effort", "reasoning_effort", "model_provider", "provider", "context_segment_id", "context_id") if key in payload}
            elif record.get("type") == "event_msg" and payload.get("type") == "task_started":
                sanitized = {"type": "task_started", "turn_id": payload.get("turn_id")}
            elif record.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
                settings = payload.get("thread_settings")
                if (clean_identifier(payload.get("thread_id")) == self.conversation.session_id and
                        isinstance(settings, dict)):
                    known = {key: settings[key] for key in ("model", "effort", "reasoning_effort", "model_reasoning_effort", "model_provider", "provider") if key in settings}
                    previous_settings = latest_settings["payload"]["thread_settings"] if latest_settings else {}
                    latest_settings = {
                        "type": "event_msg", "timestamp": record.get("timestamp"),
                        "payload": {"type": "thread_settings_applied", "thread_id": payload.get("thread_id"),
                                    "thread_settings": {**previous_settings, **known}},
                    }
                return
            else:
                return
            latest = {"type": record["type"], "timestamp": record.get("timestamp"), "payload": sanitized}

        while remaining:
            chunk = handle.read(min(BOOTSTRAP_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            pieces = chunk.split(b"\n")
            for piece in pieces[:-1]:
                if discarding:
                    discarding = False
                    pending = b""
                    continue
                if len(pending) + len(piece) <= MAX_LINE_BYTES:
                    metadata(pending + piece)
                pending = b""
            if not discarding:
                if len(pending) + len(pieces[-1]) <= MAX_LINE_BYTES:
                    pending += pieces[-1]
                else:
                    pending = b""
                    discarding = True
        if latest_settings is not None:
            self.conversation.consume(latest_settings)
        if latest is not None:
            self.conversation.consume(latest)

    def refresh(self) -> None:
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        signature = (stat.st_mtime_ns, stat.st_size)
        if self.signature == signature and self.identity == identity:
            return
        if self.identity != identity or stat.st_size < self.offset:
            self.conversation = Conversation(self.path, self.config)
            self.offset, self.pending, self.discarding, self.initialized = 0, b"", False, False
            self._oversized = None
        self.identity = identity
        with self.path.open("rb") as handle:
            if not self.initialized:
                header = handle.readline(MAX_LINE_BYTES + 1)
                if header.endswith(b"\n"):
                    self._consume_line(header)
                if self.conversation.subagent:
                    self.offset = stat.st_size
                    self.initialized = True
                    self.caught_up = True
                    self.signature = signature
                    return
                self.offset = max(0, stat.st_size - WARM_START_BYTES)
                self._bootstrap_turn(handle, self.offset, stat.st_size)
                handle.seek(self.offset)
                if self.offset:
                    # The warm start may begin midway through a large record.
                    # Skip bounded chunks until its next newline.
                    self.discarding = True
                self.initialized = True
            else:
                handle.seek(self.offset)
            chunk = handle.read(WARM_START_BYTES)
            self.offset = handle.tell()
            self._feed(chunk)
        self.caught_up = self.offset >= stat.st_size
        self.signature = signature if self.caught_up else None

    def snapshot(self) -> dict[str, Any]:
        result = self.conversation.snapshot()
        if not self.caught_up:
            result.update(status="loading", label="读取会话中", observed_model=None, model_family=None,
                          reasoning_effort=None, selected_model=None, selected_model_source=None,
                          selected_effort=None, selected_effort_source=None, selected_provider=None, session_provider=None,
                          configuration_model=None, configuration_effort=None, configuration_provider=None,
                          context_available=False, busy=True, turn_running=True,
                          detail="正在读取新增会话记录，暂不使用尚未读完的配置。")
        return result


def empty_snapshot(status: str = "idle", detail: Optional[str] = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "status": status,
        "label": "读取失败" if status == "error" else "等待会话",
        "selected_model": None, "selected_model_source": None,
        "selected_effort": None, "selected_effort_source": None, "selected_provider": None, "session_provider": None,
        "selected_at": None, "configuration_model": None, "configuration_effort": None, "configuration_provider": None,
        "model_family": None, "reasoning_effort": None, "observed_model": None, "session_id": None,
        "turn_id": None, "busy": False, "turn_running": False, "normal_completed_turns": 0,
        "completed_turn_count": 0, "normal_tool_calls": 0, "tool_call_count": 0,
        "context_segment": None, "context_available": False,
        "session_title": None, "session_title_source": None, "source_file": None, "updated_at": None,
        "detail": detail or "选择会话后可开始数字归因。",
    }


class PassiveMonitor:
    def __init__(self, sessions_dir: Path, config: Optional[dict[str, Any]] = None, session: Optional[str] = None, codex_home: Optional[Path] = None,
                 state_file: Optional[Path] = None) -> None:
        self.sessions_dir = sessions_dir
        self.config = config if config is not None else load_model_config()
        self.session = clean_identifier(session)
        self.state_file = state_file
        self.binding_error: Optional[str] = None
        if state_file and not session:
            try:
                if state_file.stat().st_size > 65536:
                    raise ValueError("oversized state")
                with state_file.open(encoding="utf-8") as handle:
                    saved = json.load(handle)
                if not isinstance(saved, dict):
                    raise ValueError("invalid state")
                if saved.get("mode") == "bound":
                    self.session = clean_identifier(saved.get("session_id"))
                    if not self.session:
                        raise ValueError("invalid session")
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                self.binding_error = "无法恢复已保存的会话绑定；请重新选择会话。"
        title_home = codex_home or (sessions_dir.parent if sessions_dir.name == "sessions" else None)
        self.titles = SessionTitleIndex(title_home)
        self.cursors: dict[Path, FileCursor] = {}
        self._last_discovery = float("-inf")
        self._last_full_discovery = float("-inf")
        self._header_cache: dict[Path, tuple[tuple[int, int], int, bool, Optional[str]]] = {}
        self._session_catalog: dict[str, tuple[Path, float]] = {}
        self._catalog_titles: dict[str, str] = {}
        self._scan_failure: Optional[str] = None
        self.excluded_sessions: set[str] = set()
        self._live_counters: dict[str, dict[str, Any]] = {}

    def _apply_live_counters(self, cursor: FileCursor, result: dict[str, Any]) -> None:
        """Count new records only, keeping one continuous total across runs."""
        session_id = cursor.conversation.session_id
        if not session_id:
            return
        current = self._live_counters.get(session_id)
        if cursor.caught_up:
            tools = cursor.conversation.normal_tool_calls
            completed = cursor.conversation.normal_completed_turns
            if current is None:
                if len(self._live_counters) >= MAX_SESSION_CHOICES:
                    self._live_counters.pop(next(iter(self._live_counters)))
                current = {"epoch": cursor.conversation._counter_epoch, "tools_base": tools,
                           "completed_base": completed, "tools": 0, "completed": 0}
                self._live_counters[session_id] = current
            elif current["epoch"] is cursor.conversation._counter_epoch:
                current["tools"] += max(0, tools - current["tools_base"])
                current["completed"] += max(0, completed - current["completed_base"])
                current["tools_base"], current["completed_base"] = tools, completed
            else:
                # A resumed native run can replay a large historical tail.
                # Preserve totals but establish its fresh cursor as a baseline.
                current.update(epoch=cursor.conversation._counter_epoch, tools_base=tools,
                               completed_base=completed)
        result.update(normal_tool_calls=current["tools"] if current else 0,
                      tool_call_count=current["tools"] if current else 0,
                      normal_completed_turns=current["completed"] if current else 0,
                      completed_turn_count=current["completed"] if current else 0)

    def exclude_sessions(self, identifiers: set[str]) -> None:
        owned = {value for value in identifiers if clean_identifier(value) == value}
        if owned != self.excluded_sessions:
            self.excluded_sessions = owned
            self._last_discovery = self._last_full_discovery = float("-inf")
            self.cursors = {path: cursor for path, cursor in self.cursors.items()
                            if cursor.conversation.session_id not in owned}

    def _save_binding(self) -> None:
        if self.state_file is None:
            return
        temporary = self.state_file.with_name(self.state_file.name + f".{os.getpid()}.{time.monotonic_ns()}.tmp")
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                json.dump({"schema_version": 1, "mode": "bound" if self.session else "recent_activity",
                           "session_id": self.session}, handle, ensure_ascii=False)
            temporary.replace(self.state_file)
            self.binding_error = None
        except OSError:
            self.binding_error = "已切换会话，但无法保存绑定；请检查应用数据目录的写入权限。"
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def bind(self, session_id: str) -> None:
        """Switch by exact identity, clearing all previous visible state."""
        if clean_identifier(session_id) != session_id:
            raise ValueError("无效的会话 ID")
        self.session = session_id
        self.cursors.clear()
        self._last_discovery = self._last_full_discovery = float("-inf")
        self._save_binding()

    def follow_recent(self) -> None:
        """Follow log activity; this is never presented as foreground tracking."""
        self.session = None
        self.cursors.clear()
        self._last_discovery = self._last_full_discovery = float("-inf")
        self._save_binding()

    def _decorate(self, result: dict[str, Any]) -> dict[str, Any]:
        choices = []
        for identifier, (path, modified) in sorted(self._session_catalog.items(), key=lambda row: (-row[1][1], row[0])):
            cursor = self.cursors.get(path)
            fallback = cursor.conversation.session_title if cursor else self._catalog_titles.get(identifier)
            title, _source = self.titles.resolve(identifier, fallback)
            choices.append({"session_id": identifier, "session_title": title,
                            "updated_at": datetime.fromtimestamp(modified, timezone.utc).isoformat()})
        if self.session and not any(row["session_id"] == self.session for row in choices):
            title, _source = self.titles.resolve(self.session)
            choices.insert(0, {"session_id": self.session, "session_title": title, "updated_at": None})
        result.update(selection_mode="bound" if self.session else "recent_activity",
                      bound_session_id=self.session, sessions=choices, binding_error=self.binding_error)
        if self.session and result.get("session_id") is None:
            result["session_id"] = self.session
        if self.binding_error:
            result["detail"] = self.binding_error + " " + result["detail"]
        return result

    def _eligible(self, path: Path, stat: os.stat_result) -> bool:
        identity = (stat.st_dev, stat.st_ino)
        cached = self._header_cache.get(path)
        if not cached or cached[0] != identity or stat.st_size < cached[1]:
            try:
                with path.open("rb") as handle:
                    raw = handle.readline(MAX_LINE_BYTES + 1)
                meta = json.loads(raw) if len(raw) <= MAX_LINE_BYTES else {}
                payload = meta.get("payload", {}) if meta.get("type") == "session_meta" else {}
                source = payload.get("source")
                subagent = (background_source(source) or background_source(payload.get("thread_source")) or
                            payload.get("model_beacon_attribution") is True)
                session_id = clean_identifier(payload.get("id")) or clean_identifier(payload.get("session_id"))
                title = next((title for key in ("thread_name", "title", "name") if (title := clean_title(payload.get(key)))), None)
                if session_id and title:
                    if len(self._catalog_titles) >= MAX_SESSION_CHOICES * 2:
                        self._catalog_titles.pop(next(iter(self._catalog_titles)))
                    self._catalog_titles[session_id] = title
                cached = (identity, stat.st_size, subagent, session_id)
                if len(self._header_cache) >= MAX_SESSION_CHOICES * 2:
                    self._header_cache.pop(next(iter(self._header_cache)))
                self._header_cache[path] = cached
            except PermissionError:
                self._scan_failure = "会话文件访问被拒绝，请检查目录访问权限。"
                return False
            except (OSError, ValueError, UnicodeError, AttributeError):
                return False
        return bool(not cached[2] and cached[3] and cached[3] not in self.excluded_sessions
                    and rollout_matches_session(path, cached[3]))

    def _discover(self, now: float) -> None:
        if now - self._last_discovery < DISCOVERY_SECONDS:
            return
        full = now - self._last_full_discovery >= FULL_DISCOVERY_SECONDS
        self._last_discovery = now
        self._scan_failure = None
        if full:
            self._last_full_discovery = now
        root = self.sessions_dir
        if not root.exists():
            return
        candidates: set[Path] = set(self.cursors)
        if not full:
            candidates.update(row[0] for row in self._session_catalog.values())
        if full:
            def walk_error(_error: OSError) -> None:
                self._scan_failure = "无法访问会话目录，请检查目录访问权限。"

            def newest_directories():
                for directory, subdirs, _ in os.walk(root, followlinks=False, onerror=walk_error):
                    subdirs[:] = sorted((name for name in subdirs if not (Path(directory) / name).is_symlink()), reverse=True)
                    yield Path(directory)
            directories = newest_directories()
        else:
            # Codex normally writes sessions/YYYY/MM/DD. Only the most recent
            # date directories need frequent discovery; a low-frequency scan
            # also finds old conversations that have been resumed.
            date_dirs = heapq.nlargest(3, (path for path in root.glob("*/*/*") if path.is_dir() and not path.is_symlink()), key=lambda path: path.name if path.parent.parent == root else str(path))
            directories = iter([root, *date_dirs])
        newest: list[tuple[float, str, Path]] = []
        for directory in directories:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not entry.name.startswith("rollout-") or not entry.name.endswith(".jsonl") or not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        item = (stat.st_mtime, entry.path, Path(entry.path))
                        is_target = self.session and rollout_matches_session(Path(entry.name), self.session)
                        if not is_target and len(newest) >= MAX_SESSION_CHOICES and item <= newest[0]:
                            continue
                        if not self._eligible(item[2], stat):
                            continue
                        if is_target:
                            candidates.add(item[2])
                        if len(newest) < MAX_SESSION_CHOICES:
                            heapq.heappush(newest, item)
                        elif item > newest[0]:
                            heapq.heapreplace(newest, item)
            except PermissionError:
                self._scan_failure = "会话目录访问被拒绝，请检查目录访问权限。"
                continue
            except FileNotFoundError:
                continue
        candidates.update(item[2] for item in newest)
        ordered: list[tuple[float, str, Path]] = []
        for path in candidates:
            try:
                if not path.is_symlink():
                    stat = path.stat()
                    if self._eligible(path, stat):
                        ordered.append((stat.st_mtime, str(path), path))
            except FileNotFoundError:
                continue
        self._session_catalog = {}
        for modified, _name, path in heapq.nlargest(MAX_SESSION_CHOICES, ordered):
            identifier = self._header_cache[path][3]
            if identifier and identifier not in self._session_catalog:
                self._session_catalog[identifier] = (path, modified)
        # Keep only the newest physical run per conversation. Older segments
        # cannot take candidate slots or become current again due to old text.
        selected = [row[0] for row in list(self._session_catalog.values())[:MAX_CANDIDATES]]
        if self.session:
            current = self._session_catalog.get(self.session)
            selected = [current[0]] if current else []
        self.cursors = {path: self.cursors.get(path) or FileCursor(path, self.config) for path in selected}

    def poll(self, now: Optional[float] = None) -> dict[str, Any]:
        try:
            self._discover(time.monotonic() if now is None else now)
            failures = 0
            available: list[FileCursor] = []
            for cursor in self.cursors.values():
                try:
                    cursor.refresh()
                except OSError:
                    failures += 1
                    continue
                conversation = cursor.conversation
                if ((conversation.has_activity or (self.session and (conversation.selected_model or conversation.selected_effort))) and not conversation.subagent and
                        (self.session is None or conversation.session_id == self.session)):
                    available.append(cursor)
            if available:
                latest = max(available, key=lambda item: (item.conversation.activity, str(item.path)))
                result = latest.snapshot()
                self._apply_live_counters(latest, result)
                title, source = self.titles.resolve(result["session_id"], result["session_title"])
                result.update(session_title=title, session_title_source=source)
                return self._decorate(result)
            if failures or self._scan_failure:
                return self._decorate(empty_snapshot("error", self._scan_failure or "无法读取本地会话文件，请检查目录访问权限。"))
            result = empty_snapshot(detail="等待指定会话的配置记录；可手动核对并采样。" if self.session else None)
            if self.session:
                title, source = self.titles.resolve(self.session)
                result.update(session_title=title, session_title_source=source)
            return self._decorate(result)
        except OSError:
            return self._decorate(empty_snapshot("error", "无法扫描本地会话目录，请检查目录访问权限。"))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="读取本地 Codex 元数据；明确绑定后提供独立数字归因控制（可用性受入口和参考库约束）。")
    parser.add_argument("--watch", action="store_true", help="持续监测，仅在状态变化时输出一行 JSON")
    parser.add_argument("--interval", type=float, default=1.0, help="检查间隔秒数，最小 1 秒（默认 1）")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("--sessions-dir", type=Path, help="指定本地 sessions 目录，用于自定义存放位置或离线验证")
    parser.add_argument("--session", help="锁定指定会话 ID；只读取文件名及 session_meta.id 均匹配的主会话")
    parser.add_argument("--state-file", type=Path, help="绑定状态文件；归因历史保存在同一目录")
    parser.add_argument("--control-stdin", action="store_true", help="监测时接收 stdin JSON 控制：bind/follow_recent/attribute/set_auto/sample_new/cancel_attribution")
    args = parser.parse_args(argv)
    if args.interval < 1 or not args.interval < float("inf"):
        parser.error("--interval 必须是至少 1 秒的有限数值")
    if args.session and clean_identifier(args.session) != args.session:
        parser.error("--session 必须是有效的会话 ID")
    state_path = args.state_file
    if state_path is None and args.sessions_dir is None:
        state_path = Path.home() / "Library" / "Application Support" / "Model Beacon" / "monitor-state.json"
    try:
        monitor = PassiveMonitor(args.sessions_dir or args.codex_home / "sessions", session=args.session,
                                 codex_home=None if args.sessions_dir else args.codex_home, state_file=state_path)
        if args.session:
            monitor.bind(args.session)
    except (OSError, ValueError):
        print(json.dumps(empty_snapshot("error", "型号配置不可用，请重新构建或恢复 models.json。"), ensure_ascii=False), flush=True)
        return 1
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    from monitor_runtime import run_monitor
    return run_monitor(monitor, watch=args.watch, interval=args.interval, control_stdin=args.control_stdin,
                       state_path=state_path, stopping=lambda: stopping)


if __name__ == "__main__":
    raise SystemExit(main())
