"""dsh session log → trajectory.json converter.

dsh persists sessions as an event-stream jsonl: each line is
``{"type": <kind>, "data": {...}}``. Relevant events:

  user/message       data.content = [{type:"text", text}]
  assistant/message  data.message.content = [{type:"text",text} | {type:"tool-call",id,name,arguments}]
  tool/result        data.message.content = [{type:"tool-result", content:[{type:"text",text}]}]

Skipped: assistant/chunk (streaming fragments; assistant/message already
carries the full content), step/turn/session/request markers, inbox splices.

The output schema follows the consumer contract in
``library/_shared/harbor/evidence.py`` (``_trajectory_details``):

  step.source       "user" | "agent" | "tool"
  step.message      plain string
  step.tool_calls   [{"name": str, "arguments": str|dict}]
  step.observation  {"results": [{"content": str}]}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "dsh-trajectory-v1"
_OBS_LIMIT = 8000


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def _text_parts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        item.get("text")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    return "\n".join(part for part in parts if part)


def _tool_call_parts(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool-call":
            calls.append(
                {
                    "name": str(item.get("name") or "unknown"),
                    "arguments": item.get("arguments") or "",
                }
            )
    return calls


def _tool_result_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool-result":
            texts.append(_text_parts(item.get("content")))
    return "\n".join(text for text in texts if text)


def _extract(record: dict[str, Any]) -> dict[str, Any] | None:
    kind = record.get("type")
    data = record.get("data")
    if not isinstance(data, dict):
        return None

    if kind == "user/message":
        text = _text_parts(data.get("content"))
        if not text and isinstance(data.get("message"), dict):
            text = _text_parts(data["message"].get("content"))
        return {"source": "user", "message": text} if text else None

    if kind == "assistant/message":
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else data.get("content")
        step: dict[str, Any] = {"source": "agent"}
        text = _text_parts(content)
        if text:
            step["message"] = text
        calls = _tool_call_parts(content)
        if calls:
            step["tool_calls"] = calls
        return step if (text or calls) else None

    if kind == "tool/result":
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else data.get("content")
        text = _tool_result_text(content)
        if len(text) > _OBS_LIMIT:
            text = text[:_OBS_LIMIT] + f"\n...[truncated {len(text) - _OBS_LIMIT} chars]..."
        if text:
            return {"source": "tool", "observation": {"results": [{"content": text}]}}
        return None

    return None


def convert_session(session_root: Path, out_path: Path) -> None:
    steps: list[dict[str, Any]] = []
    skipped = 0
    if session_root.is_dir():
        for path in sorted(session_root.rglob("*.jsonl")):
            for record in _read_jsonl(path):
                step = _extract(record)
                if step is None:
                    skipped += 1
                    continue
                steps.append(step)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "agent": "dsh",
        "steps": steps,
        "skipped_records": skipped,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
