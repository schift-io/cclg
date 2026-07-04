from __future__ import annotations

import ast
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .format import CODE_GRAPH_SCHEMA
from .models import now_iso
from .retrieval import tokenize


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".turbo",
    "coverage",
}

CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".swift",
    ".sh",
    ".bash",
    ".zsh",
    ".rb",
    ".php",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
}

IMPORT_RE = re.compile(r"^\s*(?:import\s+(.+?)\s+from\s+['\"](.+?)['\"]|import\s+['\"](.+?)['\"]|const\s+.+?=\s+require\(['\"](.+?)['\"]\))", re.MULTILINE)
JS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
    re.MULTILINE,
)


@dataclass(slots=True)
class CodeSymbol:
    name: str
    kind: str
    path: str
    line: int
    signature: str = ""


@dataclass(slots=True)
class CodeEdge:
    source: str
    target: str
    kind: str
    detail: str = ""


@dataclass(slots=True)
class CodeGraph:
    schema_version: str
    root: str
    generated_at: str
    git: dict[str, Any]
    files: list[dict[str, Any]]
    symbols: list[CodeSymbol]
    edges: list[CodeEdge]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["symbols"] = [asdict(symbol) for symbol in self.symbols]
        value["edges"] = [asdict(edge) for edge in self.edges]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CodeGraph":
        return cls(
            schema_version=value.get("schema_version", CODE_GRAPH_SCHEMA),
            root=value["root"],
            generated_at=value.get("generated_at", now_iso()),
            git=dict(value.get("git", {})),
            files=list(value.get("files", [])),
            symbols=[CodeSymbol(**symbol) for symbol in value.get("symbols", [])],
            edges=[CodeEdge(**edge) for edge in value.get("edges", [])],
        )


def build_code_graph(root: Path | str) -> CodeGraph:
    root_path = Path(root).expanduser().resolve()
    files = list_code_files(root_path)
    churn = git_churn(root_path)
    history = git_history(root_path)
    recency = history["recency"]
    authors = history["authors"]
    git_info = git_metadata(root_path)
    symbols: list[CodeSymbol] = []
    edges: list[CodeEdge] = []
    file_entries: list[dict[str, Any]] = []

    for file_path in files:
        rel = file_path.relative_to(root_path).as_posix()
        text = read_text_safely(file_path)
        imports = parse_imports(file_path, text)
        file_symbols = parse_symbols(file_path, text, rel)
        symbols.extend(file_symbols)
        for symbol in file_symbols:
            edges.append(CodeEdge(source=rel, target=symbol.name, kind="defines", detail=symbol.kind))
        for imported in imports:
            target = resolve_import(root_path, file_path, imported) or imported
            edges.append(CodeEdge(source=rel, target=target, kind="imports", detail=imported))
        file_entries.append(
            {
                "path": rel,
                "language": language_for(file_path),
                "lines": text.count("\n") + (1 if text else 0),
                "bytes": len(text.encode("utf-8")),
                "git_churn": churn.get(rel, 0),
                "git_last_modified": recency.get(rel),
                "git_authors": authors.get(rel, 0),
                "imports": imports,
            }
        )

    known = {entry["path"] for entry in file_entries}
    for coupling in history["couplings"]:
        if coupling["a"] in known and coupling["b"] in known:
            edges.append(CodeEdge(source=coupling["a"], target=coupling["b"], kind="co_change", detail=str(coupling["count"])))

    return CodeGraph(
        schema_version=CODE_GRAPH_SCHEMA,
        root=str(root_path),
        generated_at=now_iso(),
        git=git_info,
        files=file_entries,
        symbols=symbols,
        edges=edges,
    )


def list_code_files(root: Path) -> list[Path]:
    tracked = git_ls_files(root)
    if tracked:
        return [root / path for path in tracked if is_code_file(root / path)]

    found: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if is_code_file(path):
            found.append(path)
    return sorted(found)


def is_code_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in CODE_EXTENSIONS


