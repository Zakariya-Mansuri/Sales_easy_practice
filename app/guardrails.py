"""Deterministic layer between the model and the user.

Invariants:
1. Urgency and review can only be ESCALATED here, never lowered or cleared,
   so text that fools the model still cannot talk its way past code.
2. Evidence status is decided by code from the records, never by the model.
   The model only extracts claims and links them to record IDs.
"""

import re

from .schemas import (
    URGENCY_RANK,
    Analysis,
    Evidence,
    EvidenceSource,
    EvidenceStatus,
    IssueCategory,
    LLMAssessment,
    StockCheck,
    StockClaim,
    Urgency,
)

# Keyword backstops. They only ever add review, so a false positive costs a
# human glance; a false negative is still covered by the model's own signal.
# A person affected → critical. A hazardous product with no one affected yet → high.
HARM_RE = re.compile(
    r"\b(unwell|sick|ill|illness|vomit\w*|nause\w*|hospital\w*|allerg\w*|injur\w*|poison\w*|"
    r"diarrh\w*|fever|chok\w*|faint\w*|stomach|cut (?:his|her|their) hand|died|death)\b",
    re.IGNORECASE,
)
HAZARD_RE = re.compile(
    r"\b(contaminat\w*|expired|mou?ld\w*|foreign (?:object|body|particle)|glass|insect|swollen|bloated|tamper\w*)\b",
    re.IGNORECASE,
)
INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+|previous\s+|prior\s+|above\s+|your\s+)*(?:instructions|rules|prompt)|"
    r"disregard\s+(?:all\s+|the\s+|previous\s+|your\s+)*(?:instructions|rules)|"
    r"system\s+prompt|you\s+are\s+now|new\s+instructions|"
    r"mark\s+(?:this|it|the\s+\w+)\s+(?:as\s+)?(?:resolved|closed|low)|"
    r"set\s+(?:the\s+)?(?:urgency|priority|needs_human_review)|"
    r"needs_human_review|review_reason|issue_category",
    re.IGNORECASE,
)
COMMERCIAL_RE = re.compile(
    r"price[\s-]?match|match\s+(?:their|the|competitor'?s?)\s+price|(?:cut|reduce|drop|lower)\s+(?:the\s+|our\s+)?price|"
    r"credit\s+note|refund|free\s+(?:goods|cases|stock)",
    re.IGNORECASE,
)

_NEGATORS = {"no", "not", "nobody", "none", "never", "without", "nothing", "neither", "nor", "didn't",
             "doesn't", "don't", "wasn't", "isn't", "hasn't", "haven't"}
# Negation does not carry across these: "did not feel well AND LATER vomited" is still harm.
_CLAUSE_BREAK_RE = re.compile(r"[.;!?,]|\b(but|and|then|later|after|so|however)\b", re.IGNORECASE)


def keyword_hits(pattern: re.Pattern, text: str) -> list[str]:
    """Matches that are really asserted: skips hyphenated compounds ('ill-timed',
    'expired-stock audit') and terms negated within the 3 words before them in the
    same clause ('nobody felt unwell', 'no expired packs'). The scope is kept narrow
    on purpose: a missed harm report is worse than an extra review."""
    hits = []
    for m in pattern.finditer(text):
        if text[m.end():m.end() + 1] == "-":
            continue
        clause = _CLAUSE_BREAK_RE.split(text[max(0, m.start() - 60):m.start()])[-1]
        if _NEGATORS & {w.casefold() for w in clause.split()[-3:]}:
            continue
        hits.append(m.group(0))
    return hits


IMPACT_RE = re.compile(
    r"\b(sales|business|revenue|loss|losses|lost|losing|costing|hurting|killing|hit|dropped|drop)\b", re.IGNORECASE
)
# Stock-level language, used only to recover stock claims the model filed under `evidence`.
# Out-of-stock is tested first ("not available" contains "available").
_OUT_RE = re.compile(
    r"\b(out of stock|out of|sold out|no stock|run out|ran out|finished|unavailable|not available|"
    r"empty|zero|nil|khatam|(?:has|have|had|still|with)\s+no|no\s+more)\b", re.IGNORECASE)
