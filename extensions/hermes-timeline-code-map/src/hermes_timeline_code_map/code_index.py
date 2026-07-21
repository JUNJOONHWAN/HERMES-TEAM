from __future__ import annotations

import ast
import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXCLUDED_DIRS = {
    ".cache",
    ".codex",
    ".delamain",
    ".git",
    ".hermes",
    ".legorhythm",
    ".legoroblox",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "attempts",
    "build",
    "certs",
    "coverage",
    "current_build_replays",
    "dist",
    "gaps",
    "handoffs",
    "htmlcov",
    "node_modules",
    "screenshots",
    "site-packages",
    "transactions",
    "validation_trials",
    "venv",
    "_candidates",
}

BINARY_EXTS = {
    ".7z",
    ".bin",
    ".bmp",
    ".db",
    ".dylib",
    ".eot",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".lockb",
    ".mp3",
    ".mp4",
    ".otf",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".so",
    ".tar",
    ".ttf",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}

TEXT_EXTS = {
    "",
    ".bash",
    ".c",
    ".cc",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
    ".zsh",
}

LANG_BY_EXT = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".md": "docs",
    ".mjs": "javascript",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".toml": "config",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".yaml": "config",
    ".yml": "config",
    ".zsh": "shell",
}

SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)|"
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?[^=]*?\)?\s*=>"
)
IMPORT_RE = re.compile(
    r"^\s*(?:import\s+.+?\s+from\s+|import\s*\(|require\s*\()\s*['\"]([^'\"]+)['\"]"
)
CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
TOKEN_RE = re.compile(r"[A-Za-z0-9_.$:/-]+")


@dataclass
class IndexedFile:
    path: str
    abs_path: str
    language: str
    suffix: str
    size: int
    line_count: int
    sha256: str
    summary: str


@dataclass
class IndexedSymbol:
    path: str
    name: str
    kind: str
    line_start: int
    line_end: int | None
    signature: str
    parent: str | None
    text: str


@dataclass
class IndexedEdge:
    from_path: str
    from_symbol: str | None
    to_path: str | None
    to_symbol: str | None
    relation: str
    line_start: int | None
    evidence: str
    weight: float


def tokenize(value: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(value or "") if len(token) > 1]


def language_for(path: Path) -> str:
    if path.name == "Dockerfile":
        return "dockerfile"
    if path.name == "Makefile":
        return "makefile"
    if path.suffix.lower() in {".json", ".ini", ".cfg", ".conf"}:
        return "config"
    return LANG_BY_EXT.get(path.suffix.lower(), path.suffix.lower().lstrip(".") or "text")


def should_scan_path(path: Path, root: Path, *, include_artifacts: bool) -> bool:
    rel_parts = path.relative_to(root).parts
    if not include_artifacts and any(part in EXCLUDED_DIRS for part in rel_parts[:-1]):
        return False
    if path.suffix.lower() in BINARY_EXTS:
        return False
    if path.suffix.lower() not in TEXT_EXTS and path.name not in {"Dockerfile", "Makefile", "README", "LICENSE"}:
        return False
    return True


def iter_source_files(root: Path, *, include_artifacts: bool, max_files: int) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        if not include_artifacts:
            dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS]
        for filename in filenames:
            path = base / filename
            if should_scan_path(path, root, include_artifacts=include_artifacts):
                files.append(path)
                if len(files) >= max_files:
                    return sorted(files)
    return sorted(files)


def read_text(path: Path, max_file_bytes: int) -> tuple[str, bool]:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_file_bytes + 1)
    except OSError:
        return "", True
    truncated = len(data) > max_file_bytes
    if truncated:
        data = data[:max_file_bytes]
    return data.decode("utf-8", errors="ignore"), truncated


def line_at(lines: list[str], line_no: int | None) -> str:
    if line_no is None or line_no <= 0 or line_no > len(lines):
        return ""
    return lines[line_no - 1].strip()


def file_summary(text: str, rel_path: str, language: str) -> str:
    candidates: list[str] = [rel_path, language]
    for line in text.splitlines()[:80]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "//", "/*", "*", "--")):
            candidates.append(stripped[:160])
        elif re.match(r"^(class|def|function|export|import|from|const|let|var)\b", stripped):
            candidates.append(stripped[:160])
        if len(candidates) >= 14:
            break
    return "\n".join(candidates)


