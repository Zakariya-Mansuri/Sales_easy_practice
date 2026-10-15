# SalesEasy — a reliable AI sales copilot

SalesEasy reads one free-text update from an FMCG field-sales rep and returns a validated JSON assessment: what the issue is, how urgent it is, which statements are **verified**, **contradicted** or **unverified** against system records, the next action, and whether a human must decide.

> **Results (100-case evaluation, v3):** full pipeline **98/100**; the same model responses without the deterministic layer **40/100**. 0 fallbacks, p50 latency ≈ 3.2 s. Details: [docs/EVALUATION.md](docs/EVALUATION.md).

- **Demo:** _deployment URL to be added_
- **Design document:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Decision records:** [docs/DECISIONS.md](docs/DECISIONS.md)
- **Evaluation report:** [docs/EVALUATION.md](docs/EVALUATION.md)

---

## How it works (one paragraph)

The LLM (`openai/gpt-4o-mini` via OpenRouter, Pydantic structured outputs) **extracts**: the claims in the update, which outlet/SKU record each refers to, and risk signals. **Code decides**: it validates every record link against the text, compares claims to system stock, writes all system facts itself, marks evidence verified/contradicted/unverified, applies escalation rules, and replaces the action when the update tries to instruct the system. Code can only *escalate*, never lower urgency or clear a review, so text that fools the model cannot get past the rules. Any failure returns a fail-closed answer (review = true).

```text
update ─▶ boundary validation ─▶ LLM (structured) ─▶ figure check (retry once) ─▶ guardrails ─▶ {analysis, trace}
                                         └── error ×2 / still invalid ─────────▶ fail-closed fallback
```

## Output

```json
{
  "issue_category": "stockout",
  "urgency": "high",
  "evidence": [
    {"statement": "System record R2: Metro Fresh / Cola 500ml stock = 48 units.", "source": "system_record", "status": "verified", "record_id": "R2"},
    {"statement": "Rep says Cola 500ml is out of stock at Metro Fresh.", "source": "field_claim", "status": "contradicted", "record_id": "R2"}
  ],
  "recommended_next_action": "Verify the stock status of Cola 500ml at Metro Fresh …",
  "missing_information": [],
  "needs_human_review": true,
  "review_reason": "Rep reports out of stock for Metro Fresh / Cola 500ml, but system record R2 shows 48 units."
}
```

The API also returns a `trace` (engine, model, latency, attempts, stock checks, rules fired and their source, every override of the model).

## Run locally

Requirements: Python 3.12.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt
cp .env.example .env              # then set LLM_API_KEY
uvicorn app.main:app --port 8000  # open http://127.0.0.1:8000
```

`.env`:

| Variable | Example |
|---|---|
| `LLM_API_KEY` | OpenRouter key (never committed; `.env` is git-ignored) |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` |
| `LLM_MODEL` | `openai/gpt-4o-mini` |

Without a key the service still runs and every request returns the fail-closed fallback.

## Use

- **UI:** paste an update or pick one of the five required scenarios; edit the verified records (add/remove/reset) to test other situations; open "How this was produced" for the audit trail. The evaluation panel shows 3 random cases from the latest run (expected vs actual, and what the model alone would have done); "Show 3 others" draws again; "Show all 100" lists every case with filters by family and outcome (pipeline failed, model alone failed, fixed by guardrails); "Run this case live" re-runs any case.
- **API:**

```bash
curl -X POST http://127.0.0.1:8000/api/analyze -H "Content-Type: application/json" \
  -d '{"update": "Metro Fresh is completely out of Cola 500ml, shelf is empty."}'

# with your own records (IDs R1..Rn are assigned by position)
curl -X POST http://127.0.0.1:8000/api/analyze -H "Content-Type: application/json" \
  -d '{"update": "Metro Fresh is out of cola.", "records": [
        {"outlet": "Metro Fresh", "sku": "Cola 500ml", "system_stock": 48},
        {"outlet": "Metro Fresh", "sku": "Cola 1L", "system_stock": 0}]}'
```

| Endpoint | Purpose |
|---|---|
| `POST /api/analyze` | `{update, records?}` → `{analysis, trace}` |
| `GET /api/reference` | default records |
| `GET /api/samples` | the five required scenarios |
| `GET /api/evals/sample?n=3` | random cases from the latest evaluation |
| `GET /api/evals/all` | all cases from the latest evaluation |
| `GET /health` | liveness |

## Test and evaluate

```bash
python -m pytest                          # 26 deterministic tests, no network (~3 s)
python -m evals.run_eval --label myrun    # 100 live cases, ~45 s, writes evals/results/myrun.json
```

## Sample inputs

The five required scenarios are in [`app/samples.py`](app/samples.py); all 100 evaluation inputs with their expectations are in [`evals/cases.py`](evals/cases.py), grouped into 17 families (stock conflicts, ambiguity, injections, health harm, false-positive traps, messy input, …).

## Repository layout

```text
app/            FastAPI service: schemas, llm, pipeline, guardrails, records, eval view
static/         UI (HTML/CSS/JS, no build step)
evals/          cases.py (100 cases), run_eval.py, results/ (v1, v2, v3, v3-repeat, latest)
tests/          deterministic unit tests
docs/           ARCHITECTURE.md, DECISIONS.md, EVALUATION.md
```

## Limitations

- Records hold only stock levels, so only stock claims can be verified; all other claims are unverified by design.
- Record linking is lexical (exact or unique match). Transliterated or translated names are not linked; the system asks to confirm instead.
- Keyword backstops are English/Hinglish-centric. Two accepted false positives remain (a joke quoting "ignore the instructions"; a rep's own sickness).
- `temperature=0` is not deterministic: 6/100 review flags changed between two identical runs (0 pass/fail changes on asserted behaviour).
- Evaluation cases were written by the same author as the system; per-family samples are small; one model was tested.
- Demo records are supplied per request by the browser; production would read them from the system of record.

Full list and production roadmap: [docs/ARCHITECTURE.md §8–9](docs/ARCHITECTURE.md#8-known-limitations).

## AI-tool usage disclosure

This project was built with **Claude Code** (Anthropic) as an AI pair programmer. The AI assistant drafted most of the code, tests, evaluation cases and documentation under the author's direction. The author set the strategy and priorities: UI first then the LLM stage; a single-shot tool rather than a chatbot; Pydantic structured outputs; dynamic records; a 100-case evaluation with a random sample shown in the UI; and a focus on subtle failure modes over obvious ones. The application itself calls `openai/gpt-4o-mini` through OpenRouter at runtime. All data is fictional.