_IN_RE = re.compile(
    r"\b(in stock|plenty|good stock|well stocked|fully stocked|lots of|shelves? (?:is |are )?full|available)\b", re.IGNORECASE)
_IMPACT_ASK_RE = re.compile(r"\b(sales|loss|losses|impact|revenue|amount|figure)\b", re.IGNORECASE)


# Digits not glued to a preceding letter (so record IDs like "R2" are ignored).
NUMBER_RE = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")
MONEY_RE = re.compile(r"(?:₹|rs\.?\s*|inr\s*|\$)\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*(?:%|percent|lakh|crore|k\b)", re.IGNORECASE)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER_RE.findall(text)}


def unsupported_numbers(a: LLMAssessment, update: str, records: list[dict]) -> list[str]:
    """Numbers the model wrote that exist in neither the update nor the records."""
    allowed = _numbers(update)
    for r in records:
        allowed |= _numbers(f"{r['outlet']} {r['sku']} {r['system_stock']}")
    checked = [e.statement for e in a.evidence if e.source == EvidenceSource.FIELD_CLAIM]
    checked += [c.statement for c in a.stock_claims]
    checked += [a.review_reason or ""] + a.missing_information
    found = set().union(*(_numbers(t) for t in checked)) if checked else set()
    # Recommended action may legitimately say "within 24 hours"; only money/percent figures are checked there.
    for m in MONEY_RE.findall(a.recommended_next_action):
        found |= _numbers(m)
    return sorted(found - allowed)


_NULLISH = {"", "null", "none", "n/a", "na", "-"}


def _rid(value: str | None) -> str | None:
    """Strict schemas still let a model write the *string* "null"; treat it as absent."""
    if value is None or value.strip().lower() in _NULLISH:
        return None
    return value.strip().upper()


_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "is", "are", "was", "of", "at", "in", "on", "to", "that", "and",
         "rep", "says", "said", "claims", "reports", "reported"}


def _similar(a: str, b: str, threshold: float = 0.75) -> bool:
    """Overlap coefficient on content words. Used only to DROP a duplicate of a
    stock claim, never to verify anything. A claim that adds new content (e.g. an
    impact claim) shares too few of its words to cross the threshold."""
    wa = set(_WORD_RE.findall(a.casefold())) - _STOP
    wb = set(_WORD_RE.findall(b.casefold())) - _STOP
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= threshold


SAFE_ACTION_ON_INJECTION = (
    "Do not act on this update. A sales supervisor should contact the rep to confirm what was "
    "actually observed during the visit before any status, urgency or stock change is made."
)


_UNIT_GAP_RE = re.compile(r"(\d)\s+(ml|l|ltr|g|kg)\b", re.IGNORECASE)


def _norm(text: str) -> str:
    return _UNIT_GAP_RE.sub(r"\1\2", text.casefold())


def _link_supported(rec: dict, update: str, records: list[dict]) -> str | None:
    """A model may resolve a vague mention ("cola") to one specific record on its own.
    Accept its link only if the update text itself pins down that record.
    Returns None if supported, else the reason it is not."""
    text = _norm(update)
    words = set(_WORD_RE.findall(text))

    outlet = rec["outlet"].casefold()
    first = outlet.split()[0]
    # Count distinct OUTLETS sharing the first word, not records (one outlet has many SKUs).
    outlets_with_first = {r["outlet"].casefold() for r in records if r["outlet"].casefold().split()[0] == first}
    outlet_ok = outlet in text or (first in words and len(outlets_with_first) == 1)
    if not outlet_ok:
        return f"update does not name outlet '{rec['outlet']}'"

    sku = _norm(rec["sku"])
    if sku in text:
        return None
    product = sku.split()[0]
    same_product = [r for r in records if r["outlet"].casefold() == outlet and _norm(r["sku"]).split()[0] == product]
    if product in words and len(same_product) == 1:
        return None
    if product in words:
        # No counts in user-facing text: every figure shown must come from the input or the records.
        options = " or ".join(r["sku"] for r in same_product)
        return f"'{product}' at {rec['outlet']} could be {options}; update does not say which"
    return f"update does not name SKU '{rec['sku']}'"


