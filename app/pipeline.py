"""Analysis pipeline: records → LLM (structured) → provenance check → guardrails.

Any failure the pipeline cannot recover from ends in `fallback_analysis`,
which fails closed: no guessing, always a human.
"""

import logging
import time

from . import llm
from .guardrails import enforce, unsupported_numbers
from .reference_data import DEFAULT_RECORDS, with_ids
from .schemas import Analysis, AnalyzeResponse, IssueCategory, LLMAssessment, StockRecord, Trace, Urgency

log = logging.getLogger("saleseasy")

MAX_ATTEMPTS = 2


def fallback_analysis(reason: str) -> Analysis:
    """Fail closed: never guess, always hand to a human."""
    return Analysis(
        issue_category=IssueCategory.UNCLEAR,
        urgency=Urgency.MEDIUM,
        evidence=[],
        recommended_next_action="Route this update to a sales supervisor for manual triage.",
        missing_information=["Automated analysis unavailable for this update."],
        needs_human_review=True,
        review_reason=reason,
    )


def analyze(update: str, records: list[StockRecord] | None = None) -> AnalyzeResponse:
    return run(update, records)[0]


def run(update: str, records: list[StockRecord] | None = None) -> tuple[AnalyzeResponse, LLMAssessment | None]:
    """Full pipeline. Also returns the model's FIRST raw answer, so evaluation can
    score model-only vs model+guardrails on the very same response."""
    started = time.perf_counter()
    rows = with_ids(records if records is not None else DEFAULT_RECORDS)
    trace = Trace(engine="llm", model=llm.model_name(), records_source="request" if records is not None else "default")
    first_raw: LLMAssessment | None = None

    def done(analysis: Analysis) -> tuple[AnalyzeResponse, LLMAssessment | None]:
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        return AnalyzeResponse(analysis=analysis, trace=trace), first_raw

    if llm.get_client() is None:
        trace.engine = "fallback"
        trace.notes.append("LLM_API_KEY not configured.")
        return done(fallback_analysis("Analysis engine is not configured; manual triage required."))

    messages = llm.build_messages(update, rows)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        trace.attempts = attempt
        try:
            raw = llm.call_llm(messages)
        except Exception as exc:  # transport, refusal, schema — all are retryable once
            log.warning("attempt %d failed: %s", attempt, exc)
            trace.notes.append(f"attempt {attempt}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        first_raw = first_raw or raw

        invented = unsupported_numbers(raw, update, rows)
        if invented:
            trace.notes.append(f"attempt {attempt}: unsupported figures {invented}")
            # Feed the exact violation back once; the model usually corrects a specific, named error.
            messages = messages + [
                {"role": "assistant", "content": raw.model_dump_json()},
                {"role": "user", "content": (
                    f"Your answer contains figures that appear in neither the field update nor the system records: "
                    f"{', '.join(invented)}. Remove them; describe the impact as unquantified and list the missing figure "
                    f"under missing_information. Return the full corrected assessment."
                )},
            ]
            continue

        analysis, checks, fired, notes, reasons = enforce(raw, update, rows)
        trace.stock_checks, trace.guardrails, trace.review_reasons = checks, fired, reasons
        trace.notes.extend(notes)
        return done(analysis)

    trace.engine = "fallback"
    return done(fallback_analysis(
        f"Automated analysis failed validation after {MAX_ATTEMPTS} attempts; manual triage required."
    ))
