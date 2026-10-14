"""Read-only view of the latest evaluation run, for the UI panel."""

import json
from functools import lru_cache
from pathlib import Path

from evals.cases import COMMIT, DEFAULT, EVAL, EVAL_RECORDS, NO_ACTION

from .reference_data import DEFAULT_RECORDS

LATEST = Path(__file__).resolve().parent.parent / "evals" / "results" / "latest.json"
_CHECK_WORD = {"consistent": "verified", "conflict": "contradicted", "none": "not linked to a record"}


@lru_cache(maxsize=4)
def _load(mtime: float) -> dict:
    return json.loads(LATEST.read_text(encoding="utf-8"))


def load_latest() -> dict | None:
    return _load(LATEST.stat().st_mtime) if LATEST.exists() else None


def expectation_text(e: dict) -> list[str]:
    out = []
    if e.get("rejected"):
        out.append("rejected at input validation")
    if "review" in e:
        out.append("needs human review" if e["review"] else "no human review")
    if "urg_min" in e:
        out.append(f"urgency ≥ {e['urg_min']}")
    if "urg_max" in e:
        out.append(f"urgency ≤ {e['urg_max']}")
    if "cat" in e:
        out.append("category: " + " or ".join(c.replace("_", " ") for c in e["cat"]))
    for rid, want in e.get("checks", {}).items():
        out.append(f"{rid} {_CHECK_WORD[want]}")
    if "missing_min" in e:
        out.append("asks for missing information")
    if e.get("action_forbid") == COMMIT:
        out.append("no commercial commitment")
    elif e.get("action_forbid") == NO_ACTION:
        out.append("does not close the case")
    return out


def _records(c: dict) -> list[dict]:
    if c["records"] == DEFAULT:
        return [r.model_dump() for r in DEFAULT_RECORDS]
    if c["records"] == EVAL:
        return EVAL_RECORDS
    return [{k: r[k] for k in ("outlet", "sku", "system_stock")} for r in c.get("custom_records") or []]


def _verdict(side: dict) -> dict:
    a = side.get("analysis")
    return {
        "pass": side["pass"],
        "failures": side["failures"],
        "rejected": side.get("rejected"),
        "review": a and a["needs_human_review"],
        "urgency": a and a["urgency"],
        "category": a and a["issue_category"],
        "action": a and a["recommended_next_action"],
    }


def present_case(c: dict) -> dict:
    return {
        "id": c["id"],
        "family": c["family"],
        "text": c["text"],
        "records": _records(c),
        "records_label": c["records"],
        "expected": expectation_text(c["expect"]),
        "full": _verdict(c["full"]),
        "model_only": _verdict(c["model_only"]),
    }
