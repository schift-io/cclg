#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.request import urlopen


REVISION = "b4304866ec8d9784fb77bebb1ce4660806abcded"
BASE_URL = f"https://huggingface.co/datasets/Ray368/GateMem/resolve/{REVISION}/gatemem/data/office"
EPISODES_URL = f"{BASE_URL}/episodes.jsonl"
CHECKPOINTS_URL = f"{BASE_URL}/checkpoints.jsonl"

EXPECTED_EPISODES = 17
EXPECTED_CHECKPOINTS = 547
HIDDEN_FIELDS = {"query_type", "expected_action", "judge_spec", "leak_targets", "attack_type"}
AGENT_CHECKPOINT_FIELDS = ("checkpoint_id", "episode_id", "as_of_turn_id", "asker", "query_text")


def load_jsonl_url(url: str) -> list[dict[str, Any]]:
    with urlopen(url, timeout=60) as response:
        body = response.read().decode("utf-8")
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(body.splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{url}:{line_no} is not a JSON object")
        rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_office_rows(episodes: list[dict[str, Any]], checkpoints: list[dict[str, Any]]) -> None:
    if len(episodes) != EXPECTED_EPISODES:
        raise ValueError(f"expected {EXPECTED_EPISODES} office episodes, got {len(episodes)}")
    if len(checkpoints) != EXPECTED_CHECKPOINTS:
        raise ValueError(f"expected {EXPECTED_CHECKPOINTS} office checkpoints, got {len(checkpoints)}")

    episode_turn_ids: dict[str, set[str]] = {}
    for episode in episodes:
        if episode.get("domain") != "office":
            raise ValueError(f"non-office episode found: {episode.get('episode_id')}")
        episode_id = str(episode.get("episode_id") or "")
        turns = episode.get("turns")
        if not episode_id or not isinstance(turns, list) or not turns:
            raise ValueError(f"episode has no turns: {episode_id}")
        episode_turn_ids[episode_id] = {str(turn.get("turn_id") or "") for turn in turns}
        for key in ("entities", "turns"):
            if key not in episode:
                raise ValueError(f"episode {episode_id} missing {key}")

    for checkpoint in checkpoints:
        episode_id = str(checkpoint.get("episode_id") or "")
        turn_id = str(checkpoint.get("as_of_turn_id") or "")
        if episode_id not in episode_turn_ids:
            raise ValueError(f"checkpoint points to unknown episode: {checkpoint.get('checkpoint_id')}")
        if turn_id not in episode_turn_ids[episode_id]:
            raise ValueError(f"checkpoint points to unknown turn: {checkpoint.get('checkpoint_id')} -> {turn_id}")
        for key in AGENT_CHECKPOINT_FIELDS:
            if key not in checkpoint:
                raise ValueError(f"checkpoint {checkpoint.get('checkpoint_id')} missing {key}")


def agent_checkpoint_view(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {key: checkpoint.get(key) for key in AGENT_CHECKPOINT_FIELDS}


def empty_prediction(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint_id": checkpoint["checkpoint_id"],
        "action": "no_memory",
        "answer": "",
        "answer_structured": {},
        "used_record_ids": [],
    }


def build_manifest(episodes: list[dict[str, Any]], checkpoints: list[dict[str, Any]]) -> dict[str, Any]:
    query_types = Counter(str(row.get("query_type") or "unknown") for row in checkpoints)
    expected_actions = Counter(str(row.get("expected_action") or "unknown") for row in checkpoints)
    checkpoint_agent_keys = set(agent_checkpoint_view(checkpoints[0]).keys()) if checkpoints else set()
    hidden_overlap = sorted(checkpoint_agent_keys & HIDDEN_FIELDS)
    return {
        "schema_version": "cclg.gatemem_office_probe.v0.1",
        "source": {
            "dataset": "Ray368/GateMem",
            "revision": REVISION,
            "episodes_url": EPISODES_URL,
            "checkpoints_url": CHECKPOINTS_URL,
            "license": "cc-by-4.0",
        },
        "office": {
            "episodes": len(episodes),
            "checkpoints": len(checkpoints),
            "query_type_counts": dict(sorted(query_types.items())),
            "expected_action_counts": dict(sorted(expected_actions.items())),
        },
        "agent_input_contract": {
            "checkpoint_fields": list(AGENT_CHECKPOINT_FIELDS),
            "hidden_fields_excluded": sorted(HIDDEN_FIELDS),
            "hidden_field_overlap": hidden_overlap,
        },
        "modes": {
            "cclg_local": {
                "goal": "Validate CCLG schema, patches, effective view, and ActiveMemoryPack governance.",
                "network": "dataset download only",
                "schift_dependency": False,
            },
            "schift_hosted": {
                "goal": "Validate Schift auth, redaction, bucket isolation, hosted search/fetch, and MCP scope.",
                "network": "required",
                "schift_dependency": True,
            },
            "combined": {
                "goal": "Validate CCLG-shaped payloads transported through Schift Memory end to end.",
                "network": "required",
                "schift_dependency": True,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare and validate the GateMem office-only slice.")
    parser.add_argument("--out-dir", default="tmp/gatemem-office", help="Directory for downloaded office data and probe outputs.")
    parser.add_argument("--write-empty-predictions", action="store_true", help="Write no_memory predictions for scorer plumbing tests.")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    episodes = load_jsonl_url(EPISODES_URL)
    checkpoints = load_jsonl_url(CHECKPOINTS_URL)
    validate_office_rows(episodes, checkpoints)

    write_jsonl(out_dir / "office_episodes.jsonl", episodes)
    write_jsonl(out_dir / "office_checkpoints.jsonl", checkpoints)
    write_jsonl(out_dir / "agent_checkpoints.jsonl", [agent_checkpoint_view(row) for row in checkpoints])
    manifest = build_manifest(episodes, checkpoints)
    write_json(out_dir / "manifest.json", manifest)

    if args.write_empty_predictions:
        write_jsonl(out_dir / "predictions.empty.jsonl", [empty_prediction(row) for row in checkpoints])

    print(json.dumps({"ok": True, "out_dir": str(out_dir), **manifest["office"]}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
