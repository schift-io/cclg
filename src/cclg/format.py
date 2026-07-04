from __future__ import annotations

CCLG_FORMAT_VERSION = "0.1"
CCLG_FORMAT_ID = "cclg.format.v0.1"
CCLG_NAMESPACE = "https://github.com/schift-io/cclg/ns/v0"

STORE_SCHEMA = "cclg.store.v0.1"
MEMORY_NODE_SCHEMA = "cclg.memory_node.v0.1"
MEMORY_PATCH_SCHEMA = "cclg.memory_patch.v0.1"
MEMORY_EDGE_SCHEMA = "cclg.edge.v0.1"
ACTIVE_MEMORY_PACK_SCHEMA = "cclg.active_memory_pack.v0.1"
CODE_GRAPH_SCHEMA = "cclg.code_graph.v0.1"
SESSION_SCHEMA = "cclg.session.v0.1"
HOOK_OUTPUT_SCHEMA = "cclg.hook_output.v0.1"

# .cclg portable container (docs/CCLG_CONTAINER.md). Distinct from CCLG_FORMAT_ID:
# the format id versions the *record* schemas; the container id versions the
# *artifact* (magic + header + section framing) that carries those records.
CCLG_CONTAINER_MAGIC = "CCLG"
CCLG_CONTAINER_VERSION = "0.1"
CCLG_CONTAINER_ID = f"cclg.container.v{CCLG_CONTAINER_VERSION}"


def toml_string(value: object) -> str:
    import json

    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def source_label(source: object) -> str:
    if isinstance(source, dict):
        label = source.get("label") or source.get("source")
        if label:
            return str(label)
        session_ids = source.get("session_ids")
        if isinstance(session_ids, list) and session_ids:
            return f"session:{session_ids[0]}"
    if source is None:
        return "unknown"
    return str(source)


def render_active_pack_toml(pack: dict) -> str:
    lines = [
        f'schema_version = {toml_string(pack["schema_version"])}',
        f'query = {toml_string(pack["query"])}',
        f'generated_at = {toml_string(pack["generated_at"])}',
        "",
    ]

    for node in pack.get("memory_nodes", []):
        source = source_label(node.get("source") or node.get("provenance"))
        lines.extend(
            [
                "[[memory]]",
                f'id = {toml_string(node.get("id"))}',
                f'type = {toml_string(node.get("type"))}',
                f'status = {toml_string(node.get("status"))}',
                f'source = {toml_string(source)}',
                f'content = {toml_string(node.get("content"))}',
                "",
            ]
        )

    for node in pack.get("suppressed_nodes", []):
        lines.extend(
            [
                "[[suppressed]]",
                f'id = {toml_string(node.get("id"))}',
                f'status = {toml_string(node.get("status"))}',
                f'content = {toml_string(node.get("content"))}',
                "",
            ]
        )

    budget = pack.get("budget", {})
    lines.extend(
        [
            "[budget]",
            f'max_nodes = {int(budget.get("max_nodes", 0))}',
            f'max_chars = {int(budget.get("max_chars", 0))}',
            f'used_chars = {int(budget.get("used_chars", 0))}',
        ]
    )
    return "\n".join(lines) + "\n"
