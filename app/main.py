"""FastAPI application: World Cup match list + probability analysis."""
from __future__ import annotations

import threading
import time
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


_finished_cache: dict[int, dict] = {}        # finished matches: immutable -> cache forever
_upcoming_cache: dict[int, tuple] = {}       # upcoming: (timestamp, result) with TTL
_UPCOMING_TTL = 1800                         # 30 min
_finished_lock = threading.Lock()


def _analyze_sync(event_id: int, force_lineups: bool = False) -> dict:
    if not force_lineups:
        with _finished_lock:
            cached = _finished_cache.get(event_id)
            if cached is None:
                up = _upcoming_cache.get(event_id)
                if up and (time.time() - up[0]) < _UPCOMING_TTL:
                    cached = up[1]
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
    with _finished_lock:
        if finished:
            actuals = accuracy.extract_actuals(md)
            result["accuracy"] = accuracy.evaluate(result, actuals)
            _finished_cache[event_id] = result
        else:
            _upcoming_cache[event_id] = (time.time(), result)
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


def _finished_accuracy() -> dict:
    """Aggregate accuracy across all finished matches (shared by dashboard + reliability)."""
    days = fixtures.list_world_cup_matches()
    finished = [m for d in days for m in d["matches"] if m["statusType"] == "finished"]

    per_match, all_calls = [], []
    result_correct, brier_sum, evaluated = 0, 0.0, 0
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

    by_market: dict[str, dict] = {}
    for c in all_calls:
        b = by_market.setdefault(c["market"], {"hits": 0, "total": 0})
        b["total"] += 1
        b["hits"] += 1 if c["correct"] else 0
    for b in by_market.values():
        b["rate"] = round(b["hits"] / b["total"], 3) if b["total"] else None

    return {"per_match": per_match, "all_calls": all_calls, "by_market": by_market,
            "result_correct": result_correct, "brier_sum": brier_sum, "evaluated": evaluated}


def _market_reliability() -> dict:
    fa = _finished_accuracy()
    return {"byMarket": fa["by_market"], "matchesEvaluated": fa["evaluated"]}


def _dashboard_sync() -> dict:
    fa = _finished_accuracy()
    all_calls, evaluated = fa["all_calls"], fa["evaluated"]

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
            "correct": fa["result_correct"], "total": evaluated,
            "accuracy": round(fa["result_correct"] / evaluated, 3) if evaluated else None,
            "avgBrier": round(fa["brier_sum"] / evaluated, 4) if evaluated else None,
        },
        "markets": {
            "overall": {"hits": total_hits, "total": total_calls,
                        "rate": round(total_hits / total_calls, 3) if total_calls else None},
            "byMarket": fa["by_market"],
        },
        "calibration": calibration,
        "perMatch": fa["per_match"],
    }


@betstats.get("/api/dashboard")
async def api_dashboard():
    try:
        return await run_in_threadpool(_dashboard_sync)
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"Data source unavailable: {exc}")


@betstats.get("/api/reliability")
async def api_reliability():
    """Per-market historical hit-rate, for inline reliability badges."""
    try:
        return await run_in_threadpool(_market_reliability)
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"Data source unavailable: {exc}")


# ---- best bets board ----
_BESTBETS_TTL = 600           # 10 min
_BESTBETS_WINDOW = 48 * 3600  # consider matches kicking off within 48h
_BESTBETS_MAX_MATCHES = 16    # bound first-load latency
_BESTBETS_MAX_PER_CATEGORY = 5  # cap per market so no category floods the board
_REL_SHRINK_K = 20            # pulls small-sample hit-rates toward the baseline
_REL_BASELINE = 0.5
_bestbets_cache: dict = {"ts": 0.0, "data": None}


def _adjusted_reliability(entry: dict | None) -> float:
    """Sample-size-shrunk hit-rate: tiny samples pulled toward 0.5."""
    if not entry or not entry.get("total"):
        return _REL_BASELINE
    return (entry["hits"] + _REL_SHRINK_K * _REL_BASELINE) / (entry["total"] + _REL_SHRINK_K)


