"""dsh session log → trajectory.json converter.

dsh persists sessions as an event-stream jsonl: each line is
``{"type": <kind>, "data": {...}}``. Relevant events:

  user/message       data.content = [{type:"text", text}]
  assistant/message  data.message.content = [{type:"text",text} | {type:"tool-call",id,name,arguments}]
  tool/result        data.message.content = [{type:"tool-result", content:[{type:"text",text}]}]

Skipped: assistant/chunk streaming text/tool fragments (assistant/message already
carries the full content), step/turn/session/request markers, inbox splices.

Host-owned usage (parsed only when present — never invented):

  assistant/message  data.usage = {inputTokens, outputTokens, totalTokens?,
                                   cacheReadTokens?, cacheWriteTokens?, reasoningTokens?}
  assistant/chunk    data.chunk = {type:"usage", usage: {...}}  (fallback if no
                                   message-level usage for that turn)

Mapped into trajectory.json as:

  step.usage         per-agent-step metering when the log provides it
  usage.schema       "dsh-usage-v1"
  usage.source       "dsh-session-log"
  usage.totals       session sums over observed fields (null if none observed)
  usage.per_step     parallel list of per-agent-step usage objects (or null)

The step schema follows the consumer contract in
``library/_shared/harbor/evidence.py`` (``_trajectory_details``):

  step.source       "user" | "agent" | "tool"
  step.message      plain string
  step.tool_calls   [{"name": str, "arguments": str|dict}]
  step.observation  {"results": [{"content": str}]}
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "dsh-trajectory-v1"
_OBS_LIMIT = 8000

_USAGE_FIELD_MAP = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("totalTokens", "total_tokens"),
    ("cacheReadTokens", "cache_read_tokens"),
    ("cacheWriteTokens", "cache_write_tokens"),
    ("reasoningTokens", "reasoning_tokens"),
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("total_tokens", "total_tokens"),
    ("cache_read_tokens", "cache_read_tokens"),
    ("cache_write_tokens", "cache_write_tokens"),
    ("reasoning_tokens", "reasoning_tokens"),
)


def _read_text_lines(path: Path) -> str:
    """Read a session log, decompressing ``*.jsonl.zst`` when needed."""
    if path.suffix == ".zst" or path.name.endswith(".jsonl.zst"):
        try:
            import zstandard  # type: ignore[import-not-found]

            return zstandard.ZstdDecompressor().decompress(path.read_bytes()).decode("utf-8", errors="replace")
        except Exception:
            try:
                decoded = subprocess.run(
                    ["zstd", "-d", "-c", str(path)],
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                return decoded.stdout.decode("utf-8", errors="replace")
            except (FileNotFoundError, subprocess.SubprocessError, OSError):
                return ""
    return path.read_text(errors="replace")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in _read_text_lines(path).splitlines():
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


def _iter_session_logs(session_root: Path) -> list[Path]:
    """Collect session event logs under an isolated dsh_home (or legacy root).

    Current SDK layouts store JSONL under ``<dsh_home>/sessions/`` (optionally
    ``*.jsonl.zst``). Older layouts wrote ``*.jsonl`` directly under the root.
    """
    found: list[Path] = []
    for pattern in ("*.jsonl", "*.jsonl.zst"):
        found.extend(session_root.rglob(pattern))
    return sorted({path for path in found if path.is_file()})


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


def _coerce_token_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _parse_usage(blob: object) -> dict[str, int] | None:
    """Parse a provider usage object into the host-owned snake_case schema.

    Returns ``None`` when no recognized token fields are present (do not invent).
    """
    if not isinstance(blob, dict):
        return None
    out: dict[str, int] = {}
    for src, dest in _USAGE_FIELD_MAP:
        if src not in blob:
            continue
        count = _coerce_token_count(blob.get(src))
        if count is not None:
            out[dest] = count
    return out or None


def _usage_from_record(record: dict[str, Any]) -> dict[str, int] | None:
    kind = record.get("type")
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    if kind == "assistant/message":
        parsed = _parse_usage(data.get("usage"))
        if parsed is not None:
            return parsed
        message = data.get("message")
        if isinstance(message, dict):
            return _parse_usage(message.get("usage"))
        return None
    if kind == "assistant/chunk":
        chunk = data.get("chunk")
        if isinstance(chunk, dict) and chunk.get("type") == "usage":
            return _parse_usage(chunk.get("usage"))
    return None


def _add_usage(totals: dict[str, int], piece: dict[str, int]) -> None:
    for key, value in piece.items():
        totals[key] = totals.get(key, 0) + value


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
        usage = _usage_from_record(record)
        if usage is not None:
            step["usage"] = usage
        return step if (text or calls or usage) else None

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
    totals: dict[str, int] = {}
    per_step: list[dict[str, int] | None] = []
    pending_chunk_usage: dict[str, int] | None = None
    if session_root.is_dir():
        for path in _iter_session_logs(session_root):
            for record in _read_jsonl(path):
                kind = record.get("type")
                if kind == "assistant/chunk":
                    chunk_usage = _usage_from_record(record)
                    if chunk_usage is not None:
                        pending_chunk_usage = chunk_usage
                    skipped += 1
                    continue
                step = _extract(record)
                if step is None:
                    skipped += 1
                    continue
                if step.get("source") == "agent":
                    usage = step.get("usage")
                    if not isinstance(usage, dict) and pending_chunk_usage is not None:
                        usage = pending_chunk_usage
                        step["usage"] = usage
                    pending_chunk_usage = None
                    if isinstance(usage, dict):
                        _add_usage(totals, usage)
                        per_step.append(usage)
                    else:
                        per_step.append(None)
                steps.append(step)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "agent": "dsh",
        "steps": steps,
        "skipped_records": skipped,
        "usage": {
            "schema": "dsh-usage-v1",
            "source": "dsh-session-log",
            "totals": totals or None,
            "per_step": per_step if per_step else None,
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
