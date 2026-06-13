"""FastAPI application: World Cup match list + probability analysis."""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.analysis import accuracy, engine
from app.analysis.gemini import GeminiUnavailable, analyze_with_gemini
from app.config import get_settings
from app.models import AnalyzeUrlRequest
from app.scraper import endpoints, fixtures
from app.scraper.aggregator import _epoch, build_dataset
from app.scraper.client import SofaScoreError
from app.scraper.url import parse_event_id

STATIC_DIR = Path(__file__).parent / "static"

# Served at the domain root. ``app = betstats`` is set at the bottom for uvicorn.
betstats = FastAPI(title="Bet Stats — World Cup Match Analyzer", version="1.0")
betstats.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@betstats.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@betstats.get("/api/config")
async def api_config():
    return {"geminiEnabled": get_settings().gemini_enabled}


@betstats.get("/api/matches")
async def api_matches():
    try:
        groups = await run_in_threadpool(fixtures.list_world_cup_matches)
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"SofaScore unavailable: {exc}")
    return {"days": groups}


_finished_cache: dict[int, dict] = {}
_finished_lock = threading.Lock()


def _analyze_sync(event_id: int, force_lineups: bool = False) -> dict:
    if not force_lineups:
        with _finished_lock:
            cached = _finished_cache.get(event_id)
        if cached is not None:
            return cached

    md = endpoints.match_details(event_id, force=force_lineups)
    if not isinstance(md, dict) or not md.get("header"):
        raise ValueError(f"Match {event_id} not found")
    status = (md.get("header") or {}).get("status") or {}
    finished = bool(status.get("finished"))
    # For finished matches, predict from pre-match form only (exclude this match
    # and anything after it) so the accuracy comparison is fair.
    before_ts = _epoch(status.get("utcTime")) if finished else None

    dataset = build_dataset(event_id, force_lineups=force_lineups, before_ts=before_ts)
    result = engine.analyze(dataset)
    if finished:
        actuals = accuracy.extract_actuals(md)
        result["accuracy"] = accuracy.evaluate(result, actuals)
        with _finished_lock:  # finished matches are immutable -> cache forever
            _finished_cache[event_id] = result
    return result


async def _analyze(event_id: int, force_lineups: bool = False) -> dict:
    return await run_in_threadpool(_analyze_sync, event_id, force_lineups)


@betstats.get("/api/analyze/{event_id}")
async def api_analyze(event_id: int):
    try:
        return await _analyze(event_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"SofaScore unavailable: {exc}")


@betstats.post("/api/analyze")
async def api_analyze_url(req: AnalyzeUrlRequest):
    try:
        event_id = parse_event_id(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await api_analyze(event_id)


@betstats.post("/api/refresh/{event_id}")
async def api_refresh(event_id: int):
    """Force-refresh lineups and recompute. Player props become final once confirmed."""
    try:
        result = await _analyze(event_id, force_lineups=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"SofaScore unavailable: {exc}")
    return result


_CALIB_EDGES = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]


def _dashboard_sync() -> dict:
    days = fixtures.list_world_cup_matches()
    finished = [m for d in days for m in d["matches"] if m["statusType"] == "finished"]

    per_match = []
    all_calls: list[dict] = []
    result_correct = 0
    brier_sum = 0.0
    evaluated = 0

    for m in finished:
        try:
            a = _analyze_sync(m["id"]).get("accuracy")
        except Exception:
            continue
        if not a:
            continue
        evaluated += 1
        s = a["summary"]
        result_correct += 1 if s["resultCorrect"] else 0
        brier_sum += s["brier"]
        all_calls.extend(a.get("calls", []))
        per_match.append({
            "id": m["id"], "home": m["home"], "away": m["away"], "date": m["date"],
            "score": a["result"]["scoreStr"], "resultPick": a["result"]["predicted"],
            "resultCorrect": s["resultCorrect"], "marketHitRate": s["marketHitRate"],
        })

    # by-market aggregation
    by_market: dict[str, dict] = {}
    for c in all_calls:
        b = by_market.setdefault(c["market"], {"hits": 0, "total": 0})
        b["total"] += 1
        b["hits"] += 1 if c["correct"] else 0
    for b in by_market.values():
        b["rate"] = round(b["hits"] / b["total"], 3) if b["total"] else None

    # calibration buckets
    calibration = []
    for i in range(len(_CALIB_EDGES) - 1):
        lo, hi = _CALIB_EDGES[i], _CALIB_EDGES[i + 1]
        bucket = [c for c in all_calls if lo <= c["conf"] < hi]
        if not bucket:
            continue
        calibration.append({
            "range": f"{int(lo*100)}–{int(min(hi,1.0)*100)}%",
            "n": len(bucket),
            "predicted": round(sum(c["conf"] for c in bucket) / len(bucket), 3),
            "actual": round(sum(1 for c in bucket if c["correct"]) / len(bucket), 3),
        })

    total_calls = len(all_calls)
    total_hits = sum(1 for c in all_calls if c["correct"])
    return {
        "matchesEvaluated": evaluated,
        "result": {
            "correct": result_correct, "total": evaluated,
            "accuracy": round(result_correct / evaluated, 3) if evaluated else None,
            "avgBrier": round(brier_sum / evaluated, 4) if evaluated else None,
        },
        "markets": {
            "overall": {"hits": total_hits, "total": total_calls,
                        "rate": round(total_hits / total_calls, 3) if total_calls else None},
            "byMarket": by_market,
        },
        "calibration": calibration,
        "perMatch": per_match,
    }


@betstats.get("/api/dashboard")
async def api_dashboard():
    try:
        return await run_in_threadpool(_dashboard_sync)
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"Data source unavailable: {exc}")


@betstats.post("/api/gemini/{event_id}")
async def api_gemini(event_id: int):
    try:
        dataset = await run_in_threadpool(build_dataset, event_id)
        engine_output = await run_in_threadpool(engine.analyze, dataset)
        analysis = await run_in_threadpool(analyze_with_gemini, dataset, engine_output)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except GeminiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"SofaScore unavailable: {exc}")
    return JSONResponse(analysis)


# App served at the domain root (compubot.online is dedicated to it).
app = betstats