def _best_bets_sync() -> dict:
    now = time.time()
    if _bestbets_cache["data"] and (now - _bestbets_cache["ts"]) < _BESTBETS_TTL:
        return _bestbets_cache["data"]

    days = fixtures.list_world_cup_matches()
    upcoming = [m for d in days for m in d["matches"]
                if m["statusType"] in ("notstarted", "inprogress")
                and m.get("startTimestamp")
                and (now - 3 * 3600) <= m["startTimestamp"] <= (now + _BESTBETS_WINDOW)]
    upcoming.sort(key=lambda m: m["startTimestamp"])
    upcoming = upcoming[:_BESTBETS_MAX_MATCHES]

    rel = _market_reliability()["byMarket"]
    bets = []
    for m in upcoming:
        try:
            a = _analyze_sync(m["id"])
        except Exception:
            continue
        for p in a.get("predictions", []):
            entry = rel.get(p.get("category"))
            r = entry.get("rate") if entry else None
            # Aggressive cross-reference: weight by reliability SQUARED (sample-adjusted),
            # so proven markets dominate and weak ones (e.g. 1X2) sink.
            adj = _adjusted_reliability(entry)
            score = round(p["prob"] * adj * adj, 4)
            bets.append({
                "matchId": m["id"], "home": m["home"], "away": m["away"],
                "time": m["time"], "date": m["date"], "startTimestamp": m.get("startTimestamp"),
                "market": p["market"], "category": p.get("category"),
                "selection": p["selection"], "prob": p["prob"],
                "expected": p.get("expected"), "reliability": r, "score": score,
            })

    bets.sort(key=lambda x: x["score"], reverse=True)
    # Cap per category AND per match so the board stays varied (no single market or
    # game floods it) -> guarantees several categories are represented.
    final, cat_count, match_count = [], {}, {}
    for b in bets:
        c, mid = b["category"], b["matchId"]
        if cat_count.get(c, 0) >= _BESTBETS_MAX_PER_CATEGORY:
            continue
        if match_count.get(mid, 0) >= 6:  # at most 6 picks from one match
            continue
        final.append(b)
        cat_count[c] = cat_count.get(c, 0) + 1
        match_count[mid] = match_count.get(mid, 0) + 1
        if len(final) >= 40:
            break
    data = {"bets": final, "matchesConsidered": len(upcoming),
            "windowHours": _BESTBETS_WINDOW // 3600}
    _bestbets_cache.update(ts=now, data=data)
    return data


@betstats.get("/api/best-bets")
async def api_best_bets():
    try:
        return await run_in_threadpool(_best_bets_sync)
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"Data source unavailable: {exc}")


_GEMINI_LIMIT = 3
_gemini_store: dict[int, list] = {}
_gemini_lock = threading.Lock()


def _gemini_payload(event_id: int, limit_reached: bool = False) -> dict:
    with _gemini_lock:
        items = list(_gemini_store.get(event_id, []))
    return {
        "enabled": get_settings().gemini_enabled,
        "limit": _GEMINI_LIMIT,
        "used": len(items),
        "remaining": max(0, _GEMINI_LIMIT - len(items)),
        "limitReached": limit_reached or len(items) >= _GEMINI_LIMIT,
        "analyses": items,
    }


@betstats.get("/api/gemini/{event_id}")
async def api_gemini_list(event_id: int):
    """Return the stored AI analyses for a match (without consuming one)."""
    return _gemini_payload(event_id)


@betstats.post("/api/gemini/{event_id}")
async def api_gemini_run(event_id: int):
    """Run one new AI analysis (max 3 per match) and return all stored ones."""
    with _gemini_lock:
        if len(_gemini_store.get(event_id, [])) >= _GEMINI_LIMIT:
            return _gemini_payload(event_id, limit_reached=True)
    try:
        dataset = await run_in_threadpool(build_dataset, event_id)
        engine_output = await run_in_threadpool(engine.analyze, dataset)
        reliability = await run_in_threadpool(_market_reliability)
        analysis = await run_in_threadpool(analyze_with_gemini, dataset, engine_output, reliability)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except GeminiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"SofaScore unavailable: {exc}")
    with _gemini_lock:
        items = _gemini_store.setdefault(event_id, [])
        if len(items) < _GEMINI_LIMIT:
            items.append(analysis)
    return _gemini_payload(event_id)


# App served at the domain root (compubot.online is dedicated to it).
app = betstats
