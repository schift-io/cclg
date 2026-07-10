from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .codegraph import build_code_graph, render_code_pack, save_code_graph
from .format import render_active_pack_toml, source_label
from .pack import compile_pack
from .session import append_session_event, normalize_session_id
from .store import CCLGStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cclg-hook", description="CCLG hook adapter for Codex/Claude-style lifecycle events")
    parser.add_argument("event", choices=["user-prompt", "post-tool", "pre-compact", "post-compact", "session-start", "stop"])
    parser.add_argument("--root", help="CCLG store root")
    parser.add_argument("--code-root", default=os.getcwd(), help="Repository root to code-index for user prompts")
    parser.add_argument("--max-chars", type=int, default=6000)
    parser.add_argument("--include-codegraph", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # stdin read is inside the guard too: a non-UTF-8 stdin would otherwise
        # raise UnicodeDecodeError here and escape the hook.
        payload = read_stdin_json()
        store = CCLGStore(args.root)
        if args.event in {"user-prompt", "session-start"}:
            hook_event_name = "SessionStart" if args.event == "session-start" else "UserPromptSubmit"
            output = user_prompt_context(store, payload, code_root=Path(args.code_root), max_chars=args.max_chars, include_codegraph=args.include_codegraph, hook_event_name=hook_event_name)
        else:
            output = ingest_event(store, args.event, payload)
    except Exception as exc:  # noqa: BLE001 - a memory hook must never break the host session
        # Degrade gracefully: emit a valid continue:true payload and exit 0 so
        # the host (Codex/Claude) proceeds even if CCLG hit an internal error.
        output = _fallback_output(args.event, exc)
    _emit(output)
    return 0


def _emit(output: dict[str, Any]) -> None:
    """Emit the hook payload without ever raising.

    This is the one operation that runs on EVERY invocation (success and fallback
    alike), so it must not crash the host. Two hazards: (1) ``print`` goes through
    stdout's text codec, so a non-UTF-8 locale/PYTHONIOENCODING raises
    UnicodeEncodeError on the non-ASCII content ``ensure_ascii=False`` produces
    (this is a Korean-heavy store); we bypass the text layer and write UTF-8 bytes
    directly. (2) A closed read end raises BrokenPipeError/OSError; we swallow it.
    """
    text = json.dumps(output, ensure_ascii=False)
    try:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            buffer.write(text.encode("utf-8", "replace") + b"\n")
            buffer.flush()
        else:  # stdout replaced (e.g. a StringIO in tests) — no binary buffer
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except (BrokenPipeError, OSError):
        pass
    except Exception:  # noqa: BLE001 - last resort: an emit error must not break the host
        pass


def _fallback_output(event: str, exc: Exception) -> dict[str, Any]:
    _audit_error(event, exc)
    if event in {"user-prompt", "session-start"}:
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart" if event == "session-start" else "UserPromptSubmit",
                "additionalContext": "",
            },
        }
    return {}


def _audit_error(event: str, exc: Exception) -> None:
    """Record hook errors in the store's audit log instead of stdout.

    stdout is reserved for the host's hook-output schema: Claude Code and
    Codex plugin_hooks strictly validate the JSON and reject unknown
    top-level keys ("hook returned invalid ... JSON output"), so error
    details must never ride along in the emitted payload.
    """
    try:
        CCLGStore(None).append_audit({"kind": "hook_error", "event": event, "error": f"{type(exc).__name__}: {exc}"})
    except Exception:  # noqa: BLE001 - auditing must never break the host
        pass


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw_stdin": raw}
    if not isinstance(parsed, dict):
        # Valid JSON but not an object (list/str/number): wrap it so downstream
        # .get() calls don't blow up and the event is still recorded.
        return {"raw_stdin": raw, "parsed": parsed}
    return parsed


def user_prompt_context(
    store: CCLGStore,
    payload: dict[str, Any],
    *,
    code_root: Path,
    max_chars: int,
    include_codegraph: bool,
    hook_event_name: str = "UserPromptSubmit",
) -> dict[str, Any]:
    prompt = extract_prompt(payload)
    session_id = extract_session_id(payload)
    normalized_session_id = normalize_session_id(session_id)
    append_session_event(store, session_id=normalized_session_id, event="user_prompt", payload={"prompt": prompt, "raw": payload})
    pack = compile_pack(store, prompt, max_chars=max_chars, session_id=normalized_session_id)
    context = render_active_pack_toml(pack.to_dict())
    if include_codegraph and code_root.exists():
        graph = build_code_graph(code_root)
        graph_path = store.active_dir / "codegraphs" / f"{code_root.name or 'repo'}.json"
        save_code_graph(graph, graph_path)
        context += "\n" + render_code_pack(graph, prompt, limit=12)
    # Only schema-listed keys may appear on stdout (strict host validation);
    # CCLG telemetry lives in the session/audit store, not the hook payload.
    return {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": context,
        },
    }


def ingest_event(store: CCLGStore, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    session_id = extract_session_id(payload)
    append_session_event(store, session_id=session_id, event=event, payload=payload)
    # Empty object = "no action" for every hook event; anything beyond the
    # host's schema (counters, session ids) gets rejected as invalid output.
    return {}


def markdown_pack(pack: dict[str, Any]) -> str:
    lines = [
        "# CCLG Active Memory",
        "",
        f"query: {pack['query']}",
        f"generated_at: {pack['generated_at']}",
        "",
    ]
    if pack["memory_nodes"]:
        lines.append("## Active")
        for node in pack["memory_nodes"]:
            lines.append(f"- `{node['id']}` {node['content']} (source: {source_label(node.get('source') or node.get('provenance'))})")
    if pack["suppressed_nodes"]:
        lines.extend(["", "## Suppressed, do not inject as active"])
        for node in pack["suppressed_nodes"][:8]:
            lines.append(f"- `{node['id']}` {node['status']}: {node['content']}")
    return "\n".join(lines) + "\n"


def extract_prompt(payload: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "message", "input"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            content = last.get("content")
            if isinstance(content, str):
                return content
    return json.dumps(payload, ensure_ascii=False)[:1000]


def extract_session_id(payload: dict[str, Any]) -> str | None:
    for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
