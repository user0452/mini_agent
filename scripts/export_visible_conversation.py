"""Export only visible user/assistant messages from a Codex rollout JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"input_text", "output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


def quote_markdown(text: str) -> str:
    return "\n".join(
        ">" if not line.rstrip() else f"> {line.rstrip()}"
        for line in text.splitlines()
    )


def strip_platform_context(text: str) -> str:
    """Remove app-injected plugin/AGENTS/environment envelopes from user messages."""
    stripped = text.lstrip()
    if stripped.startswith(("<recommended_plugins>", "<environment_context>")):
        marker = "</environment_context>"
        if marker not in stripped:
            return ""
        return stripped.split(marker, 1)[1].strip()
    return text.strip()


def export_conversation(source: Path, destination: Path) -> int:
    messages: list[dict[str, str]] = []
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # The live rollout can end with a partially written line.
                continue
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload") or {}
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = extract_text(payload.get("content"))
            if role == "user":
                text = strip_platform_context(text)
            if not text:
                continue
            messages.append(
                {
                    "role": role,
                    "phase": payload.get("phase") or "message",
                    "timestamp": record.get("timestamp") or "",
                    "text": text,
                }
            )

    lines = [
        "# mini_agent 完整开发对话",
        "",
        "本文件由 `scripts/export_visible_conversation.py` 从本次 Codex rollout 机械导出。",
        "仅包含用户与助手可见消息；不包含 system/developer 指令、内部 reasoning、",
        "工具调用、工具输出或本地密钥。当前正在执行的最后一轮只包含导出时已经产生",
        "的可见内容。",
        "",
    ]
    for index, message in enumerate(messages, start=1):
        role = "用户" if message["role"] == "user" else "助手"
        phase = "" if message["role"] == "user" else f" / {message['phase']}"
        lines.extend(
            [
                f"## {index}. {role}{phase}",
                "",
                f"时间：`{message['timestamp']}`",
                "",
                quote_markdown(message["text"]),
                "",
            ]
        )

    destination.write_text("\n".join(lines), encoding="utf-8")
    return len(messages)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    count = export_conversation(args.source.resolve(), args.destination.resolve())
    print(f"exported {count} visible messages to {args.destination}")


if __name__ == "__main__":
    main()