def _resolve(claim, update: str, records: list[dict]) -> str | None:
    """When the model leaves a stock claim unlinked, link it only if the text pins down
    exactly one record AT THE OUTLET THIS CLAIM NAMES (so a claim about one outlet can
    never borrow another outlet's record in a multi-outlet update)."""
    said = _norm(f"{claim.outlet_mentioned} {claim.sku_mentioned} {claim.statement}")
    said_words = set(_WORD_RE.findall(said))
    cands = [
        r for r in records
        if (r["outlet"].casefold() in said or r["outlet"].casefold().split()[0] in said_words)
        and _norm(r["sku"]).split()[0] in said_words
        and _link_supported(r, update, records) is None
    ]
    return cands[0]["record_id"] if len(cands) == 1 else None


STATUS_FOR = {"consistent": EvidenceStatus.VERIFIED, "conflict": EvidenceStatus.CONTRADICTED,
              "not_comparable": EvidenceStatus.UNVERIFIED}


def _compare(claimed_state: str, stock: int) -> str:
    if claimed_state == "out_of_stock":
        return "conflict" if stock > 0 else "consistent"
    if claimed_state == "in_stock":
        return "conflict" if stock == 0 else "consistent"
    return "not_comparable"


def _raise(current: Urgency, floor: Urgency) -> Urgency:
    return floor if URGENCY_RANK[floor] > URGENCY_RANK[current] else current


