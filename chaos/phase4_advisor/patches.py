"""Template patches per resilience gap, rendered as unified diffs and checked with `git apply --check`.

Nothing here writes to src/: every patch is built in memory from the file's current content.
"""
from __future__ import annotations

import difflib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .localize import CallSite, read_lines


def unified_diff(rel: str, old: list[str], new: list[str]) -> str:
    """Diff of two line lists (no line terminators); the file is assumed to end with a newline."""
    old = old[:-1] if old and old[-1] == "" else old
    new = new[:-1] if new and new[-1] == "" else new
    lines = list(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}", lineterm="", n=3))
    return "\n".join(lines) + "\n" if lines else ""


def new_file_diff(rel: str, content: str) -> str:
    body = content.rstrip("\n").split("\n")
    return "\n".join([f"--- /dev/null", f"+++ b/{rel}", f"@@ -0,0 +1,{len(body)} @@", *("+" + ln for ln in body)]) + "\n"


# ------------------------------------------------------------------ Go: per-call context deadline

def _go_ensure_imports(lines: list[str], packages: list[str]) -> list[str]:
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "import (")
    except StopIteration:
        return lines
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == ")")
    present = {ln.strip() for ln in lines[start + 1:end]}
    out = list(lines)
    for pkg in packages:
        if f'"{pkg}"' in present:
            continue
        group_end = start + 1
        while group_end < end and out[group_end].strip():
            group_end += 1
        pos = start + 1
        while pos < group_end and out[pos].strip() < f'"{pkg}"':
            pos += 1
        out.insert(pos, f'\t"{pkg}"')
        end += 1
    return out


def _fresh_name(body: str, preferred: str, alternative: str) -> str:
    return preferred if not re.search(rf"\b{preferred}\b", body) else alternative


def go_deadline(repo: Path, site: CallSite, deadline_ms: int) -> Optional[str]:
    if site.language != "go" or not site.method or not site.ctx_arg:
        return None
    lines = read_lines(repo / site.file)
    body = "\n".join(lines[site.func_start:site.func_end + 1])
    stem = site.method[0].lower() + site.method[1:]
    ctx_name = _fresh_name(body, "tCtx", f"{stem}Ctx")
    cancel_name = _fresh_name(body, "cancel", f"{stem}Cancel")

    new = list(lines)
    call_rx = re.compile(rf"(\b{re.escape(site.method)}\(\s*){re.escape(site.ctx_arg)}")
    replaced, count = call_rx.subn(lambda m: m.group(1) + ctx_name, new[site.call_line], count=1)
    if count != 1:
        return None
    new[site.call_line] = replaced
    indent = re.match(r"\s*", lines[site.stmt_line]).group(0)
    new[site.stmt_line:site.stmt_line] = [
        f"{indent}{ctx_name}, {cancel_name} := context.WithTimeout({site.ctx_arg}, {deadline_ms}*time.Millisecond)",
        f"{indent}defer {cancel_name}()",
    ]
    new = _go_ensure_imports(new, ["context", "time"])
    return unified_diff(site.file, lines, new)


# ------------------------------------------------------------------ Python: grpc timeout= keyword

def python_deadline(repo: Path, site: CallSite, deadline_ms: int) -> Optional[str]:
    if site.language != "python" or not site.method:
        return None
    lines = read_lines(repo / site.file)
    text = "\n".join(lines)
    offset = sum(len(ln) + 1 for ln in lines[:site.call_line])
    m = re.compile(rf"\.{re.escape(site.method)}\(").search(text, offset)
    if not m:
        return None
    depth, i = 0, m.end() - 1
    while i < len(text):
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        if depth == 0:
            break
        i += 1
    else:
        return None
    before = text[m.end():i].rstrip()
    kwarg = f"timeout={deadline_ms / 1000:g}"
    insert = kwarg if not before else (f" {kwarg}" if before.endswith(",") else f", {kwarg}")
    new_text = text[:m.end() + len(before)] + insert + text[m.end() + len(before):]
    return unified_diff(site.file, lines, new_text.split("\n"))


# ------------------------------------------------------------------ Istio: retries + outlier detection

def mesh_policy(repo: Path, downstream: str, deadline_ms: int, want_retry: bool, want_breaker: bool,
                experiment_id: str) -> tuple[Optional[str], Optional[str]]:
    if not (want_retry or want_breaker):
        return None, None
    rel = f"istio-manifests/{downstream}-resilience.yaml"
    if (repo / rel).exists():
        return None, None
    docs = [f"# Suggested by the chaos platform after experiment {experiment_id}.\n"
            f"# Apply with: kubectl apply -f {rel}"]
    if want_retry:
        per_try = max(25, deadline_ms // 2)
        docs.append(f"""apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: {downstream}-resilience
spec:
  hosts:
    - {downstream}
  http:
    - route:
        - destination:
            host: {downstream}
      timeout: {deadline_ms}ms
      retries:
        attempts: 2
        perTryTimeout: {per_try}ms
        retryOn: cancelled,deadline-exceeded,unavailable,resource-exhausted""")
    if want_breaker:
        docs.append(f"""apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: {downstream}-resilience
spec:
  host: {downstream}
  trafficPolicy:
    outlierDetection:
      consecutiveGatewayErrors: 3
      consecutive5xxErrors: 5
      interval: 5s
      baseEjectionTime: 30s
      maxEjectionPercent: 50""")
    content = docs[0] + "\n" + "\n---\n".join(docs[1:]) + "\n"
    return rel, new_file_diff(rel, content)


# ------------------------------------------------------------------ verification

def git_apply_check(repo: Path, diff: str) -> tuple[bool, str]:
    """Return (ok, detail). Runs `git apply --check` from the repo root; never applies anything."""
    if not diff:
        return False, "empty diff"
    git = shutil.which("git")
    if git is None:
        return False, "git not found on PATH"
    proc = subprocess.run([git, "apply", "--check", "-"], cwd=repo, input=diff.encode("utf-8"),
                          capture_output=True)
    if proc.returncode == 0:
        return True, "git apply --check passed"
    return False, proc.stderr.decode("utf-8", "replace").strip()
