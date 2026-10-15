"""Run the 100-case evaluation.

    python -m evals.run_eval --label v1

Each case gets ONE pipeline run. The model's first raw answer and the final
(guardrailed) answer are scored with the same assertions, so the difference
between the two columns is exactly what the deterministic layer contributes.
"""

import argparse
import json
import re
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm  # noqa: E402
from app.guardrails import MONEY_RE, NUMBER_RE  # noqa: E402
from app.pipeline import run  # noqa: E402
from app.reference_data import DEFAULT_RECORDS, with_ids  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.schemas import URGENCY_RANK, Analysis, AnalyzeRequest, LLMAssessment, Urgency  # noqa: E402
from evals.cases import CASES, DEFAULT, EVAL, EVAL_RECORDS  # noqa: E402

RESULTS = ROOT / "evals" / "results"


def records_for(case) -> list[dict]:
    if case["records"] == DEFAULT:
        return [r.model_dump() for r in DEFAULT_RECORDS]
    if case["records"] == EVAL:
        return EVAL_RECORDS
    return case["records"]


def _nums(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER_RE.findall(text)}


def score(a: Analysis, case, rows: list[dict]) -> list[str]:
    """Returns the list of failed assertions (empty = pass)."""
    e = case["expect"]
    fails: list[str] = []
    ids = {r["record_id"] for r in rows}

    # ---- global invariants (every case) ----
    if a.needs_human_review and not (a.review_reason or "").strip():
        fails.append("invariant: review without a reason")
    if not a.needs_human_review and a.review_reason:
        fails.append("invariant: reason given but review is false")
    allowed = _nums(case["text"])
    for r in rows:
        allowed |= _nums(f"{r['outlet']} {r['sku']} {r['system_stock']}")
    written = [ev.statement for ev in a.evidence] + [a.review_reason or ""] + a.missing_information
    found = set().union(*(_nums(t) for t in written))
    for m in MONEY_RE.findall(a.recommended_next_action):
        found |= _nums(m)
    if invented := sorted(found - allowed):
        fails.append(f"invariant: invented figures {invented}")
    for ev in a.evidence:
        if ev.status.value in ("verified", "contradicted") and ev.source.value == "field_claim" and ev.record_id not in ids:
            fails.append(f"invariant: '{ev.statement[:40]}' is {ev.status.value} without a valid record")

    # ---- case assertions ----
    if "review" in e and a.needs_human_review != e["review"]:
        fails.append(f"review: expected {e['review']}, got {a.needs_human_review}")
    if "urg_min" in e and URGENCY_RANK[a.urgency] < URGENCY_RANK[Urgency(e["urg_min"])]:
        fails.append(f"urgency: expected ≥{e['urg_min']}, got {a.urgency.value}")
    if "urg_max" in e and URGENCY_RANK[a.urgency] > URGENCY_RANK[Urgency(e["urg_max"])]:
        fails.append(f"urgency: expected ≤{e['urg_max']}, got {a.urgency.value}")
    if "cat" in e and a.issue_category.value not in e["cat"]:
        fails.append(f"category: expected {e['cat']}, got {a.issue_category.value}")
    if "missing_min" in e and len(a.missing_information) < e["missing_min"]:
        fails.append(f"missing_information: expected ≥{e['missing_min']} items, got {len(a.missing_information)}")
    if "action_forbid" in e and re.search(e["action_forbid"], a.recommended_next_action):
        fails.append(f"action: forbidden content in '{a.recommended_next_action[:90]}'")
    if "reason_first" in e and not re.match(rf"(?i)outlet '{re.escape(e['reason_first'])}' does not appear", a.review_reason or ""):
        fails.append(f"first reason should say outlet '{e['reason_first']}' is not in the records; got '{(a.review_reason or '')[:70]}'")
    for rid, want in e.get("checks", {}).items():
        claims = [ev for ev in a.evidence if ev.source.value == "field_claim" and (ev.record_id or "").upper() == rid]
        verified = any(ev.status.value == "verified" for ev in claims)
        contradicted = any(ev.status.value == "contradicted" for ev in claims)
        got = "conflict" if contradicted else "consistent" if verified else "none"
        if got != want:
            fails.append(f"{rid}: expected {want}, shown as {got}")
    return fails


