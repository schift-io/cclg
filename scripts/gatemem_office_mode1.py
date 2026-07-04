#!/usr/bin/env python3
"""GateMem Office Mode 1 (CCLG local) runner -- see docs/GATEMEM_OFFICE.md.

Replays each of the 17 GateMem office episodes turn-by-turn into a temporary
CCLGStore (one per episode), and for every one of the 547 checkpoints, packs
the store through the `.cclg` container round trip (pack_from_store ->
load_container per docs/GATEMEM_OFFICE.md's P4 Mode 1 definition), computes a
policy-aware ActiveMemoryPack via cclg.gatemem_pack, and writes a
predictions.jsonl row shaped like the upstream GateMem harness's own
run_eval.py output.

No network access, no LLM/paid API calls, no randomness: `deterministic_ids`
makes two runs over the same input byte-identical. See
cclg.gatemem_adapter's module docstring for the full boundary and documented
heuristic limitations.

Usage:
    PYTHONPATH=src python3 scripts/gatemem_office_mode1.py \\
        --data-dir tmp/gatemem-data --out tmp/gatemem-out/predictions.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cclg.container import load_container, pack_from_store  # noqa: E402
from cclg.gatemem_adapter import (  # noqa: E402
    EpisodePolicyState,
    build_project_directory,
    ingest_turn,
)
from cclg.gatemem_pack import (  # noqa: E402
    agent_view_of,
    build_policy_pack,
    build_prediction_row,
    check_pack_leaks,
    deterministic_ids,
)
from cclg.session import start_session  # noqa: E402
from cclg.store import CCLGStore  # noqa: E402


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _turn_sort_key(turn: dict[str, Any]) -> int:
    digits = re.sub(r"\D", "", str(turn.get("turn_id") or "0"))
    return int(digits or 0)


def _post_hoc_leak_target_hit(checkpoint: dict[str, Any], exposed_text: str) -> bool:
    """Honest, post-hoc-only cross-check against the hidden ``leak_targets``
    field: did our exposed context literally contain one of the real
    protected/deleted values? Used purely for reporting -- computed *after*
    the deterministic decision was already made blind to this field (see
    cclg.gatemem_pack.AgentCheckpointView), mirroring how the released
    rule-based scorer itself uses leak_targets only to grade, never to steer
    the agent (docs/GATEMEM_OFFICE.md's Hidden Field Rule)."""
    targets = checkpoint.get("leak_targets") or []
    low = exposed_text.lower()
    return any(str(target).lower() in low for target in targets if target)


def run_episode(episode: dict[str, Any], checkpoints: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    turns = sorted(episode.get("turns") or [], key=_turn_sort_key)
    checkpoints_by_turn: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for checkpoint in checkpoints:
        checkpoints_by_turn[str(checkpoint.get("as_of_turn_id"))].append(checkpoint)

    directory = build_project_directory(episode)
    policy_state = EpisodePolicyState(directory=directory)

    episode_id = str(episode.get("episode_id"))
    predictions: list[dict[str, Any]] = []
    violations: list[str] = []
    audit_rows: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="cclg_gatemem_") as tmp_dir:
        store = CCLGStore(Path(tmp_dir))
        store.init()
        start_session(store, session_id=episode_id, agent="gatemem-office-mode1", workspace=episode_id, project="gatemem-office")

        for turn in turns:
            ingest_turn(store, episode_id=episode_id, turn=turn, directory=directory, policy_state=policy_state)

            turn_id = str(turn.get("turn_id"))
            pending = checkpoints_by_turn.get(turn_id)
            if not pending:
                continue

            # Mandatory container round trip (docs/GATEMEM_OFFICE.md P4 Mode 1):
            # pack the mutable store, then load it back before computing the
            # effective view/pack for every checkpoint anchored at this turn.
            container_text = pack_from_store(store)
            bundle = load_container(container_text)

            for checkpoint in pending:
                view = agent_view_of(checkpoint)
                pack = build_policy_pack(bundle, view, policy_state)
                predictions.append(build_prediction_row(checkpoint, view, pack))
                violations.extend(check_pack_leaks(bundle, view, pack, policy_state))
                audit_rows.append(
                    {
                        "checkpoint_id": view.checkpoint_id,
                        "query_type": checkpoint.get("query_type"),
                        "action": pack.action,
                        "post_hoc_leak_target_hit": _post_hoc_leak_target_hit(checkpoint, pack.exposed_text),
                    }
                )

    return predictions, violations, audit_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="tmp/gatemem-data", help="Directory with office_episodes.jsonl / office_checkpoints.jsonl (see scripts/gatemem_office_probe.py).")
    parser.add_argument("--out", default="tmp/gatemem-out/predictions.jsonl", help="Output predictions.jsonl path.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    episodes = load_jsonl(data_dir / "office_episodes.jsonl")
    checkpoints = load_jsonl(data_dir / "office_checkpoints.jsonl")

    checkpoints_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for checkpoint in checkpoints:
        checkpoints_by_episode[str(checkpoint.get("episode_id"))].append(checkpoint)

    all_predictions: list[dict[str, Any]] = []
    all_violations: list[str] = []
    all_audit: list[dict[str, Any]] = []

    with deterministic_ids():
        for episode in episodes:
            episode_id = str(episode.get("episode_id"))
            predictions, violations, audit_rows = run_episode(episode, checkpoints_by_episode.get(episode_id, []))
            all_predictions.extend(predictions)
            all_violations.extend(violations)
            all_audit.extend(audit_rows)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for row in all_predictions:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    action_counts = Counter(row["output"]["action"] for row in all_predictions)
    query_type_counts = Counter(str(row.get("query_type") or "unknown") for row in all_audit)
    post_hoc_hits = sum(1 for row in all_audit if row["post_hoc_leak_target_hit"])

    summary = {
        "ok": len(all_violations) == 0,
        "predictions": len(all_predictions),
        "expected_checkpoints": len(checkpoints),
        "out_path": str(out_path),
        "action_counts": dict(sorted(action_counts.items())),
        "query_type_counts": dict(sorted(query_type_counts.items())),
        "internal_leak_violations": len(all_violations),
        "post_hoc_leak_target_hits": post_hoc_hits,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))

    if all_violations:
        print("LEAK VIOLATIONS DETECTED (failing loudly per docs/GATEMEM_OFFICE.md):", file=sys.stderr)
        for violation in all_violations[:50]:
            print(f"  - {violation}", file=sys.stderr)
        if len(all_violations) > 50:
            print(f"  ... and {len(all_violations) - 50} more", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