def parse_symbols(path: Path, text: str, rel: str) -> list[CodeSymbol]:
    if path.suffix == ".py":
        return parse_python_symbols(text, rel)
    return parse_text_symbols(text, rel)


def parse_python_symbols(text: str, rel: str) -> list[CodeSymbol]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return parse_text_symbols(text, rel)

    symbols: list[CodeSymbol] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            symbols.append(
                CodeSymbol(
                    name=node.name,
                    kind=kind,
                    path=rel,
                    line=node.lineno,
                    signature=python_signature(node),
                )
            )
    symbols.sort(key=lambda symbol: (symbol.path, symbol.line, symbol.name))
    return symbols


def python_signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        bases = [getattr(base, "id", "") for base in node.bases]
        return f"class {node.name}({', '.join(base for base in bases if base)})"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = [arg.arg for arg in node.args.args]
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"
    return ""


def parse_text_symbols(text: str, rel: str) -> list[CodeSymbol]:
    symbols: list[CodeSymbol] = []
    for match in JS_SYMBOL_RE.finditer(text):
        name = match.group(1) or match.group(2)
        if not name:
            continue
        line = text.count("\n", 0, match.start()) + 1
        head = text[match.start() : text.find("\n", match.start())].strip()
        kind = "class" if "class " in head else "function"
        symbols.append(CodeSymbol(name=name, kind=kind, path=rel, line=line, signature=head[:180]))
    return symbols


def parse_imports(path: Path, text: str) -> list[str]:
    if path.suffix == ".py":
        return parse_python_imports(text)
    imports: set[str] = set()
    for match in IMPORT_RE.finditer(text):
        value = next((group for group in match.groups() if group), None)
        if value:
            imports.add(value)
    return sorted(imports)