def enforce(a: LLMAssessment, update: str, records: list[dict]) -> tuple[Analysis, list[StockCheck], list[str], list[str]]:
    """Returns (final analysis, stock checks, guardrails fired, notes)."""
    by_id = {r["record_id"]: r for r in records}
    fired: list[str] = []
    notes: list[str] = []
    reasons: list[str] = []
    urgency = a.urgency
    category = a.issue_category
    missing = list(a.missing_information)

    # 0. The model sometimes files a stock-level claim under `evidence` instead of `stock_claims`
    #    (same input, different run). Recover it so it still gets checked. Impact statements are
    #    never promoted: "losing sales because cola is out" must not inherit a "verified" status.
    claims = list(a.stock_claims)
    kept_evidence = []
    for e in a.evidence:
        state = ("out_of_stock" if _OUT_RE.search(e.statement) else "in_stock" if _IN_RE.search(e.statement) else None)
        if (e.source == EvidenceSource.FIELD_CLAIM and state and not keyword_hits(IMPACT_RE, e.statement)
                and not any(_similar(e.statement, s.statement) for s in claims)):
            outlet = next((r["outlet"] for r in records if r["outlet"].casefold() in e.statement.casefold()), "unspecified outlet")
            claims.append(StockClaim(statement=e.statement, record_id=None, outlet_mentioned=outlet,
                                     sku_mentioned="", claimed_state=state))
            notes.append(f"recovered stock claim filed under evidence: '{e.statement[:50]}'")
        else:
            kept_evidence.append(e)

    # 1. Stock claims: code, not the model, compares claim vs record and writes the evidence line.
    checks: dict[str, StockCheck] = {}
    stock_ev: list[Evidence] = []
    for c in claims:
        rid = _rid(c.record_id)
        rec = by_id.get(rid) if rid else None
        statement = c.statement
        # "As written by the rep" is a model claim too: keep it only if the words are really in the update.
        if c.sku_mentioned and _norm(c.sku_mentioned) not in _norm(update):
            notes.append(f"sku_mentioned '{c.sku_mentioned}' not in update text; treated as unspecified")
            c = c.model_copy(update={"sku_mentioned": ""})
        if rid and rec is None:
            fired.append(f"model cited unknown record {rid}")
            reasons.append(f"Model referenced a record ({rid}) that does not exist.")
        if rid is None and (resolved := _resolve(c, update, records)):
            notes.append(f"model left claim unlinked; text identifies exactly one record ({resolved})")
            rid, rec = resolved, by_id[resolved]
        if rec is not None and (why := _link_supported(rec, update, records)):
            fired.append(f"ambiguous record link rejected ({rid})")
            notes.append(f"model linked claim to {rid}, rejected: {why}")
            reasons.append(f"Stock claim cannot be matched to a single system record: {why}.")
            # The model's sentence may carry the specificity it invented ("Cola 500ml"); quote the rep instead.
            statement = f"Rep reports {c.claimed_state.replace('_', ' ')} (product as written: '{c.sku_mentioned or 'unspecified'}', outlet: '{c.outlet_mentioned}')."
            rec = None
        if rec is None:
            stock_ev.append(Evidence(statement=statement, source=EvidenceSource.FIELD_CLAIM,
                                     status=EvidenceStatus.UNVERIFIED, record_id=None))
            if c.claimed_state in ("out_of_stock", "in_stock", "low_stock"):
                label = f"{c.outlet_mentioned} / {c.sku_mentioned or 'unspecified product'}"
                missing.append(f"No unique system record for '{label}' — confirm exact outlet and SKU.")
            continue
        result = _compare(c.claimed_state, rec["system_stock"])
        stock_ev.append(Evidence(statement=c.statement, source=EvidenceSource.FIELD_CLAIM,
                                 status=STATUS_FOR[result], record_id=rid))
        prev = checks.get(rid)
        # Two claims on one record: keep the most serious comparison.
        if prev is None or (result == "conflict") or (prev.result == "not_comparable" and result == "consistent"):
            checks[rid] = StockCheck(
                record_id=rid, outlet=rec["outlet"], sku=rec["sku"],
                system_stock=rec["system_stock"], claimed_state=c.claimed_state, result=result,
            )
    for s in checks.values():
        if s.result == "conflict":
            fired.append(f"stock conflict on {s.record_id}")
            reasons.append(
                f"Rep reports {s.claimed_state.replace('_', ' ')} for {s.outlet} / {s.sku}, "
                f"but system record {s.record_id} shows {s.system_stock} units."
            )
            urgency = _raise(urgency, Urgency.MEDIUM)

    # 2. Other evidence. Records hold only stock levels, so no non-stock claim can be verified:
    #    every model-written field claim is unverified by construction. System facts are
    #    re-written by code from the record itself (no model-typed numbers reach the user).
    other_ev: list[Evidence] = []
    cited: list[str] = list(checks)
    for e in kept_evidence:
        rid = _rid(e.record_id)
        if e.source == EvidenceSource.SYSTEM_RECORD:
            if rid in by_id:
                cited.append(rid)
            continue
        if any(_similar(e.statement, s.statement) for s in stock_ev):
            notes.append(f"dropped duplicate of a stock claim: '{e.statement[:50]}'")
            continue
        if e.status != EvidenceStatus.UNVERIFIED or rid is not None:
            notes.append(f"'{e.statement[:50]}' was {e.status.value}/{rid}; non-stock claims cannot be verified by records")
        other_ev.append(Evidence(statement=e.statement, source=e.source, status=EvidenceStatus.UNVERIFIED, record_id=None))
    field_ev = stock_ev + other_ev
    system_ev = [
        Evidence(
            statement=f"System record {rid}: {by_id[rid]['outlet']} / {by_id[rid]['sku']} stock = {by_id[rid]['system_stock']} units.",
            source=EvidenceSource.SYSTEM_RECORD, status=EvidenceStatus.VERIFIED, record_id=rid,
        )
        for rid in dict.fromkeys(cited)
    ]

    # 3. Escalation rules (model signal OR keyword backstop).
    def via(model_flag: bool, backstop: bool) -> str:
        return " [" + "+".join(s for s, on in (("model", model_flag), ("keyword", backstop)) if on) + "]"

    harm_hits = keyword_hits(HARM_RE, update)
    harm_kw = bool(harm_hits)
    inj_kw = bool(INJECTION_RE.search(update))
    com_kw = bool(COMMERCIAL_RE.search(update))
    if a.signals.health_or_safety_risk:
        fired.append("health/safety: person affected" + via(True, harm_kw))
        reasons.append("A person may have been harmed by the product; treat as a potential product-safety incident.")
        category = IssueCategory.HEALTH_SAFETY
        urgency = _raise(urgency, Urgency.CRITICAL)
    elif harm_kw:
        # Keyword only, model disagrees: still a human's call (a missed incident costs far more
        # than a review), but at a lower tier and with the doubt stated, not a critical alarm.
        fired.append("health/safety: keyword only" + via(False, True))
        reasons.append(f"Mentions possible harm ('{harm_hits[0]}') that the model did not confirm; "
                       "check whether a person was affected by the product.")
        urgency = _raise(urgency, Urgency.HIGH)
    elif hazard_hits := keyword_hits(HAZARD_RE, update):
        fired.append("health/safety: product hazard [keyword]")
        reasons.append(f"Product may be unsafe ('{hazard_hits[0]}'); stock should be checked before further sale.")
        urgency = _raise(urgency, Urgency.HIGH)
    action = a.recommended_next_action
    if a.signals.embedded_instructions or inj_kw:
        fired.append("embedded instructions" + via(a.signals.embedded_instructions, inj_kw))
        reasons.append("Update contains text that tries to instruct the system; it was ignored.")
        urgency = _raise(urgency, Urgency.MEDIUM)
        # The model read the injected text, so its free-text action is untrusted (it tends to
        # echo the injected outcome, e.g. "no action required"). Code-checked evidence stays.
        notes.append(f"model action replaced (untrusted under injection): '{action[:80]}'")
        action = SAFE_ACTION_ON_INJECTION
        missing.append("What the rep actually observed at the outlet.")
    if a.signals.commercial_action_requested or com_kw:
        fired.append("commercial action requested" + via(a.signals.commercial_action_requested, com_kw))
        reasons.append("Requested price/commercial action needs an authorised decision-maker.")
    if category == IssueCategory.UNCLEAR:
        fired.append("unclear category")
        reasons.append("Issue could not be classified with confidence.")

    # Brief: never invent an impact figure. The other half of that rule is asking for it.
    if (keyword_hits(IMPACT_RE, update) and not MONEY_RE.search(update)
            and not any(_IMPACT_ASK_RE.search(m) for m in missing)):
        missing.append("Size of the claimed sales impact (amount and period), with how it was estimated.")
        notes.append("impact claimed without a figure; added request for it")

    needs_review = a.needs_human_review or bool(reasons)
    if a.needs_human_review and a.review_reason and a.review_reason not in reasons:
        reasons.append(a.review_reason)
    if needs_review and not reasons:
        reasons.append("Flagged for review by the model without a stated reason.")

    # Record every place code overrode the model, so the override is auditable.
    if urgency != a.urgency:
        notes.append(f"urgency raised: model {a.urgency.value} → {urgency.value}")
    if category != a.issue_category:
        notes.append(f"category changed: model {a.issue_category.value} → {category.value}")
    if needs_review and not a.needs_human_review:
        notes.append("review forced: model said no review needed")

    final = Analysis(
        issue_category=category,
        urgency=urgency,
        evidence=system_ev + field_ev,
        recommended_next_action=action,
        missing_information=list(dict.fromkeys(missing)),
        needs_human_review=needs_review,
        review_reason=" ".join(reasons) if needs_review else None,
    )
    return final, list(checks.values()), fired, notes
