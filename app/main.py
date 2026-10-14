import random
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .eval_view import load_latest, present_case
from .pipeline import analyze
from .reference_data import DEFAULT_RECORDS
from .samples import SAMPLES
from .schemas import AnalyzeRequest, AnalyzeResponse

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="SalesEasy", version="0.2.0")


@app.get("/api/reference")
def reference() -> dict:
    return {"records": [r.model_dump() for r in DEFAULT_RECORDS]}


@app.get("/api/samples")
def samples() -> dict:
    return {"samples": SAMPLES}


@app.get("/api/evals/sample")
def eval_sample(n: int = Query(3, ge=1, le=10)) -> dict:
    """n random cases from the latest committed evaluation run (expected vs actual)."""
    data = load_latest()
    if data is None:
        raise HTTPException(404, "No evaluation results found. Run: python -m evals.run_eval --label v1")
    picked = random.sample(data["cases"], min(n, len(data["cases"])))
    return {"meta": data["meta"], "summary": {k: data["summary"][k] for k in ("n", "full_pass", "model_only_pass")},
            "cases": [present_case(c) for c in picked]}


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze_update(req: AnalyzeRequest) -> AnalyzeResponse:
    return analyze(req.update, req.records)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