class PythonVisitor(ast.NodeVisitor):
    def __init__(self, path: str, lines: list[str]) -> None:
        self.path = path
        self.lines = lines
        self.symbols: list[IndexedSymbol] = []
        self.edges: list[IndexedEdge] = []
        self.stack: list[str] = []

    @property
    def current_symbol(self) -> str | None:
        return ".".join(self.stack) if self.stack else None

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        name = ".".join([*self.stack, node.name]) if self.stack else node.name
        self.symbols.append(
            IndexedSymbol(
                path=self.path,
                name=name,
                kind="class",
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", None),
                signature=line_at(self.lines, node.lineno),
                parent=self.current_symbol,
                text=ast.get_docstring(node) or "",
            )
        )
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node, "async_function")

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        name = ".".join([*self.stack, node.name]) if self.stack else node.name
        self.symbols.append(
            IndexedSymbol(
                path=self.path,
                name=name,
                kind=kind,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", None),
                signature=line_at(self.lines, node.lineno),
                parent=self.current_symbol,
                text=ast.get_docstring(node) or "",
            )
        )
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            self.edges.append(
                IndexedEdge(
                    from_path=self.path,
                    from_symbol=self.current_symbol,
                    to_path=None,
                    to_symbol=alias.name,
                    relation="imports",
                    line_start=node.lineno,
                    evidence=line_at(self.lines, node.lineno),
                    weight=0.8,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            target = f"{module}.{alias.name}".strip(".")
            self.edges.append(
                IndexedEdge(
                    from_path=self.path,
                    from_symbol=self.current_symbol,
                    to_path=None,
                    to_symbol=target,
                    relation="imports",
                    line_start=node.lineno,
                    evidence=line_at(self.lines, node.lineno),
                    weight=0.8,
                )
            )

    def visit_Call(self, node: ast.Call) -> Any:
        name = call_name(node.func)
        if name:
            self.edges.append(
                IndexedEdge(
                    from_path=self.path,
                    from_symbol=self.current_symbol,
                    to_path=None,
                    to_symbol=name,
                    relation="calls",
                    line_start=getattr(node, "lineno", None),
                    evidence=line_at(self.lines, getattr(node, "lineno", None)),
                    weight=0.5,
                )
            )
        self.generic_visit(node)


def call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def parse_python(path: str, text: str) -> tuple[list[IndexedSymbol], list[IndexedEdge]]:
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], []
    visitor = PythonVisitor(path, lines)
    visitor.visit(tree)
    return visitor.symbols, visitor.edges


def parse_javascript_like(path: str, text: str) -> tuple[list[IndexedSymbol], list[IndexedEdge]]:
    symbols: list[IndexedSymbol] = []
    edges: list[IndexedEdge] = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, start=1):
        match = SYMBOL_RE.search(line)
        if match:
            name = next(group for group in match.groups() if group)
            kind = "class" if "class " in line else "function"
            symbols.append(
                IndexedSymbol(
                    path=path,
                    name=name,
                    kind=kind,
                    line_start=idx,
                    line_end=None,
                    signature=line.strip(),
                    parent=None,
                    text="",
                )
            )
        import_match = IMPORT_RE.search(line)
        if import_match:
            edges.append(
                IndexedEdge(
                    from_path=path,
                    from_symbol=None,
                    to_path=None,
                    to_symbol=import_match.group(1),
                    relation="imports",
                    line_start=idx,
                    evidence=line.strip(),
                    weight=0.8,
                )
            )
        for call in CALL_RE.findall(line):
            if call in {"if", "for", "while", "switch", "catch", "function", "return"}:
                continue
            edges.append(
                IndexedEdge(
                    from_path=path,
                    from_symbol=None,
                    to_path=None,
                    to_symbol=call,
                    relation="calls",
                    line_start=idx,
                    evidence=line.strip()[:240],
                    weight=0.35,
                )
            )
    return symbols, edges


def parse_generic(path: str, text: str) -> tuple[list[IndexedSymbol], list[IndexedEdge]]:
    symbols: list[IndexedSymbol] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                symbols.append(
                    IndexedSymbol(
                        path=path,
                        name=title[:120],
                        kind="heading",
                        line_start=idx,
                        line_end=None,
                        signature=stripped[:240],
                        parent=None,
                        text="",
                    )
                )
    return symbols, []


def build_code_index(
    repo_root: str,
    *,
    include_artifacts: bool = False,
    max_file_bytes: int = 512_000,
    max_files: int = 20_000,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"repository root not found: {root}")

    indexed_files: list[IndexedFile] = []
    symbols: list[IndexedSymbol] = []
    edges: list[IndexedEdge] = []
    truncated_files = 0
    language_counts: Counter[str] = Counter()

    for path in iter_source_files(root, include_artifacts=include_artifacts, max_files=max_files):
        rel_path = path.relative_to(root).as_posix()
        try:
            stat = path.stat()
        except OSError:
            continue
        text, truncated = read_text(path, max_file_bytes)
        truncated_files += int(truncated)
        digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
        language = language_for(path)
        language_counts[language] += 1
        lines = text.splitlines()
        indexed_files.append(
            IndexedFile(
                path=rel_path,
                abs_path=str(path),
                language=language,
                suffix=path.suffix.lower(),
                size=stat.st_size,
                line_count=len(lines),
                sha256=digest,
                summary=file_summary(text, rel_path, language),
            )
        )
        if language == "python":
            file_symbols, file_edges = parse_python(rel_path, text)
        elif language in {"javascript", "typescript"}:
            file_symbols, file_edges = parse_javascript_like(rel_path, text)
        else:
            file_symbols, file_edges = parse_generic(rel_path, text)
        symbols.extend(file_symbols)
        edges.extend(file_edges)

    return {
        "repo_root": str(root),
        "repo_name": root.name,
        "files": [item.__dict__ for item in indexed_files],
        "symbols": [item.__dict__ for item in symbols],
        "edges": [item.__dict__ for item in edges],
        "counts": {
            "files": len(indexed_files),
            "symbols": len(symbols),
            "edges": len(edges),
            "languages": dict(sorted(language_counts.items())),
            "truncated_files": truncated_files,
            "max_files_reached": len(indexed_files) >= max_files,
        },
        "limits": {
            "include_artifacts": include_artifacts,
            "max_file_bytes": max_file_bytes,
            "max_files": max_files,
            "excluded_dirs": sorted(EXCLUDED_DIRS),
        },
    }


def score_text(query_tokens: list[str], text: str, weight: float = 1.0) -> tuple[float, list[str]]:
    haystack = text.lower()
    score = 0.0
    reasons: list[str] = []
    for token in query_tokens:
        if token and token in haystack:
            hits = haystack.count(token)
            score += weight * min(4, hits)
            reasons.append(token)
    return score, reasons


def summarize_reasons(reasons: list[str], max_items: int = 6) -> str:
    if not reasons:
        return "structural neighbor"
    unique = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)
    return ", ".join(unique[:max_items])
