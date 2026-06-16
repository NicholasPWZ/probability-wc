"""FastAPI application: World Cup match list + probability analysis."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.analysis import accuracy, engine
from app.analysis.gemini import GeminiUnavailable, analyze_with_gemini, synthesize_with_gemini
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
    bias_acc: dict[str, dict] = {}   # stat -> {label, predSum, actSum, n} (total scope)
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
            "resultProb": a["result"]["predictedProb"], "resultActual": a["result"]["actual"],
            "resultCorrect": s["resultCorrect"], "marketHitRate": s["marketHitRate"],
            "marketHits": s["marketLineHits"], "marketTotal": s["marketLineTotal"],
        })
        # systematic bias: predicted vs actual per stat (total scope)
        for key, tp in (a.get("teamProps") or {}).items():
            tot = (tp.get("scopes") or {}).get("total")
            if not tot:
                continue
            e = bias_acc.setdefault(key, {"label": tp.get("label", key), "predSum": 0.0, "actSum": 0.0, "n": 0})
            e["predSum"] += tot["expected"]
            e["actSum"] += tot["actual"]
            e["n"] += 1

    by_market: dict[str, dict] = {}
    for c in all_calls:
        b = by_market.setdefault(c["market"], {"hits": 0, "total": 0, "confSum": 0.0})
        b["total"] += 1
        b["hits"] += 1 if c["correct"] else 0
        b["confSum"] += c.get("conf", 0)
    for b in by_market.values():
        b["rate"] = round(b["hits"] / b["total"], 3) if b["total"] else None
        b["avgConf"] = round(b["confSum"] / b["total"], 3) if b["total"] else None
        del b["confSum"]

    # over vs under direction split (across all line markets)
    direction = {}
    for sidekey in ("over", "under"):
        sub = [c for c in all_calls if c.get("side") == sidekey]
        h = sum(1 for c in sub if c["correct"])
        direction[sidekey] = {"hits": h, "total": len(sub),
                              "rate": round(h / len(sub), 3) if sub else None}

    bias = []
    for key, e in bias_acc.items():
        if not e["n"]:
            continue
        pred, act = e["predSum"] / e["n"], e["actSum"] / e["n"]
        bias.append({"label": e["label"], "predicted": round(pred, 2), "actual": round(act, 2),
                     "error": round(pred - act, 2), "n": e["n"]})
    bias.sort(key=lambda x: abs(x["error"]), reverse=True)

    return {"per_match": per_match, "all_calls": all_calls, "by_market": by_market,
            "direction": direction, "bias": bias,
            "result_correct": result_correct, "brier_sum": brier_sum, "evaluated": evaluated}


_reliability_cache: dict = {"ts": 0.0, "data": None}
_RELIABILITY_TTL = 600


def _market_reliability() -> dict:
    now = time.time()
    if _reliability_cache["data"] and (now - _reliability_cache["ts"]) < _RELIABILITY_TTL:
        return _reliability_cache["data"]
    fa = _finished_accuracy()
    data = {"byMarket": fa["by_market"], "matchesEvaluated": fa["evaluated"]}
    _reliability_cache.update(ts=now, data=data)
    return data


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
            "direction": fa["direction"],
        },
        "bias": fa["bias"],
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

    def scored(category, prob, base):
        entry = rel.get(category)
        adj = _adjusted_reliability(entry)
        base["category"] = category
        base["prob"] = prob
        base["reliability"] = entry.get("rate") if entry else None
        base["score"] = round(prob * adj * adj, 4)  # reliability-squared weighting
        return base

    def player_pick(prop):
        # Over side only (the bettable "player to reach N+"). Pick the highest line
        # the model still favors (>=50%); else the lowest line. Ranks by that prob,
        # so low-volume players (e.g. keepers) sink instead of flooding with ~100% unders.
        if not prop or not prop.get("lines"):
            return None
        lines = prop["lines"]
        best = None
        for k in sorted(lines, key=float):
            if lines[k]["over"] >= 0.5:
                best = k
        if best is None:
            best = min(lines, key=float)
        return "+" + best, lines[best]["over"], prop.get("expected")

    bets, player_bets = [], []
    for m in upcoming:
        try:
            a = _analyze_sync(m["id"])
        except Exception:
            continue
        common = {"matchId": m["id"], "home": m["home"], "away": m["away"],
                  "time": m["time"], "date": m["date"], "startTimestamp": m.get("startTimestamp")}
        # market predictions
        for p in a.get("predictions", []):
            bets.append(scored(p.get("category"), p["prob"], {
                **common, "market": p["market"], "selection": p["selection"],
                "expected": p.get("expected")}))
        # player predictions (shots / fouls / cards)
        pp = a.get("playerProps", {})
        for side, team in (("home", m["home"]), ("away", m["away"])):
            for pl in pp.get(side, []):
                if not pl.get("appearances"):
                    continue
                base = {**common, "playerId": pl["playerId"], "player": pl["name"], "team": team}
                sp = player_pick(pl.get("shots"))
                if sp:
                    player_bets.append(scored("Chutes (jogador)", sp[1], {
                        **base, "market": "Chutes", "selection": sp[0] + " chutes", "expected": sp[2]}))
                fp = player_pick(pl.get("fouls"))
                if fp:
                    player_bets.append(scored("Faltas (jogador)", fp[1], {
                        **base, "market": "Faltas", "selection": fp[0] + " faltas", "expected": fp[2]}))
                card = pl.get("card") or {}
                pa = card.get("probAtLeastOne")
                # Only surface "recebe cartão" when there's a real card risk (>=30%),
                # consistent with how the market is scored.
                if pa is not None and pa >= 0.30:
                    player_bets.append(scored("Cartões (jogador)", pa, {
                        **base, "market": "Cartão", "selection": "Recebe cartão", "expected": None}))

    def cap(items, per_cat, per_match, per_player=None, total=40):
        items = sorted(items, key=lambda x: x["score"], reverse=True)
        out, cc, mc, pcnt = [], {}, {}, {}
        for b in items:
            c, mid, pid = b["category"], b["matchId"], b.get("playerId")
            if cc.get(c, 0) >= per_cat or mc.get(mid, 0) >= per_match:
                continue
            if per_player and pid is not None and pcnt.get(pid, 0) >= per_player:
                continue
            out.append(b)
            cc[c] = cc.get(c, 0) + 1
            mc[mid] = mc.get(mid, 0) + 1
            if pid is not None:
                pcnt[pid] = pcnt.get(pid, 0) + 1
            if len(out) >= total:
                break
        return out

    data = {
        "bets": cap(bets, _BESTBETS_MAX_PER_CATEGORY, 6),
        "playerBets": cap(player_bets, 15, 8, per_player=2),
        "matchesConsidered": len(upcoming),
        "windowHours": _BESTBETS_WINDOW // 3600,
    }
    _bestbets_cache.update(ts=now, data=data)
    return data


@betstats.get("/api/best-bets")
async def api_best_bets():
    try:
        return await run_in_threadpool(_best_bets_sync)
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"Data source unavailable: {exc}")


_GEMINI_LIMIT = 2  # two independent analyses, then a final consensus
_gemini_lock = threading.Lock()


def _gemini_file() -> Path:
    return Path(get_settings().cache_dir) / "gemini_store.json"


def _gemini_load() -> dict:
    fp = _gemini_file()
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _gemini_save(data: dict) -> None:
    fp = _gemini_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, fp)  # atomic; safe across gunicorn workers


def _gemini_entry(data: dict, event_id: int) -> dict:
    return data.get(str(event_id)) or {"analyses": [], "final": None}


def _now_str() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(get_settings().display_tz)).strftime("%d/%m %H:%M")


def _gemini_payload(event_id: int) -> dict:
    with _gemini_lock:
        entry = _gemini_entry(_gemini_load(), event_id)
    analyses = entry.get("analyses", [])
    final = entry.get("final")
    used = len(analyses)
    return {
        "enabled": get_settings().gemini_enabled,
        "limit": _GEMINI_LIMIT,
        "used": used,
        "analyses": analyses,
        "final": final,
        "canAnalyze": used < _GEMINI_LIMIT,
        "canFinal": used >= _GEMINI_LIMIT and not final,
        "done": used >= _GEMINI_LIMIT and bool(final),
    }


@betstats.get("/api/gemini/{event_id}")
async def api_gemini_list(event_id: int):
    """Stored AI analyses + final consensus (server-persisted, shared by everyone)."""
    return _gemini_payload(event_id)


@betstats.post("/api/gemini/{event_id}")
async def api_gemini_run(event_id: int):
    """Next AI step: a new analysis (up to 2), then a final consensus comparing both."""
    with _gemini_lock:
        entry = _gemini_entry(_gemini_load(), event_id)
        used = len(entry.get("analyses", []))
        final = entry.get("final")
        if used < _GEMINI_LIMIT:
            action = "analyze"
        elif not final:
            action, a1, a2 = "final", entry["analyses"][0], entry["analyses"][1]
        else:
            return _gemini_payload(event_id)  # nothing left to do

    try:
        if action == "analyze":
            dataset = await run_in_threadpool(build_dataset, event_id)
            engine_output = await run_in_threadpool(engine.analyze, dataset)
            reliability = await run_in_threadpool(_market_reliability)
            result = await run_in_threadpool(analyze_with_gemini, dataset, engine_output, reliability)
        else:
            result = await run_in_threadpool(synthesize_with_gemini, a1, a2)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except GeminiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"SofaScore unavailable: {exc}")

    if isinstance(result, dict):
        result["createdAt"] = _now_str()
    with _gemini_lock:  # re-read, mutate, persist atomically
        data = _gemini_load()
        entry = _gemini_entry(data, event_id)
        if action == "analyze":
            if len(entry["analyses"]) < _GEMINI_LIMIT:
                entry["analyses"].append(result)
        elif not entry.get("final"):
            entry["final"] = result
        data[str(event_id)] = entry
        _gemini_save(data)
    return _gemini_payload(event_id)


# App served at the domain root (compubot.online is dedicated to it).
app = betstats