def parse_python_imports(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.add(module)
    return sorted(imports)


def resolve_import(root: Path, file_path: Path, imported: str) -> str | None:
    candidates: list[Path] = []
    if imported.startswith("."):
        dots = len(imported) - len(imported.lstrip("."))
        base = file_path.parent
        for _ in range(max(0, dots - 1)):
            base = base.parent
        imported_path = imported.lstrip(".").replace(".", "/")
        candidates.extend([base / imported_path, base / f"{imported_path}.py", base / f"{imported_path}.ts", base / f"{imported_path}.js"])
    elif imported.startswith("./") or imported.startswith("../"):
        candidates.extend([file_path.parent / imported, file_path.parent / f"{imported}.ts", file_path.parent / f"{imported}.js", file_path.parent / imported / "index.ts"])
    else:
        candidates.extend([root / imported.replace(".", "/"), root / f"{imported.replace('.', '/')}.py"])

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve().relative_to(root).as_posix()
        if candidate.is_dir():
            for suffix in ("__init__.py", "index.ts", "index.tsx", "index.js", "index.jsx"):
                index = candidate / suffix
                if index.is_file():
                    return index.resolve().relative_to(root).as_posix()
    return None


def search_code_graph(graph: CodeGraph, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    terms = tokenize(query)
    results: list[dict[str, Any]] = []
    if not terms:
        return []
    for file_entry in graph.files:
        haystack = " ".join([file_entry["path"], file_entry["language"], *file_entry.get("imports", [])]).lower()
        score = sum(2 for term in terms if term in haystack) + min(5, int(file_entry.get("git_churn", 0)))
        if score:
            results.append({"kind": "file", "score": score, "path": file_entry["path"], "item": file_entry})
    for symbol in graph.symbols:
        haystack = f"{symbol.name} {symbol.kind} {symbol.path} {symbol.signature}".lower()
        score = sum(3 for term in terms if term in haystack)
        if score:
            results.append({"kind": "symbol", "score": score, "path": symbol.path, "item": asdict(symbol)})
    results.sort(key=lambda item: (item["score"], item["path"]), reverse=True)
    return results[:limit]


def render_code_pack(graph: CodeGraph, query: str, *, limit: int = 20) -> str:
    results = search_code_graph(graph, query, limit=limit)
    lines = [
        "# CodeGraphPack",
        "",
        f"root: {graph.root}",
        f"generated_at: {graph.generated_at}",
        f"query: {query}",
        "",
        "## Relevant Code",
    ]
    for result in results:
        if result["kind"] == "symbol":
            item = result["item"]
            lines.append(f"- symbol `{item['name']}` {item['kind']} in `{item['path']}:{item['line']}` score={result['score']}")
            if item.get("signature"):
                lines.append(f"  `{item['signature']}`")
        else:
            item = result["item"]
            lines.append(f"- file `{item['path']}` lang={item['language']} churn={item['git_churn']} score={result['score']}")
    return "\n".join(lines) + "\n"


def save_code_graph(graph: CodeGraph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_code_graph(path: Path) -> CodeGraph:
    return CodeGraph.from_dict(json.loads(path.read_text(encoding="utf-8")))


def language_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript-react",
        ".ts": "typescript",
        ".tsx": "typescript-react",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".swift": "swift",
        ".sh": "shell",
    }.get(suffix, suffix.lstrip(".") or "unknown")


def read_text_safely(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def git_ls_files(root: Path) -> list[str]:
    result = run_git(root, ["ls-files", "--cached", "--others", "--exclude-standard"])
    if result is None:
        return []
    return [line for line in result.splitlines() if line]


def git_churn(root: Path) -> dict[str, int]:
    result = run_git(root, ["log", "--name-only", "--pretty=format:"])
    churn: dict[str, int] = {}
    if result is None:
        return churn
    for line in result.splitlines():
        line = line.strip()
        if line:
            churn[line] = churn.get(line, 0) + 1
    return churn


def git_history(root: Path, *, max_pair_files: int = 25, top_couplings: int = 200) -> dict[str, Any]:
    """One pass over git log for per-file recency/authors and co-change coupling.

    Co-change coupling (files changed in the same commit) is a strong, cheap
    signal of logical dependency that import edges miss. Huge commits are skipped
    (``max_pair_files``) so vendored/bulk commits don't create noise.
    """
    result = run_git(root, ["log", "--name-only", "--date=short", "--pretty=format:%x01%H%x1f%an%x1f%ad"])
    recency: dict[str, str] = {}
    authors: dict[str, set[str]] = {}
    pair_counts: dict[tuple[str, str], int] = {}
    if result is None:
        return {"recency": {}, "authors": {}, "couplings": []}

    commit_author = commit_date = None
    commit_files: list[str] = []

    def flush() -> None:
        for path in commit_files:
            if commit_date and path not in recency:  # log is newest-first
                recency[path] = commit_date
            if commit_author:
                authors.setdefault(path, set()).add(commit_author)
        if 1 < len(commit_files) <= max_pair_files:
            ordered = sorted(set(commit_files))
            for i in range(len(ordered)):
                for j in range(i + 1, len(ordered)):
                    key = (ordered[i], ordered[j])
                    pair_counts[key] = pair_counts.get(key, 0) + 1

    for line in result.splitlines():
        if line.startswith("\x01"):
            flush()
            parts = line[1:].split("\x1f")
            commit_author = parts[1] if len(parts) > 1 else None
            commit_date = parts[2] if len(parts) > 2 else None
            commit_files = []
        elif line.strip():
            commit_files.append(line.strip())
    flush()

    couplings = sorted(pair_counts.items(), key=lambda item: item[1], reverse=True)[:top_couplings]
    return {
        "recency": recency,
        "authors": {path: len(names) for path, names in authors.items()},
        "couplings": [{"a": a, "b": b, "count": count} for (a, b), count in couplings if count >= 2],
    }


def git_metadata(root: Path) -> dict[str, Any]:
    return {
        "is_repo": run_git(root, ["rev-parse", "--is-inside-work-tree"]) == "true",
        "head": run_git(root, ["rev-parse", "--short", "HEAD"]),
        "branch": run_git(root, ["branch", "--show-current"]),
    }


def run_git(root: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip()