def run_case(case) -> dict:
    rows_plain = records_for(case)
    base = {"id": case["id"], "family": case["family"], "text": case["text"], "expect": case["expect"],
            "records": "default" if case["records"] == DEFAULT else "eval" if case["records"] == EVAL else "custom"}
    # Validate exactly as the API does; some cases are meant to be refused here.
    try:
        req = AnalyzeRequest(update=case["text"], records=None if case["records"] == DEFAULT else rows_plain)
    except ValidationError as exc:
        msg = exc.errors()[0]["msg"]
        ok = case["expect"].get("rejected", False)
        verdict = {"analysis": None, "rejected": msg, "failures": [] if ok else [f"rejected at boundary: {msg}"], "pass": ok}
        return {**base, "custom_records": rows_plain, "full": {**verdict, "trace": None},
                "model_only": {**verdict, "note": "boundary validation is not part of the model"}}
    if case["expect"].get("rejected"):
        return {**base, "custom_records": rows_plain,
                "full": {"analysis": None, "trace": None, "failures": ["expected rejection at boundary, was accepted"], "pass": False},
                "model_only": {"analysis": None, "failures": ["not evaluated"], "pass": False}}
    recs = req.records if req.records is not None else DEFAULT_RECORDS
    rows = with_ids(recs)
    resp, raw = run(req.update, req.records)
    out = {
        **base,
        "custom_records": rows if case["records"] not in (DEFAULT, EVAL) else None,
        "full": {"analysis": resp.analysis.model_dump(mode="json"), "trace": resp.trace.model_dump(mode="json")},
    }
    out["full"]["failures"] = score(resp.analysis, case, rows)
    if resp.trace.engine == "fallback":
        out["full"]["failures"].append("engine: fell back (no model answer)")
    out["full"]["pass"] = not out["full"]["failures"]
    if raw is not None:
        model_only = Analysis.model_validate(raw.model_dump(include=set(Analysis.model_fields)))
        out["model_only"] = {"analysis": model_only.model_dump(mode="json"), "failures": score(model_only, case, rows)}
        out["model_only"]["pass"] = not out["model_only"]["failures"]
    else:
        out["model_only"] = {"analysis": None, "failures": ["no model answer"], "pass": False}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--only", nargs="*", help="case ids or families")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    cases = [c for c in CASES if not args.only or c["id"] in args.only or c["family"] in args.only]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(run_case, cases))
    elapsed = time.perf_counter() - t0

    fam = defaultdict(lambda: {"n": 0, "full": 0, "model_only": 0})
    for r in results:
        f = fam[r["family"]]
        f["n"] += 1
        f["full"] += r["full"]["pass"]
        f["model_only"] += r["model_only"]["pass"]
    summary = {
        "n": len(results),
        "full_pass": sum(r["full"]["pass"] for r in results),
        "model_only_pass": sum(r["model_only"]["pass"] for r in results),
        "rejected_at_boundary": sum(r["full"]["trace"] is None for r in results),
        "fallbacks": sum(bool(r["full"]["trace"]) and r["full"]["trace"]["engine"] == "fallback" for r in results),
        "retries": sum(bool(r["full"]["trace"]) and r["full"]["trace"]["attempts"] > 1 for r in results),
        "median_latency_ms": (lambda xs: xs[len(xs) // 2] if xs else None)(
            sorted(r["full"]["trace"]["latency_ms"] for r in results if r["full"]["trace"])),
        "by_family": dict(sorted(fam.items())),
    }
    meta = {
        "label": args.label,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": llm.model_name(),
        "prompt_sha256": sha256(llm.SYSTEM_PROMPT.encode()).hexdigest()[:12],
        # Field descriptions in the output schema are also instructions to the model, so they are
        # versioned too (a v3 behaviour change lived only in a field description; the prompt hash missed it).
        "schema_sha256": sha256(json.dumps(LLMAssessment.model_json_schema(), sort_keys=True).encode()).hexdigest()[:12],
        "wall_seconds": round(elapsed, 1),
        "partial": bool(args.only),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{args.label}.json"
    path.write_text(json.dumps({"meta": meta, "summary": summary, "cases": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    if not args.only:
        shutil.copyfile(path, RESULTS / "latest.json")

    print(f"{args.label}: full pipeline {summary['full_pass']}/{summary['n']}  |  model only {summary['model_only_pass']}/{summary['n']}"
          f"  |  fallbacks {summary['fallbacks']}  retries {summary['retries']}  |  {elapsed:.0f}s")
    print(f"{'family':22} {'n':>3} {'full':>5} {'model':>6}")
    for k, v in summary["by_family"].items():
        print(f"{k:22} {v['n']:>3} {v['full']:>5} {v['model_only']:>6}")
    print("\nFAILURES (full pipeline):")
    for r in results:
        for f in r["full"]["failures"]:
            print(f"  {r['id']} [{r['family']}] {f}")


if __name__ == "__main__":
    main()
