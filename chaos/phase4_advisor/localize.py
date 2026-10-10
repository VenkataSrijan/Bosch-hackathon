"""Find the real call site in src/<faulty_service>/ where the faulty service calls the degraded dependency.

Line numbers from the RCA are never trusted; the code is searched. Go and Python gRPC clients are
understood precisely; other languages fall back to a name match without a patchable call site.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LANGUAGES = {".go": "go", ".py": "python", ".js": "node", ".ts": "node", ".cs": "csharp", ".java": "java"}
SKIP_DIRS = {"genproto", "node_modules", "vendor", "proto", "protos", "obj", "bin", "build", "tests", "test", "__pycache__"}
GENERATED = re.compile(r"(_pb2(_grpc)?\.py|\.pb\.go|_grpc\.pb\.go|\.pb\.cs|Grpc\.cs)$")

GO_CLIENT = re.compile(r"\bNew(\w+?)Client\(")
GO_INVOKE = re.compile(r"\bNew(?P<client>\w+?)Client\([^()]*\)\.\s*(?P<method>\w+)\(\s*(?P<arg>[\w.]+(?:\(\))?)\s*[,)]")
PY_STUB = re.compile(r"(?P<var>[\w.]+)\s*=\s*[\w.]*?(?P<client>\w+?)Stub\(")


@dataclass
class CallSite:
    file: str                 # repo-relative, posix
    language: str
    symbol: str               # e.g. "chargeCard()"
    method: Optional[str]     # RPC method, e.g. "Charge"
    func_start: int           # 0-based line index of the function header
    func_end: int             # 0-based line index of the function's last line
    stmt_line: int            # 0-based line where the calling statement starts
    call_line: int            # 0-based line holding "<Method>(<ctx arg>"
    ctx_arg: Optional[str]    # first argument of the RPC (Go), e.g. "ctx"
    patchable: bool = True

    def location(self) -> str:
        return f"{self.file}:{self.call_line + 1}"


@dataclass
class Localization:
    faulty_service: str
    downstream: Optional[str]
    service_dir: Optional[str]
    primary: Optional[CallSite] = None
    others: list[CallSite] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").split("\n")


def find_service_dir(repo: Path, service: str) -> Optional[Path]:
    src = repo / "src"
    direct = src / service
    if direct.is_dir():
        return direct
    want = re.sub(r"[^a-z0-9]", "", service.lower())
    for d in sorted(src.glob("*")) if src.is_dir() else []:
        if d.is_dir() and re.sub(r"[^a-z0-9]", "", d.name.lower()) == want:
            return d
    return None


def source_files(service_dir: Path) -> list[Path]:
    out = []
    for path in sorted(service_dir.rglob("*")):
        if path.suffix not in LANGUAGES or not path.is_file() or GENERATED.search(path.name):
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(service_dir).parts[:-1]):
            continue
        if path.name.endswith("_test.go"):
            continue
        out.append(path)
    return out


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _go_func_bounds(lines: list[str], idx: int) -> tuple[int, int, str]:
    start = idx
    while start > 0 and not lines[start].startswith("func "):
        start -= 1
    end = start
    while end < len(lines) - 1 and lines[end] != "}":
        end += 1
    m = re.match(r"func\s+(?:\([^)]*\)\s*)?(\w+)", lines[start])
    return start, end, (m.group(1) + "()") if m else "<anonymous>"


def _go_call_sites(rel: str, lines: list[str], downstream: str) -> list[CallSite]:
    sites = []
    for i, line in enumerate(lines):
        for m in GO_CLIENT.finditer(line):
            if _norm(m.group(1)) != _norm(downstream):
                continue
            window = "\n".join(lines[i:i + 4])
            inv = GO_INVOKE.search(window, m.start())
            if not inv:
                continue
            call_line = i + window[: inv.start("method")].count("\n")
            start, end, symbol = _go_func_bounds(lines, i)
            sites.append(CallSite(rel, "go", symbol, inv.group("method"), start, end, i, call_line, inv.group("arg")))
    return sites


def _py_func_bounds(lines: list[str], idx: int) -> tuple[int, int, str]:
    start = idx
    while start > 0 and not re.match(r"\s*(async\s+)?def \w+", lines[start]):
        start -= 1
    m = re.match(r"(\s*)(?:async\s+)?def (\w+)", lines[start])
    if not m:
        return 0, len(lines) - 1, "<module>"
    indent = len(m.group(1))
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            break
        end += 1
    return start, end - 1, m.group(2) + "()"


def _py_call_sites(rel: str, lines: list[str], downstream: str) -> list[CallSite]:
    stub_vars = {m.group("var") for ln in lines for m in [PY_STUB.search(ln)]
                 if m and _norm(m.group("client")) == _norm(downstream)}
    sites = []
    for var in stub_vars:
        call = re.compile(rf"(?<![\w.]){re.escape(var)}\.(\w+)\(")
        for i, line in enumerate(lines):
            m = call.search(line)
            if m:
                start, end, symbol = _py_func_bounds(lines, i)
                sites.append(CallSite(rel, "python", symbol, m.group(1), start, end, i, i, None))
    return sites


def _name_match_sites(rel: str, lang: str, lines: list[str], downstream: str) -> list[CallSite]:
    base = _norm(downstream).removesuffix("service")
    pat = re.compile(rf"{base}[\W_]*(service|svc|client|stub)", re.I)
    sites = []
    for i, line in enumerate(lines):
        if pat.search(line):
            sites.append(CallSite(rel, lang, "<unknown>", None, i, i, i, i, None, patchable=False))
            break
    return sites


def _hint_score(site: CallSite, hint_text: str) -> int:
    score = 0
    if site.method and re.search(rf"\b{re.escape(site.method)}\b", hint_text):
        score += 4
    if Path(site.file).name in hint_text:
        score += 2
    if site.symbol.rstrip("()") and site.symbol.rstrip("()") in hint_text:
        score += 3
    if site.patchable:
        score += 1
    return score


def localize(repo: Path, faulty_service: str, downstream: Optional[str], hint_text: str = "") -> Localization:
    loc = Localization(faulty_service=faulty_service, downstream=downstream, service_dir=None)
    service_dir = find_service_dir(repo, faulty_service)
    if service_dir is None:
        loc.notes.append(f"no source folder for {faulty_service} under src/")
        return loc
    loc.service_dir = service_dir.relative_to(repo).as_posix()
    if not downstream:
        loc.notes.append("no downstream dependency could be identified from the RCA or the plan")
        return loc

    sites: list[CallSite] = []
    for path in source_files(service_dir):
        rel = path.relative_to(repo).as_posix()
        lang = LANGUAGES[path.suffix]
        lines = read_lines(path)
        if lang == "go":
            sites += _go_call_sites(rel, lines, downstream)
        elif lang == "python":
            sites += _py_call_sites(rel, lines, downstream)
    if not sites:
        for path in source_files(service_dir):
            sites += _name_match_sites(path.relative_to(repo).as_posix(), LANGUAGES[path.suffix],
                                       read_lines(path), downstream)
    if not sites:
        loc.notes.append(f"no call from {faulty_service} to {downstream} found in {loc.service_dir}")
        return loc

    ranked = sorted(enumerate(sites), key=lambda p: (-_hint_score(p[1], hint_text), p[0]))
    loc.primary = ranked[0][1]
    loc.others = [s for _, s in ranked[1:]]
    return loc
