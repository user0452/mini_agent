"""A minimal tool-calling agent built without any agent framework."""

from __future__ import annotations

import ast
import argparse
import json
import operator
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path
from threading import Lock
from typing import Any, Callable


SYSTEM_PROMPT = """你是一个简洁、可靠的 AI 助手。
需要计算时使用 calculator；需要搜索信息时使用 search；需要读取本地文档时使用 read_docs。
可以连续调用多个工具。工具结果足够后，直接回答用户，不要再调用工具。
"""

load_dotenv()

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算一个只包含数字、括号和常见算术运算符的表达式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "例如：(12 + 8) * 3 / 2",
                    }
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "搜索信息。当前是本地 mock 搜索，用于演示工具调用流程。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_docs",
            "description": "读取 docs 目录下的 UTF-8 文本文档。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于 docs 目录的文件路径，例如 intro.md",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


_BINARY_OPERATORS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def calculator(expression: str) -> int | float:
    """Safely evaluate a small arithmetic expression without using eval()."""
    if len(expression) > 200:
        raise ValueError("表达式过长")

    tree = ast.parse(expression, mode="eval")

    def evaluate(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("只允许数字常量")
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            return _UNARY_OPERATORS[type(node.op)](evaluate(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("指数绝对值不能超过 100")
            result = _BINARY_OPERATORS[type(node.op)](left, right)
            if isinstance(result, (int, float)) and abs(result) > 1e100:
                raise ValueError("计算结果过大")
            return result
        raise ValueError("表达式包含不支持的语法")

    return evaluate(tree)


_MOCK_SEARCH_DATA = [
    {
        "title": "Python",
        "url": "https://www.python.org/",
        "snippet": "Python 是一种强调可读性的通用编程语言。",
        "keywords": "python 编程 语言",
    },
    {
        "title": "Tool calling",
        "url": "https://platform.openai.com/docs/guides/function-calling",
        "snippet": "Tool calling 让模型生成结构化参数，由应用程序执行函数。",
        "keywords": "tool calling function call 工具调用",
    },
    {
        "title": "Minimal agent",
        "url": "mock://minimal-agent",
        "snippet": "最小 Agent 可以由模型、工具注册表、消息历史和循环组成。",
        "keywords": "agent 智能体 while loop 最小",
    },
]


def search(query: str) -> list[dict[str, str]]:
    """Return deterministic mock search results."""
    terms = {term.lower() for term in re.findall(r"[\w\u4e00-\u9fff]+", query)}
    ranked: list[tuple[int, dict[str, str]]] = []
    for item in _MOCK_SEARCH_DATA:
        haystack = f"{item['title']} {item['snippet']} {item['keywords']}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    results = [item for _, item in ranked[:3]]
    if results:
        return results
    return [
        {
            "title": "Mock search",
            "url": "mock://no-result",
            "snippet": f"没有找到与“{query}”匹配的 mock 数据。",
        }
    ]


def read_docs(path: str, docs_dir: Path | None = None) -> str:
    """Read one text file while preventing access outside docs_dir."""
    root = (docs_dir or Path(__file__).parent / "docs").resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("只能读取 docs 目录内的文件") from exc

    if target.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("只支持 .md 和 .txt 文档")
    if not target.is_file():
        available = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        raise FileNotFoundError(f"文档不存在。可用文档：{available}")
    return target.read_text(encoding="utf-8")[:12_000]


class OpenAICompatibleClient:
    """Tiny Chat Completions client implemented with Python's standard library."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 60,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        if backoff_base < 0:
            raise ValueError("backoff_base 不能为负数")
        base_url = base_url.rstrip("/")
        self.url = (
            base_url
            if base_url.endswith("/chat/completions")
            else f"{base_url}/chat/completions"
        )
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base

    def _sleep_before_retry(self, failed_attempt: int) -> None:
        """Exponential backoff plus 0%-25% random jitter."""
        exponential = self.backoff_base * (2 ** (failed_attempt - 1))
        jitter = random.uniform(0, exponential * 0.25)
        time.sleep(exponential + jitter)

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")

        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code in {408, 429} or 500 <= exc.code <= 599
                if not retryable or attempt == self.max_attempts:
                    raise RuntimeError(
                        f"API 请求失败：HTTP {exc.code}\n{detail}"
                    ) from exc
                self._sleep_before_retry(attempt)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self.max_attempts:
                    reason = getattr(exc, "reason", exc)
                    raise RuntimeError(
                        f"无法连接模型 API，已尝试 {self.max_attempts} 次：{reason}"
                    ) from exc
                self._sleep_before_retry(attempt)

        try:
            return body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"API 返回格式不正确：{body}") from exc

    def chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            {
                "model": self.model,
                "messages": messages,
                "tools": TOOL_SCHEMAS,
                "tool_choice": "auto",
            }
        )

    def summarize(self, messages: list[dict[str, Any]]) -> str:
        """Call the same model without tools and retain content only."""
        message = self._request({"model": self.model, "messages": messages})
        return message.get("content") or ""


class SessionStore:
    """Persist raw turns and independent five-turn summaries per session."""

    def __init__(self, db_path: Path):
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id)
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "turn_count" not in columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_no INTEGER NOT NULL,
                    messages_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id, turn_no)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS summaries (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    start_turn INTEGER NOT NULL,
                    end_turn INTEGER NOT NULL,
                    summary_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id, end_turn)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_traces (
                    trace_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_no INTEGER NOT NULL,
                    model_step INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_ms REAL,
                    PRIMARY KEY (trace_id, tool_call_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tool_traces_session
                ON tool_traces (user_id, session_id, started_at DESC)
                """
            )
            connection.commit()

    @staticmethod
    def _split_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Convert a legacy flat history into user-led complete turns."""
        turns: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "system":
                continue
            if message.get("role") == "user":
                if current:
                    turns.append(current)
                current = [message]
            elif current:
                current.append(message)
        if current:
            turns.append(current)
        return turns

    def _migrate_legacy_turns(self, user_id: str, session_id: str) -> None:
        """Lazily add turn boundaries to sessions created by the earlier version."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT messages_json FROM sessions
                WHERE user_id = ? AND session_id = ?
                """,
                (user_id, session_id),
            ).fetchone()
            if row is None:
                return
            turns = self._split_turns(json.loads(row[0]))
            now = datetime.now(timezone.utc).isoformat()
            for turn_no, turn in enumerate(turns, start=1):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO conversation_turns
                    (user_id, session_id, turn_no, messages_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        session_id,
                        turn_no,
                        json.dumps(turn, ensure_ascii=False),
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE sessions SET turn_count = ?
                WHERE user_id = ? AND session_id = ?
                """,
                (len(turns), user_id, session_id),
            )
            connection.commit()

    def load_messages(self, user_id: str, session_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT messages_json
                FROM sessions
                WHERE user_id = ? AND session_id = ?
                """,
                (user_id, session_id),
            ).fetchone()
        if row is None:
            return []
        messages = json.loads(row[0])
        if not isinstance(messages, list):
            raise RuntimeError("Session 中的消息格式无效")
        return messages

    def save_messages(
        self,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Replace raw history; retained for compatibility and test setup."""
        payload = json.dumps(messages, ensure_ascii=False)
        updated_at = datetime.now(timezone.utc).isoformat()
        turns = self._split_turns(messages)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO sessions
                    (user_id, session_id, messages_json, updated_at, turn_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, session_id) DO UPDATE SET
                    messages_json = excluded.messages_json,
                    updated_at = excluded.updated_at,
                    turn_count = excluded.turn_count
                """,
                (user_id, session_id, payload, updated_at, len(turns)),
            )
            connection.execute(
                "DELETE FROM conversation_turns WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            connection.execute(
                "DELETE FROM summaries WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            for turn_no, turn in enumerate(turns, start=1):
                connection.execute(
                    """
                    INSERT INTO conversation_turns
                    (user_id, session_id, turn_no, messages_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        session_id,
                        turn_no,
                        json.dumps(turn, ensure_ascii=False),
                        updated_at,
                    ),
                )
            connection.commit()

    def append_turn(
        self,
        user_id: str,
        session_id: str,
        turn_messages: list[dict[str, Any]],
    ) -> int:
        """Atomically append one complete raw turn and return its turn number."""
        if not turn_messages or turn_messages[0].get("role") != "user":
            raise ValueError("一个对话轮次必须以 user 消息开始")
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT messages_json, turn_count FROM sessions
                WHERE user_id = ? AND session_id = ?
                """,
                (user_id, session_id),
            ).fetchone()
            if row is None:
                all_messages: list[dict[str, Any]] = []
                turn_no = 1
            else:
                all_messages = json.loads(row[0])
                turn_no = int(row[1]) + 1
            all_messages.extend(turn_messages)
            connection.execute(
                """
                INSERT INTO conversation_turns
                    (user_id, session_id, turn_no, messages_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_id,
                    turn_no,
                    json.dumps(turn_messages, ensure_ascii=False),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO sessions
                    (user_id, session_id, messages_json, updated_at, turn_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, session_id) DO UPDATE SET
                    messages_json = excluded.messages_json,
                    updated_at = excluded.updated_at,
                    turn_count = excluded.turn_count
                """,
                (
                    user_id,
                    session_id,
                    json.dumps(all_messages, ensure_ascii=False),
                    now,
                    turn_no,
                ),
            )
            connection.commit()
        return turn_no

    def load_turns(
        self,
        user_id: str,
        session_id: str,
        start_turn: int = 1,
        end_turn: int | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [user_id, session_id, start_turn]
        end_clause = ""
        if end_turn is not None:
            end_clause = "AND turn_no <= ?"
            parameters.append(end_turn)
        query = f"""
            SELECT turn_no, messages_json
            FROM conversation_turns
            WHERE user_id = ? AND session_id = ? AND turn_no >= ? {end_clause}
            ORDER BY turn_no
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        if not rows and start_turn == 1:
            self._migrate_legacy_turns(user_id, session_id)
            with closing(self._connect()) as connection:
                rows = connection.execute(query, parameters).fetchall()
        return [
            {"turn_no": turn_no, "messages": json.loads(messages_json)}
            for turn_no, messages_json in rows
        ]

    def get_turn_count(self, user_id: str, session_id: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT turn_count FROM sessions
                WHERE user_id = ? AND session_id = ?
                """,
                (user_id, session_id),
            ).fetchone()
        return int(row[0]) if row else 0

    def save_summary(
        self,
        user_id: str,
        session_id: str,
        end_turn: int,
        summary_text: str,
    ) -> None:
        start_turn = end_turn - 4
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO summaries
                    (user_id, session_id, start_turn, end_turn, summary_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, session_id, end_turn) DO UPDATE SET
                    summary_text = excluded.summary_text,
                    start_turn = excluded.start_turn,
                    created_at = excluded.created_at
                """,
                (user_id, session_id, start_turn, end_turn, summary_text, now),
            )
            connection.commit()

    def load_summary(
        self,
        user_id: str,
        session_id: str,
        end_turn: int,
    ) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT summary_text FROM summaries
                WHERE user_id = ? AND session_id = ?
                    AND start_turn = ? AND end_turn = ?
                """,
                (user_id, session_id, end_turn - 4, end_turn),
            ).fetchone()
        return row[0] if row else None

    def list_summaries(self, user_id: str, session_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT start_turn, end_turn, summary_text, created_at
                FROM summaries
                WHERE user_id = ? AND session_id = ?
                ORDER BY end_turn
                """,
                (user_id, session_id),
            ).fetchall()
        return [
            {
                "start_turn": start,
                "end_turn": end,
                "summary": summary,
                "created_at": created_at,
            }
            for start, end, summary, created_at in rows
        ]

    def start_tool_trace(
        self,
        *,
        trace_id: str,
        tool_call_id: str,
        user_id: str,
        session_id: str,
        turn_no: int,
        model_step: int,
        tool_name: str,
        arguments_json: str,
        started_at: str,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO tool_traces (
                    trace_id, tool_call_id, user_id, session_id, turn_no,
                    model_step, tool_name, arguments_json, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    trace_id,
                    tool_call_id,
                    user_id,
                    session_id,
                    turn_no,
                    model_step,
                    tool_name,
                    arguments_json,
                    started_at,
                ),
            )
            connection.commit()

    def finish_tool_trace(
        self,
        *,
        trace_id: str,
        tool_call_id: str,
        status: str,
        result_json: str,
        error_type: str | None,
        error_message: str | None,
        finished_at: str,
        duration_ms: float,
    ) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError(f"无效的工具 Trace 状态：{status}")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE tool_traces SET
                    status = ?, result_json = ?, error_type = ?,
                    error_message = ?, finished_at = ?, duration_ms = ?
                WHERE trace_id = ? AND tool_call_id = ?
                """,
                (
                    status,
                    result_json,
                    error_type,
                    error_message,
                    finished_at,
                    duration_ms,
                    trace_id,
                    tool_call_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("找不到需要结束的工具 Trace")
            connection.commit()

    def list_tool_traces(
        self,
        user_id: str,
        session_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit 必须在 1 到 200 之间")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT trace_id, tool_call_id, turn_no, model_step, tool_name,
                       arguments_json, status, result_json, error_type,
                       error_message, started_at, finished_at, duration_ms
                FROM tool_traces
                WHERE user_id = ? AND session_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (user_id, session_id, limit),
            ).fetchall()
        keys = [
            "trace_id",
            "tool_call_id",
            "turn_no",
            "model_step",
            "tool_name",
            "arguments_json",
            "status",
            "result_json",
            "error_type",
            "error_message",
            "started_at",
            "finished_at",
            "duration_ms",
        ]
        return [dict(zip(keys, row)) for row in rows]

    def list_sessions(self, user_id: str) -> list[dict[str, str]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT session_id, updated_at
                FROM sessions
                WHERE user_id = ?
                ORDER BY updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [
            {"session_id": session_id, "updated_at": updated_at}
            for session_id, updated_at in rows
        ]

    def clear_session(self, user_id: str, session_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM summaries WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            connection.execute(
                "DELETE FROM tool_traces WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            connection.execute(
                "DELETE FROM conversation_turns WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            connection.execute(
                "DELETE FROM sessions WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            connection.commit()


class MiniAgent:
    def __init__(
        self,
        client: OpenAICompatibleClient,
        docs_dir: Path | None = None,
        max_steps: int = 8,
        session_store: SessionStore | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
        reasoning_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.client = client
        self.docs_dir = docs_dir
        self.max_steps = max_steps
        self.session_store = session_store
        self.trace_callback = trace_callback
        self.reasoning_callback = reasoning_callback
        self._summary_executor = (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="session-summary")
            if session_store and hasattr(client, "summarize")
            else None
        )
        self._summary_futures: dict[tuple[str, str, int], Future[str]] = {}
        self._summary_lock = Lock()
        self.tool_registry: dict[str, Callable[..., Any]] = {
            "calculator": calculator,
            "search": search,
            "read_docs": lambda path: read_docs(path, self.docs_dir),
        }

    @staticmethod
    def _trace_json(value: Any, limit: int = 20_000) -> str:
        sensitive_keys = {
            "api_key",
            "apikey",
            "authorization",
            "password",
            "secret",
            "token",
        }

        def redact(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    key: "***REDACTED***" if key.lower() in sensitive_keys else redact(value)
                    for key, value in item.items()
                }
            if isinstance(item, list):
                return [redact(value) for value in item]
            return item

        try:
            serialized = json.dumps(redact(value), ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = json.dumps(str(value), ensure_ascii=False)
        if len(serialized) <= limit:
            return serialized
        return serialized[:limit] + "…<truncated>"

    def _emit_trace(self, event: dict[str, Any]) -> None:
        if self.trace_callback is None:
            return
        try:
            self.trace_callback(event)
        except Exception:
            # Observability callbacks must never break the agent loop.
            pass

    def _emit_reasoning(self, event: dict[str, Any]) -> None:
        if self.reasoning_callback is None:
            return
        try:
            self.reasoning_callback(event)
        except Exception:
            # Display callbacks must never break the agent loop.
            pass

    @staticmethod
    def _extract_reasoning(
        assistant_message: dict[str, Any],
    ) -> tuple[str, Any]:
        """Extract provider-visible reasoning without retaining it in messages."""
        reasoning_parts: list[str] = []
        seen: set[str] = set()

        for field in ("reasoning_content", "reasoning"):
            value = assistant_message.get(field)
            if value is None:
                continue
            if isinstance(value, str):
                text = value.strip()
            else:
                text = json.dumps(value, ensure_ascii=False)
            if text and text not in seen:
                reasoning_parts.append(text)
                seen.add(text)

        visible_content = assistant_message.get("content")
        if isinstance(visible_content, str):
            think_pattern = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
            for match in think_pattern.finditer(visible_content):
                text = match.group(1).strip()
                if text and text not in seen:
                    reasoning_parts.append(text)
                    seen.add(text)
            visible_content = think_pattern.sub("", visible_content).strip()

        return "\n\n".join(reasoning_parts), visible_content

    def _execute_tool(
        self,
        tool_call: dict[str, Any],
        *,
        trace_id: str,
        user_id: str,
        session_id: str,
        turn_no: int,
        model_step: int,
    ) -> str:
        function = tool_call.get("function", {})
        name = function.get("name", "")
        tool_call_id = tool_call["id"]
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments_for_trace = json.loads(raw_arguments)
        except (json.JSONDecodeError, TypeError):
            arguments_for_trace = raw_arguments
        arguments_json = self._trace_json(arguments_for_trace)
        started_at = datetime.now(timezone.utc).isoformat()
        started_clock = time.perf_counter()
        trace_persisted = False
        running_event = {
            "event": "tool.running",
            "trace_id": trace_id,
            "tool_call_id": tool_call_id,
            "user_id": user_id,
            "session_id": session_id,
            "turn_no": turn_no,
            "model_step": model_step,
            "tool_name": name,
            "arguments_json": arguments_json,
            "started_at": started_at,
        }
        if self.session_store:
            try:
                self.session_store.start_tool_trace(
                    trace_id=trace_id,
                    tool_call_id=tool_call_id,
                    user_id=user_id,
                    session_id=session_id,
                    turn_no=turn_no,
                    model_step=model_step,
                    tool_name=name,
                    arguments_json=arguments_json,
                    started_at=started_at,
                )
                trace_persisted = True
            except Exception as exc:
                self._emit_trace(
                    {
                        **running_event,
                        "event": "tool.trace_persistence_failed",
                        "error": str(exc),
                    }
                )
        self._emit_trace(running_event)

        error_type = None
        error_message = None
        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")
            tool = self.tool_registry.get(name)
            if tool is None:
                raise ValueError(f"未知工具：{name}")
            result = tool(**arguments)
            status = "succeeded"
            tool_result = json.dumps(
                {"ok": True, "result": result},
                ensure_ascii=False,
            )
        except Exception as exc:  # Tool errors are observations for the model.
            status = "failed"
            error_type = type(exc).__name__
            error_message = str(exc)
            tool_result = json.dumps(
                {"ok": False, "error": error_message},
                ensure_ascii=False,
            )

        finished_at = datetime.now(timezone.utc).isoformat()
        duration_ms = round((time.perf_counter() - started_clock) * 1000, 3)
        try:
            result_for_trace = json.loads(tool_result)
        except json.JSONDecodeError:
            result_for_trace = tool_result
        result_json = self._trace_json(result_for_trace)
        completed_event = {
            **running_event,
            "event": f"tool.{status}",
            "status": status,
            "result_json": result_json,
            "error_type": error_type,
            "error_message": error_message,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
        }
        if self.session_store and trace_persisted:
            try:
                self.session_store.finish_tool_trace(
                    trace_id=trace_id,
                    tool_call_id=tool_call_id,
                    status=status,
                    result_json=result_json,
                    error_type=error_type,
                    error_message=error_message,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                self._emit_trace(
                    {
                        **completed_event,
                        "event": "tool.trace_persistence_failed",
                        "error": str(exc),
                    }
                )
        self._emit_trace(completed_event)
        return tool_result

    @staticmethod
    def _normalize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only API fields needed to replay a tool call; discard private metadata."""
        normalized = []
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            normalized.append(
                {
                    "id": tool_call["id"],
                    "type": "function",
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments") or "{}",
                    },
                }
            )
        return normalized

    def _create_chunk_summary(
        self,
        user_id: str,
        session_id: str,
        end_turn: int,
    ) -> str:
        if not self.session_store or not hasattr(self.client, "summarize"):
            raise RuntimeError("当前模型客户端不支持摘要任务")

        start_turn = end_turn - 4
        recent_turns = self.session_store.load_turns(
            user_id,
            session_id,
            start_turn,
            end_turn,
        )
        if len(recent_turns) != 5:
            raise RuntimeError(
                f"无法为 {start_turn}-{end_turn} 轮生成摘要：该分块不完整"
            )

        summary_input = {
            "five_turns": recent_turns,
            "target_range": f"{start_turn}-{end_turn}",
        }
        summary_messages = [
            {
                "role": "system",
                "content": (
                    "你是会话压缩器。只压缩输入中的这五轮对话，不接收、引用或改写"
                    "其他轮次的摘要。保留用户事实、偏好、待办、承诺、关键工具结果和"
                    "未完成事项。不要编造，不要输出分析或思考过程，只输出本五轮摘要。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(summary_input, ensure_ascii=False),
            },
        ]
        summary = self.client.summarize(summary_messages).strip()
        if not summary:
            raise RuntimeError("模型返回了空摘要")
        self.session_store.save_summary(user_id, session_id, end_turn, summary)
        return summary

    def _ensure_summary(
        self,
        user_id: str,
        session_id: str,
        end_turn: int,
    ) -> str:
        if not self.session_store:
            raise RuntimeError("未配置 SessionStore")
        stored = self.session_store.load_summary(user_id, session_id, end_turn)
        if stored is not None:
            return stored

        key = (user_id, session_id, end_turn)
        with self._summary_lock:
            future = self._summary_futures.get(key)
        if future is not None:
            try:
                return future.result()
            except Exception:
                # Retry synchronously so context size remains bounded.
                pass

        stored = self.session_store.load_summary(user_id, session_id, end_turn)
        if stored is not None:
            return stored
        return self._create_chunk_summary(user_id, session_id, end_turn)

    def _schedule_summary(self, user_id: str, session_id: str, end_turn: int) -> None:
        if not self._summary_executor or end_turn % 5 != 0:
            return
        key = (user_id, session_id, end_turn)
        with self._summary_lock:
            if key in self._summary_futures:
                return
            self._summary_futures[key] = self._summary_executor.submit(
                self._create_chunk_summary,
                user_id,
                session_id,
                end_turn,
            )

    def wait_for_background(self) -> None:
        """Wait for scheduled summaries; useful for graceful shutdown and tests."""
        with self._summary_lock:
            futures = list(self._summary_futures.values())
        for future in futures:
            future.result()

    def close(self) -> None:
        if self._summary_executor:
            self._summary_executor.shutdown(wait=True)

    def _build_context(self, user_id: str, session_id: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
        if not self.session_store:
            return messages

        turn_count = self.session_store.get_turn_count(user_id, session_id)
        if turn_count == 0 and self.session_store.load_messages(user_id, session_id):
            # Trigger lazy migration for databases created by the earlier version.
            self.session_store.load_turns(user_id, session_id)
            turn_count = self.session_store.get_turn_count(user_id, session_id)

        summary_end = 0
        if turn_count >= 15:
            # 15 -> summary(1-5) + full(6-15); 20 -> summary(1-10) + full(11-20).
            summary_end = ((turn_count - 10) // 5) * 5
            summary_parts = []
            for chunk_end in range(5, summary_end + 1, 5):
                chunk_start = chunk_end - 4
                chunk_summary = self._ensure_summary(
                    user_id,
                    session_id,
                    chunk_end,
                )
                summary_parts.append(
                    f"[第 {chunk_start}-{chunk_end} 轮摘要]\n{chunk_summary}"
                )
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"以下是第 1-{summary_end} 轮按顺序拼接的分块摘要；"
                        "各分块互相独立，未被再次压缩：\n"
                        + "\n\n".join(summary_parts)
                    ),
                }
            )

        for turn in self.session_store.load_turns(
            user_id,
            session_id,
            start_turn=summary_end + 1,
        ):
            messages.extend(turn["messages"])
        return messages

    def run(
        self,
        user_input: str,
        user_id: str = "default-user",
        session_id: str = "default-session",
    ) -> str:
        if not user_id.strip() or not session_id.strip():
            raise ValueError("user_id 和 session_id 不能为空")

        messages = self._build_context(user_id, session_id)
        trace_id = uuid.uuid4().hex
        turn_no = (
            self.session_store.get_turn_count(user_id, session_id) + 1
            if self.session_store
            else 1
        )
        user_message = {"role": "user", "content": user_input}
        messages.append(user_message)
        turn_messages: list[dict[str, Any]] = [user_message]
        step = 0

        # The complete agent control flow: model -> tools -> model -> final answer.
        while step < self.max_steps:
            step += 1
            assistant_message = self.client.chat(messages)
            reasoning, visible_content = self._extract_reasoning(assistant_message)
            if reasoning:
                self._emit_reasoning(
                    {
                        "event": "model.reasoning",
                        "trace_id": trace_id,
                        "user_id": user_id,
                        "session_id": session_id,
                        "turn_no": turn_no,
                        "model_step": step,
                        "reasoning": reasoning,
                    }
                )
            tool_calls = self._normalize_tool_calls(
                assistant_message.get("tool_calls") or []
            )

            # Persist visible output only; reasoning exists only in the callback event.
            normalized_message: dict[str, Any] = {
                "role": "assistant",
                "content": visible_content,
            }
            if tool_calls:
                normalized_message["tool_calls"] = tool_calls
            messages.append(normalized_message)
            turn_messages.append(normalized_message)

            if not tool_calls:
                if self.session_store:
                    turn_no = self.session_store.append_turn(
                        user_id,
                        session_id,
                        turn_messages,
                    )
                    self._schedule_summary(user_id, session_id, turn_no)
                return visible_content or ""

            for tool_call in tool_calls:
                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": self._execute_tool(
                        tool_call,
                        trace_id=trace_id,
                        user_id=user_id,
                        session_id=session_id,
                        turn_no=turn_no,
                        model_step=step,
                    ),
                }
                messages.append(tool_message)
                turn_messages.append(tool_message)

        raise RuntimeError(f"Agent 在 {self.max_steps} 轮内没有生成最终回答")


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal tool-calling agent")
    parser.add_argument(
        "--user-id",
        default=os.getenv("AGENT_USER_ID", "user-a"),
        help="用户标识；同一用户的多个 session 仍相互隔离",
    )
    parser.add_argument(
        "--session-id",
        default=os.getenv("AGENT_SESSION_ID", "window-1"),
        help="会话标识；重新使用同一标识即可继续历史会话",
    )
    parser.add_argument(
        "--session-db",
        default=os.getenv("AGENT_SESSION_DB", "data/sessions.db"),
        help="SQLite session 数据库路径",
    )
    args = parser.parse_args()

    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    client = OpenAICompatibleClient(base_url, api_key, model)
    session_store = SessionStore(Path(args.session_db))

    def show_reasoning(event: dict[str, Any]) -> None:
        print(
            f"\n[模型思考 · step {event['model_step']}]\n"
            f"{event['reasoning']}"
        )

    agent = MiniAgent(
        client,
        session_store=session_store,
        reasoning_callback=show_reasoning,
    )

    print(
        f"Mini Agent 已启动（model={model}, user={args.user_id}, "
        f"session={args.session_id}）"
    )
    print(
        "输入 /sessions 查看会话，/trace 查看最近工具调用，"
        "/clear 清空当前会话，exit 退出。"
    )
    try:
        while True:
            try:
                user_input = input("\n你：").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                return
            if user_input.lower() in {"exit", "quit"}:
                print("再见！")
                return
            if user_input == "/sessions":
                sessions = session_store.list_sessions(args.user_id)
                print(json.dumps(sessions, ensure_ascii=False, indent=2))
                continue
            if user_input == "/trace":
                traces = session_store.list_tool_traces(
                    args.user_id,
                    args.session_id,
                )
                print(json.dumps(traces, ensure_ascii=False, indent=2))
                continue
            if user_input == "/clear":
                session_store.clear_session(args.user_id, args.session_id)
                print("当前 session 已清空。")
                continue
            if not user_input:
                continue
            try:
                answer = agent.run(user_input, args.user_id, args.session_id)
                print(f"Agent：{answer}")
            except Exception as exc:
                print(f"错误：{exc}", file=sys.stderr)
    finally:
        agent.close()


if __name__ == "__main__":
    main()
