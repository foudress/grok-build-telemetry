"""Name → module resolution for the Python ast call-graph."""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass, field
from pathlib import Path


IGNORE_DIRS = frozenset(
    {
        ".git",
        "venv",
        ".venv",
        "node_modules",
        "vendor",
        "__pycache__",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        ".idea",
        ".vscode",
        ".cache",
        ".local",
        ".npm",
        ".cargo",
        ".rustup",
        ".conda",
        ".docker",
        ".nuget",
        ".grok",
        ".cursor",
        "appdata",
        "application data",
        "local settings",
        "downloads",
        "site-packages",
        "anaconda3",
        "miniconda3",
        "program files",
        "program files (x86)",
        "programdata",
        "windows",
        "$recycle.bin",
        "library",
    }
)
MAX_FILE_BYTES = 1_048_576
MAX_FILE_NODES = 1500
MAX_SITES = 20


def posix_rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def parent_posix(rel: str) -> str:
    parent = Path(rel).parent.as_posix()
    return "" if parent == "." else parent


def module_name(rel: str) -> str:
    parts = list(Path(rel).parts)
    if not parts:
        return ""
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts = [*parts[:-1], Path(parts[-1]).stem]
    return ".".join(parts)


def ancestor_dirs(rel: str) -> list[str]:
    """Parent directories of a file, root-first, excluding the scan root."""
    out: list[str] = []
    cur = parent_posix(rel)
    while cur:
        out.append(cur)
        cur = parent_posix(cur)
    out.reverse()
    return out


@dataclass
class RawImport:
    local: str
    level: int
    module: str | None
    symbol: str | None
    is_from: bool


@dataclass
class RawCall:
    caller: str
    name: str
    attr: str | None
    lineno: int


@dataclass
class Extracted:
    defs: set[str] = field(default_factory=set)
    imports: list[RawImport] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)


@dataclass
class Binding:
    """local name → imported module (+ optional symbol for `from m import f`)."""

    module: str
    symbol: str | None


@dataclass
class ResolvedCall:
    kind: str
    dst: str
    caller: str
    callee: str


class _Extractor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.out = Extracted()
        self._stack: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if not self._stack:
            self.out.defs.add(node.name)
        self._stack.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if not self._stack:
            self.out.defs.add(node.name)
        self._stack.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self._stack.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            mod = alias.name
            local = alias.asname or mod.split(".", 1)[0]
            bound = mod if alias.asname else (mod.split(".", 1)[0] if "." in mod else mod)
            self.out.imports.append(
                RawImport(local=local, level=0, module=bound, symbol=None, is_from=False)
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if any(alias.name == "*" for alias in node.names):
            return
        for alias in node.names:
            local = alias.asname or alias.name
            self.out.imports.append(
                RawImport(
                    local=local,
                    level=node.level,
                    module=node.module,
                    symbol=alias.name,
                    is_from=True,
                )
            )

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Call):
            self.generic_visit(node)
            return
        if isinstance(func, ast.Name) and func.id == "getattr":
            self.generic_visit(node)
            return
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in {"self", "cls"}:
                self.generic_visit(node)
                return
        caller = self._stack[-1] if self._stack else "<module>"
        if isinstance(func, ast.Name):
            self.out.calls.append(RawCall(caller, func.id, None, node.lineno))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            self.out.calls.append(RawCall(caller, func.value.id, func.attr, node.lineno))
        self.generic_visit(node)


def extract_module(tree: ast.AST) -> Extracted:
    ext = _Extractor()
    ext.visit(tree)
    return ext.out


def parse_python(text: str, filename: str = "<unknown>") -> ast.AST | None:
    # User trees often have non-raw regex strings (`"\d"`). Python 3.12+
    # emits SyntaxWarning; the graph scan must not dump those to the dashboard.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(text, filename=filename)
    except SyntaxError:
        return None


def _anchor_package(current_mod: str, packages: set[str]) -> str:
    if current_mod in packages:
        return current_mod
    if "." in current_mod:
        return current_mod.rsplit(".", 1)[0]
    return ""


def _rel_base(current_mod: str, level: int, module: str | None, packages: set[str]) -> str | None:
    if level <= 0:
        return module or ""
    pkg = _anchor_package(current_mod, packages)
    for _ in range(level - 1):
        if pkg == "":
            return None
        pkg = pkg.rsplit(".", 1)[0] if "." in pkg else ""
    if module:
        return f"{pkg}.{module}" if pkg else module
    return pkg


def _same_package(
    current_mod: str, name: str, by_name: dict[str, str], packages: set[str]
) -> str | None:
    if name in by_name:
        return name
    pkg = current_mod if current_mod in packages else (
        current_mod.rsplit(".", 1)[0] if "." in current_mod else ""
    )
    if pkg:
        cand = f"{pkg}.{name}"
        if cand in by_name:
            return cand
    return None


def resolve_import(
    raw: RawImport,
    current_mod: str,
    by_name: dict[str, str],
    packages: set[str],
) -> Binding | None:
    if not raw.is_from:
        target = raw.module or raw.local
        found = _same_package(current_mod, target, by_name, packages)
        if found is None:
            return None
        return Binding(module=found, symbol=None)

    base = _rel_base(current_mod, raw.level, raw.module, packages)
    if base is None:
        return None
    symbol = raw.symbol or raw.local
    sub = f"{base}.{symbol}" if base else symbol
    if sub in by_name:
        return Binding(module=sub, symbol=None)
    if base in by_name:
        return Binding(module=base, symbol=symbol)
    if base == "" and symbol in by_name:
        return Binding(module=symbol, symbol=None)
    found = _same_package(current_mod, base or symbol, by_name, packages)
    if found is None:
        return None
    if found == (base or symbol):
        return Binding(module=found, symbol=symbol if base else None)
    return Binding(module=found, symbol=None)


def resolve_call(
    call: RawCall,
    bindings: dict[str, Binding],
    current_rel: str,
    local_defs: set[str],
    by_name: dict[str, str],
    defs_by_mod: dict[str, set[str]],
) -> ResolvedCall | None:
    if call.attr is None and call.name in local_defs:
        return None
    binding = bindings.get(call.name)
    if binding is None:
        return None
    dst = by_name.get(binding.module)
    if not dst or dst == current_rel:
        return None
    if call.attr is None:
        if binding.symbol is None:
            return ResolvedCall("import", dst, call.caller, binding.module)
        callee = binding.symbol
        kind = "call" if callee in defs_by_mod.get(binding.module, set()) else "import"
        return ResolvedCall(kind, dst, call.caller, callee)
    if binding.symbol is not None:
        return None
    callee = call.attr
    kind = "call" if callee in defs_by_mod.get(binding.module, set()) else "import"
    return ResolvedCall(kind, dst, call.caller, callee)
