from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from .format import STORE_SCHEMA
from .models import MemoryEdge, MemoryNode, MemoryPatch, now_iso


DEFAULT_HOME = Path(os.environ.get("CCLG_HOME", "~/.cclg")).expanduser()


def atomic_write_text(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` atomically (temp file + os.replace).

    A concurrent writer can never observe a partial payload or leave a stale tail
    behind a shorter one: readers either see the old file or the fully-written new
    one. The temp name carries pid + a random suffix so two writers (even threads
    in one process) never collide on it, and is hidden + suffixed ``.tmp`` so it is
    never picked up by the store's ``*.json`` globs. A failed write cleans up its
    own temp file; a hard crash may leave one behind, which is harmless.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


class CCLGStore:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root else DEFAULT_HOME
        self.raw_dir = self.root / "raw"
        self.nodes_dir = self.root / "nodes"
        self.patches_dir = self.root / "patches"
        self.edges_dir = self.root / "edges"
        self.sessions_dir = self.root / "sessions"
        self.active_dir = self.root / "active"
        self.audit_dir = self.root / "audit"

    def init(self) -> None:
        for path in [
            self.raw_dir,
            self.nodes_dir,
            self.patches_dir,
            self.edges_dir,
            self.sessions_dir,
            self.active_dir,
            self.audit_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)
        config_path = self.root / "config.json"
        if not config_path.exists():
            # Atomic so two processes racing on first-time init cannot interleave
            # a torn config; identical content means last-writer-wins is harmless.
            atomic_write_text(
                config_path,
                json.dumps(
                    {
                        "schema_version": STORE_SCHEMA,
                        "created_or_touched_at": now_iso(),
                        "root": str(self.root),
                        "dense": {"enabled": False, "model": None, "device": None},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )

    def write_node(self, node: MemoryNode) -> None:
        self.init()
        self._write_json(self.nodes_dir / f"{node.id}.json", node.to_dict())

    def read_node(self, node_id: str) -> MemoryNode:
        return MemoryNode.from_dict(self._read_json(self.nodes_dir / f"{node_id}.json"))

    def iter_nodes(self) -> Iterable[MemoryNode]:
        self.init()
        for path in sorted(self.nodes_dir.glob("*.json")):
            try:
                yield MemoryNode.from_dict(self._read_json(path))
            except Exception:  # noqa: BLE001 - skip an unrecoverable file rather than break the whole pack
                continue

    def write_patch(self, patch: MemoryPatch) -> None:
        self.init()
        self._write_json(self.patches_dir / f"{patch.id}.json", patch.to_dict())
        self.append_audit(
            {
                "event": "patch_written",
                "patch_id": patch.id,
                "operation": patch.operation,
                "target_ids": patch.target_ids,
                "created_at": patch.created_at,
            }
        )

    def iter_patches(self) -> Iterable[MemoryPatch]:
        self.init()
        for path in sorted(self.patches_dir.glob("*.json")):
            try:
                yield MemoryPatch.from_dict(self._read_json(path))
            except Exception:  # noqa: BLE001 - skip an unrecoverable file rather than break the whole pack
                continue

    def write_edge(self, edge: MemoryEdge) -> None:
        self.init()
        self._write_json(self.edges_dir / f"{edge.id}.json", edge.to_dict())

    def iter_edges(self) -> Iterable[MemoryEdge]:
        self.init()
        for path in sorted(self.edges_dir.glob("*.json")):
            try:
                yield MemoryEdge.from_dict(self._read_json(path))
            except Exception:  # noqa: BLE001 - skip an unrecoverable file rather than break the whole pack
                continue

    def append_raw(self, name: str, text: str) -> Path:
        self.init()
        safe_name = "".join(c if c.isalnum() or c in "._-" else "-" for c in name)
        stem = Path(safe_name).stem or "raw"
        suffix = Path(safe_name).suffix or ".json"
        # Always mint a unique name (timestamp + random) so two concurrent writers
        # can never collide on the same path and clobber each other's payload — the
        # previous exists() guard raced (both saw "not exists" and overwrote).
        path = self.raw_dir / f"{stem}-{now_iso().replace(':', '')}-{uuid4().hex[:8]}{suffix}"
        atomic_write_text(path, text)
        self.append_audit({"event": "raw_written", "path": str(path), "created_at": now_iso()})
        return path

    def append_audit(self, event: dict) -> None:
        self.init()
        event.setdefault("created_at", now_iso())
        path = self.audit_dir / "memory_audit.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    @staticmethod
    def _read_json(path: Path) -> dict:
        text = path.read_text(encoding="utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Defensive recovery for a partial/legacy write that left a stale tail
            # behind a complete JSON object: take the first valid object and
            # rewrite the file clean. Re-raises if nothing valid can be recovered.
            obj, _ = json.JSONDecoder().raw_decode(text)
            atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
            return obj

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
