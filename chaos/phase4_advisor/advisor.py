"""Turn RootCauseAnalysis.json into a concrete, reviewable Recommendation.json.

Pipeline: localize the call site -> audit its resilience controls -> synthesize a patch (LLM draft if
available and it passes `git apply --check`, templates otherwise) -> write the recommendation.
The patch is a suggestion only; nothing under src/ is ever modified.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common import llm
from common.config import CHAOS_ROOT
from common.contracts import ExperimentPlan, Priority, RcaVerdict, Recommendation, RootCauseAnalysis
from common.io import write_json_atomic

from .audit import LABELS, Control, audit_call_site
from .localize import CallSite, Localization, find_service_dir, localize, read_lines
from .patches import git_apply_check, go_deadline, mesh_policy, python_deadline

TARGET_REPOSITORY = "github.com/VenkataSrijan/Bosch-hackathon"
AUDIT_FILE = "ResilienceAudit.json"
PATCH_FILE = "Recommendation.patch"

VULNERABILITY = {
    "timeout": ("CASCADING_TIMEOUT_COLLAPSE", "timeout"),
    "retry": ("NO_RETRY_ON_TRANSIENT_FAILURE", "retry"),
    "circuit_breaker": ("MISSING_CIRCUIT_BREAKER", "breaker"),
    "fallback": ("NO_GRACEFUL_DEGRADATION", "fallback"),
}


def choose_deadline_ms(plan: ExperimentPlan) -> int:
    """A per-call budget well above healthy latency but well below the abort threshold."""
    generous = max(100, math.ceil(plan.steady_state_baseline.p99_latency_ms * 8 / 50) * 50)
    cap = max(50, int(plan.abort_thresholds.max_p99_latency_ms / 2) // 50 * 50)
    return int(min(generous, cap))


def downstream_of(rca: RootCauseAnalysis, plan: ExperimentPlan, faulty: str) -> Optional[str]:
    """The dependency the faulty service waits on: the hop before it on the cascade path, else the target."""
    hops = sorted(rca.cascade_path, key=lambda h: h.hop)
    for prev, hop in zip(hops, hops[1:]):
        if hop.service == faulty and prev.service != faulty:
            return prev.service
    target = plan.target_node.service_name
    return target if target != faulty else None


def describe_fault(plan: ExperimentPlan) -> str:
    fp, p = plan.fault_profile, plan.fault_profile.parameters
    if fp.action == "latency" and "delay_ms" in p:
        jitter = f" ± {p['jitter_ms']} ms" if p.get("jitter_ms") else ""
        return f"{p['delay_ms']} ms{jitter} network latency"
    return f"{fp.category}/{fp.action} fault"


def _recommendation_id(downstream: Optional[str], slug: str, experiment_id: str) -> str:
    n = int(hashlib.sha1(experiment_id.encode()).hexdigest(), 16) % 1000
    short = re.sub(r"[^a-z0-9]", "", (downstream or "svc").removesuffix("service"))[:4] or "svc"
    return f"rec-{short}-{slug}-{n:03d}"


def _verification_command(exp_dir: Path) -> str:
    try:
        rel = exp_dir.resolve().relative_to(CHAOS_ROOT).as_posix()
    except ValueError:
        rel = exp_dir.resolve().as_posix()
    return f"python -m phase2_injection --exp {rel}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# ------------------------------------------------------------------ patch synthesis

def _llm_code_patch(repo: Path, site: CallSite, missing: list[str], deadline_ms: int) -> Optional[str]:
    lines = read_lines(repo / site.file)
    lo, hi = max(0, site.func_start - 3), min(len(lines), site.func_end + 4)
    excerpt = "\n".join(f"{i + 1:5d} {lines[i]}" for i in range(lo, hi))
    system = ("You write minimal, compilable unified diffs that harden one RPC call site. Output ONLY the diff, "
              "starting with '--- a/'. Touch only the given file. Do not invent helpers that do not exist.")
    user = (f"File: {site.file}\nLanguage: {site.language}\nFunction: {site.symbol}\n"
            f"RPC: {site.method}\nMissing controls: {', '.join(missing)}\n"
            f"Use a per-call deadline of {deadline_ms} ms.\nFunction excerpt (line-numbered):\n{excerpt}\n")
    try:
        text = llm.complete(system, user)
    except llm.LLMUnavailable:
        return None
    m = re.search(r"^--- a/.*", text, re.S | re.M)
    diff = m.group(0).strip("`\n ") + "\n" if m else ""
    return diff if diff and git_apply_check(repo, diff)[0] else None


def synthesize_patch(repo: Path, site: CallSite, controls: dict[str, Control], downstream: str,
                     deadline_ms: int, experiment_id: str, use_llm: bool) -> tuple[Optional[str], dict]:
    missing = [n for n, c in controls.items() if not c.present]
    info: dict = {"code_patch": None, "mesh_patch": None, "source": None, "check": None}
    code_diff = None
    if site.patchable and "timeout" in missing:
        if use_llm:
            code_diff = _llm_code_patch(repo, site, missing, deadline_ms)
            info["source"] = "llm" if code_diff else None
        if code_diff is None:
            code_diff = go_deadline(repo, site, deadline_ms) or python_deadline(repo, site, deadline_ms)
            info["source"] = "template" if code_diff else None
        info["code_patch"] = site.file if code_diff else None
    mesh_rel, mesh_diff = mesh_policy(repo, downstream, deadline_ms, "retry" in missing,
                                      "circuit_breaker" in missing, experiment_id)
    info["mesh_patch"] = mesh_rel
    if mesh_diff and info["source"] is None:
        info["source"] = "template"
    diff = (code_diff or "") + (mesh_diff or "")
    if not diff:
        return None, info
    ok, detail = git_apply_check(repo, diff)
    info["check"] = detail
    if not ok and code_diff and mesh_diff:  # keep whichever half still applies
        for part, key in ((code_diff, "mesh_patch"), (mesh_diff, "code_patch")):
            if git_apply_check(repo, part)[0]:
                info[key] = None
                info["check"] = "git apply --check passed (partial patch)"
                return part, info
    return (diff, info) if ok else (None, info)


# ------------------------------------------------------------------ recommendation

def _guidance(faulty: str, downstream: str, site: CallSite, controls: dict[str, Control], deadline_ms: int,
              patch_info: dict) -> str:
    parts = [f"{faulty} calls {downstream} synchronously in {site.symbol} ({site.location()}). "
             "Upstream services must not wait indefinitely on a slow dependency: every outbound call needs its own "
             "budget, and failures inside that budget must be absorbed rather than passed straight to the user."]
    c = controls
    if not c["timeout"].present:
        parts.append(f"Add a client-side deadline of about {deadline_ms} ms on the {site.method or 'outbound'} call "
                     "(roughly 8x the healthy edge p99, and well under the abort threshold) so a slow replica "
                     "fails fast instead of holding request handlers.")
    if not c["retry"].present:
        parts.append("Retry idempotent failures once with backoff and a per-try timeout, so a request that hit the "
                     "degraded replica is served by a healthy one.")
    if not c["circuit_breaker"].present:
        parts.append(f"Eject {downstream} replicas that keep failing (circuit breaker / outlier detection) so "
                     "traffic stops flowing to the degraded pod.")
    if not c["fallback"].present:
        parts.append(f"Decide what {faulty} does when {downstream} stays unavailable: return a clear, retryable "
                     "error to the caller, or defer the work, rather than timing out the whole request.")
    if patch_info.get("mesh_patch"):
        parts.append(f"The suggested diff also adds {patch_info['mesh_patch']} (Istio retries and outlier "
                     "detection); apply it with kubectl after review.")
    return " ".join(parts)


# ------------------------------------------------------------------ options (a few possible fixes)

# Per missing control: a short title, its own priority, and how it is applied.
OPTION_META: dict[str, tuple[str, Priority, str]] = {
    "timeout": ("Add a client-side deadline", Priority.HIGH, "code patch"),
    "circuit_breaker": ("Add a circuit breaker / outlier detection", Priority.HIGH, "mesh config (kubectl apply)"),
    "retry": ("Retry idempotent failures with backoff", Priority.MEDIUM, "mesh config (kubectl apply)"),
    "fallback": ("Add a graceful fallback path", Priority.MEDIUM, "code change (manual)"),
}


def _gap_rationale(control: str, faulty: str, downstream: str, site: CallSite, deadline_ms: int) -> str:
    if control == "timeout":
        return (f"Add a client-side deadline of about {deadline_ms} ms on the {site.method or 'outbound'} call "
                "(~8x the healthy edge p99, well under the abort threshold) so a slow replica fails fast instead "
                "of holding request handlers.")
    if control == "retry":
        return ("Retry idempotent failures once with backoff and a per-try timeout, so a request that hit the "
                "degraded replica is served by a healthy one.")
    if control == "circuit_breaker":
        return (f"Eject {downstream} replicas that keep failing (circuit breaker / outlier detection) so traffic "
                "stops flowing to the degraded pod.")
    return (f"Decide what {faulty} does when {downstream} stays unavailable: return a clear, retryable error, or "
            "defer the work, rather than timing out the whole request.")


def build_options(repo: Path, site: CallSite, controls: dict[str, Control], faulty: str, downstream: str,
                  deadline_ms: int, experiment_id: str) -> list[dict]:
    """One candidate fix per missing resilience control, each independently reviewable.

    The combined Recommendation stays the headline; these are the alternatives a human can pick between.
    """
    missing = [n for n, c in controls.items() if not c.present]
    options: list[dict] = []
    for i, name in enumerate(missing):
        title, priority, applies_via = OPTION_META[name]
        diff: Optional[str] = None
        if name == "timeout" and site.patchable:
            diff = go_deadline(repo, site, deadline_ms) or python_deadline(repo, site, deadline_ms)
        elif name in ("retry", "circuit_breaker"):
            _, diff = mesh_policy(repo, downstream, deadline_ms, name == "retry", name == "circuit_breaker",
                                  experiment_id)
        if diff and not git_apply_check(repo, diff)[0]:
            diff = None
        options.append({
            "option_id": f"opt-{name.replace('_', '-')}",
            "rank": i + 1,
            "strategy": name,
            "title": title,
            "control": LABELS[name],
            "priority": priority.value,
            "rationale": _gap_rationale(name, faulty, downstream, site, deadline_ms),
            "target_file": site.file if name == "timeout" else None,
            "target_symbol": site.symbol if name == "timeout" else None,
            "applies_via": applies_via,
            "suggested_diff": diff,
        })
    if not options:  # all four controls present: the one option is to tune their settings
        options.append({
            "option_id": "opt-tuning", "rank": 1, "strategy": "tuning",
            "title": "Tune the existing controls", "control": "settings", "priority": Priority.LOW.value,
            "rationale": (f"{site.symbol} already has a deadline, retries, a breaker and a fallback, yet the fault "
                          "still degraded checkout; tighten the deadline, retry budget or outlier thresholds."),
            "target_file": site.file, "target_symbol": site.symbol,
            "applies_via": "tuning (code / mesh)", "suggested_diff": None,
        })
    return options


def build_recommendation(exp_dir: Path, repo: Path, plan: ExperimentPlan, rca: RootCauseAnalysis,
                         use_llm: bool = True) -> tuple[Recommendation, dict]:
    eid = plan.experiment_id
    deadline_ms = choose_deadline_ms(plan)
    audit: dict = {"experiment_id": eid, "generated_at": datetime.now(timezone.utc).isoformat(),
                   "rca_verdict": rca.verdict.value, "deadline_ms": deadline_ms, "options": []}
    common_fields = dict(experiment_id=eid, target_repository=TARGET_REPOSITORY,
                         verification_command=_verification_command(exp_dir))
    fault = describe_fault(plan)
    impact = rca.impact_summary

    if rca.verdict == RcaVerdict.STEADY_STATE_MAINTAINED:
        target = plan.target_node.service_name
        rec = Recommendation(
            **common_fields,
            recommendation_id=_recommendation_id(target, "none", eid),
            priority=Priority.INFO,
            vulnerability_type="NONE",
            summary=(f"The system tolerated {fault} on {target} ({plan.target_node.targetable_replicas} of "
                     f"{plan.target_node.replicas_total} replicas): checkout success stayed at or above "
                     f"{impact.lowest_checkout_success_rate:.2f}% and no abort threshold was breached."),
            architectural_guidance=("No code change is required. Steady state held, so the next experiment can "
                                    "widen the blast radius (a stronger fault, more replicas, or a different "
                                    "dependency on the checkout path) to look for the next weakest link."),
            suggested_diff=None,
        )
        audit["localization"] = None
        return rec, audit

    priority = Priority.HIGH if rca.verdict == RcaVerdict.ABORT_TRIGGERED else Priority.MEDIUM
    iso = rca.root_cause_isolation
    faulty = iso.faulty_service if iso else plan.target_node.service_name
    downstream = downstream_of(rca, plan, faulty)
    hint_text = " ".join([*(h.manifestation for h in rca.cascade_path),
                          iso.mechanism if iso else "", (iso.stack_trace_snippet or "") if iso else ""])
    loc: Localization = localize(repo, faulty, downstream, hint_text)
    audit["localization"] = {
        "faulty_service": faulty, "downstream": downstream, "service_dir": loc.service_dir,
        "primary": asdict(loc.primary) if loc.primary else None,
        "other_call_sites": [s.location() for s in loc.others], "notes": loc.notes,
    }

    if loc.primary is None:
        rec = Recommendation(
            **common_fields,
            recommendation_id=_recommendation_id(downstream, "review", eid),
            priority=priority,
            vulnerability_type="UNLOCALIZED_DEPENDENCY_FAILURE",
            summary=(f"{faulty} degraded under {fault} on {plan.target_node.service_name}, but the call site could "
                     f"not be located automatically ({'; '.join(loc.notes)})."),
            architectural_guidance=(f"Review how {faulty} calls {downstream or 'its dependencies'}: every outbound "
                                    "call should carry a client-side deadline, retry idempotent failures with "
                                    "backoff, and sit behind a circuit breaker or outlier detection."),
            suggested_diff=None,
        )
        return rec, audit

    site = loc.primary
    service_dir = find_service_dir(repo, faulty)
    controls = {c.name: c for c in audit_call_site(repo, site, service_dir, downstream)}
    audit["controls"] = [asdict(c) for c in controls.values()]
    missing = [n for n, c in controls.items() if not c.present]

    diff, patch_info = synthesize_patch(repo, site, controls, downstream, deadline_ms, eid, use_llm)
    audit["patch"] = patch_info
    audit["options"] = build_options(repo, site, controls, faulty, downstream, deadline_ms, eid)

    if missing:
        vuln, slug = VULNERABILITY[missing[0]]
        summary = (f"Missing {_join([LABELS[m] for m in missing])} on the {site.method or 'outbound'} call from "
                   f"{faulty} to {downstream} in {site.symbol}.")
    else:
        vuln, slug = "RESILIENCE_CONTROLS_INSUFFICIENT", "tuning"
        summary = (f"{site.symbol} in {faulty} already has a deadline, retries, a circuit breaker and a fallback, "
                   f"yet {fault} on {downstream} still degraded checkout; their settings need tuning.")
    if rca.verdict == RcaVerdict.DEGRADED_NO_ABORT and not ({"timeout", "circuit_breaker"} & set(missing)):
        priority = Priority.LOW

    rec = Recommendation(
        **common_fields,
        recommendation_id=_recommendation_id(downstream, slug, eid),
        priority=priority,
        target_file=site.file,
        target_symbol=site.symbol,
        vulnerability_type=vuln,
        summary=summary,
        architectural_guidance=_guidance(faulty, downstream, site, controls, deadline_ms, patch_info),
        suggested_diff=diff,
    )
    return rec, audit


def write_audit(exp_dir: Path, audit: dict, diff: Optional[str]) -> None:
    write_json_atomic(exp_dir / AUDIT_FILE, audit)
    patch = exp_dir / PATCH_FILE
    if diff:
        patch.write_text(diff, encoding="utf-8", newline="\n")
    elif patch.exists():
        patch.unlink()
