"""FastAPI application: World Cup match list + probability analysis."""
from __future__ import annotations

import json
import os
import re
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
from app.models import AnalyzeUrlRequest, GeminiRunRequest, GeminiSettingsRequest
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
    from app import runtime
    return {
        "geminiEnabled": runtime.gemini_enabled(),
        "geminiModel": runtime.gemini_model(),
        "adminConfigured": bool(get_settings().admin_token.strip()),
    }


@betstats.post("/api/settings/gemini")
async def api_set_gemini(req: GeminiSettingsRequest):
    """Change the Gemini key/model from the UI. Guarded by ADMIN_TOKEN."""
    from app import runtime
    admin = get_settings().admin_token.strip()
    if not admin:
        raise HTTPException(status_code=403, detail="Edição pela UI desativada: defina ADMIN_TOKEN no servidor.")
    if (req.token or "").strip() != admin:
        raise HTTPException(status_code=401, detail="Token de admin inválido.")
    if not (req.apiKey or "").strip() and not (req.model or "").strip():
        raise HTTPException(status_code=400, detail="Informe a chave e/ou o modelo.")
    runtime.set_gemini(api_key=req.apiKey, model=req.model)
    return {"ok": True, "geminiEnabled": runtime.gemini_enabled(), "geminiModel": runtime.gemini_model()}


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
    # Widened additively: bias (predicted-vs-actual per stat) and over/under direction
    # split feed the Gemini calibrationContext. /api/reliability + inline badges only
    # read byMarket, so the extra keys are harmless to them.
    data = {"byMarket": fa["by_market"], "matchesEvaluated": fa["evaluated"],
            "bias": fa["bias"], "direction": fa["direction"]}
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


def _entry_sections(raw: dict | None) -> list[dict]:
    """Normalize a stored entry into a list of sections.

    A *section* is one round of {analyses: [...up to _GEMINI_LIMIT...], final: ...}.
    Legacy entries stored a single {analyses, final} at the top level — migrate those
    into a one-section list so old data keeps working.
    """
    if not raw:
        return []
    secs = raw.get("sections")
    if secs is None:  # legacy single-round entry
        return [{"analyses": raw.get("analyses", []), "final": raw.get("final")}]
    return secs


def _gemini_entry(data: dict, event_id: int) -> dict:
    secs = _entry_sections(data.get(str(event_id)))
    if not secs:
        secs = [{"analyses": [], "final": None}]
    return {"sections": secs}


def _section_done(section: dict) -> bool:
    return len(section.get("analyses") or []) >= _GEMINI_LIMIT and bool(section.get("final"))


def _now_str() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(get_settings().display_tz)).strftime("%d/%m %H:%M")


def _section_state(section: dict) -> dict:
    analyses = section.get("analyses") or []
    final = section.get("final")
    used = len(analyses)
    return {
        "analyses": analyses,
        "final": final,
        "used": used,
        "canAnalyze": used < _GEMINI_LIMIT,
        "canFinal": used >= _GEMINI_LIMIT and not final,
        "done": used >= _GEMINI_LIMIT and bool(final),
    }


def _gemini_payload(event_id: int) -> dict:
    with _gemini_lock:
        entry = _gemini_entry(_gemini_load(), event_id)
    sections = [_section_state(s) for s in entry["sections"]]
    from app import runtime
    return {
        "enabled": runtime.gemini_enabled(),
        "limit": _GEMINI_LIMIT,
        "adminConfigured": bool(get_settings().admin_token.strip()),
        "sections": sections,
        # A new section can be added only once the current (last) one is complete.
        "canAddSection": bool(sections) and sections[-1]["done"],
    }


@betstats.get("/api/gemini/{event_id}")
async def api_gemini_list(event_id: int):
    """Stored AI analyses + final consensus (server-persisted, shared by everyone)."""
    return _gemini_payload(event_id)


@betstats.post("/api/gemini/{event_id}")
async def api_gemini_run(event_id: int, req: GeminiRunRequest | None = None):
    """Progress the active AI section: a new analysis (up to 2), then a final
    consensus. With ``action="new_section"`` (admin-gated) start a fresh round."""
    if req and req.action == "new_section":
        admin = get_settings().admin_token.strip()
        if not admin:
            raise HTTPException(status_code=403, detail="Nova seção desativada: defina ADMIN_TOKEN no servidor.")
        if (req.token or "").strip() != admin:
            raise HTTPException(status_code=401, detail="Token de admin inválido.")
        with _gemini_lock:
            data = _gemini_load()
            entry = _gemini_entry(data, event_id)
            if _section_done(entry["sections"][-1]):  # only extend a completed round
                entry["sections"].append({"analyses": [], "final": None})
                data[str(event_id)] = entry
                _gemini_save(data)
        return _gemini_payload(event_id)

    with _gemini_lock:
        entry = _gemini_entry(_gemini_load(), event_id)
        section = entry["sections"][-1]  # active round is always the last one
        analyses = section.get("analyses") or []
        used = len(analyses)
        final = section.get("final")
        if used < _GEMINI_LIMIT:
            action = "analyze"
        elif not final:
            action, a1, a2 = "final", analyses[0], analyses[1]
        else:
            return _gemini_payload(event_id)  # nothing left to do in this section

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
        section = entry["sections"][-1]
        if action == "analyze":
            section.setdefault("analyses", [])
            if len(section["analyses"]) < _GEMINI_LIMIT:
                section["analyses"].append(result)
        elif not section.get("final"):
            section["final"] = result
        data[str(event_id)] = entry
        _gemini_save(data)
    return _gemini_payload(event_id)


# ---- AI performance (grade stored AI picks — individual analyses AND consensus) ----
def _match_actual_player(text: str, actuals: dict) -> dict | None:
    """Find the finished-match player whose name best matches a free-form pick."""
    text_l = (text or "").lower()
    if not text_l:
        return None
    best, best_len = None, 0
    for p in (actuals.get("players") or {}).values():
        name = (p.get("name") or "").strip().lower()
        if not name:
            continue
        if name in text_l and len(name) > best_len:
            best, best_len = p, len(name)
            continue
        for tok in name.replace(".", " ").split():
            # surname-level match; require >=4 chars to avoid spurious hits
            if len(tok) >= 4 and tok in text_l and len(tok) > best_len:
                best, best_len = p, len(tok)
    return best


_AI_STAT_MAP = {"corners_ou": "corners", "cards_ou": "yellowCards", "shots_ou": "shots",
                "sot_ou": "shotsOnTarget", "fouls_ou": "fouls"}
_AI_DC_MAP = {"home_or_draw": ("home", "draw"), "away_or_draw": ("away", "draw"),
              "home_or_away": ("home", "away")}


def _resolve_player(bet: dict, actuals: dict) -> dict | None:
    """Join an AI player pick to the finished-match player — by playerId, else by name."""
    players = actuals.get("players") or {}
    pid = bet.get("playerId")
    if pid is not None:
        try:
            p = players.get(int(pid))
        except (TypeError, ValueError):
            p = None
        if p:
            return p
    name = bet.get("playerName") or bet.get("player") or bet.get("selection") or ""
    return _match_actual_player(f"{name} {bet.get('market', '')}", actuals)


def _grade_structured(bet: dict, actuals: dict) -> bool | None:
    """Grade a pick that carries the structured contract fields. None = ungradeable."""
    mk = (bet.get("marketKey") or "").strip().lower()
    side = (bet.get("side") or "").strip().lower()
    scope = (bet.get("scope") or "total").strip().lower()
    g, ts = actuals["goals"], actuals["teamStats"]
    try:
        line = float(bet["line"]) if bet.get("line") is not None else None
    except (TypeError, ValueError):
        line = None

    def ou(val):
        if val is None or line is None or side not in ("over", "under"):
            return None
        return (val > line) if side == "over" else (val <= line)

    if mk == "goals_ou":
        return ou(g["total"])
    if mk == "btts":
        return g["btts"] if side == "yes" else (not g["btts"] if side == "no" else None)
    if mk == "result_1x2":
        return g["result"] == side if side in ("home", "away", "draw") else None
    if mk == "double_chance":
        return g["result"] in _AI_DC_MAP[side] if side in _AI_DC_MAP else None
    if mk in _AI_STAT_MAP:
        col = ts.get(_AI_STAT_MAP[mk]) or {}
        return ou(col.get(scope if scope in ("home", "away", "total") else "total"))
    if mk in ("player_shots_ou", "player_fouls_ou", "player_cards"):
        player = _resolve_player(bet, actuals)
        if player is None:
            return None
        if mk == "player_cards":
            return (player.get("yellow") or 0) >= 1   # bettable side is "receives a card"
        return ou(player.get("fouls") if mk == "player_fouls_ou" else player.get("shots"))
    return None


def _grade_ai_bet(bet: dict, actuals: dict, home: str, away: str) -> bool | None:
    """Grade an AI pick. Prefers the structured contract fields; falls back to free-text
    parsing for legacy stored records that predate those fields. None = couldn't grade."""
    if (bet.get("marketKey") or "").strip():
        return _grade_structured(bet, actuals)
    return _grade_freetext(bet, actuals, home, away)


def _grade_freetext(bet: dict, actuals: dict, home: str, away: str) -> bool | None:
    """Best-effort grading of a free-form AI pick (legacy records with no structured fields).
    None = couldn't grade. Handles goals/result/BTTS, team stat O/U, AND player props."""
    market = bet.get("market", "") or ""
    selection = bet.get("selection", "") or ""
    subject = bet.get("player") or bet.get("subject") or ""   # dedicated player field if present
    text = f"{market} {selection} {subject}".lower()
    g = actuals["goals"]
    ts = actuals["teamStats"]
    hl, al = (home or "").lower(), (away or "").lower()
    nmatch = re.search(r"(\d+(?:\.\d+)?)", selection or "") or re.search(r"(\d+(?:\.\d+)?)", market or "")
    line = float(nmatch.group(1)) if nmatch else None
    if any(w in text for w in ("under", "menos", "abaixo", "−", "-0.5", "-1.5")):
        side = "under"
    elif any(w in text for w in ("over", "mais", "acima", "+")):
        side = "over"
    else:
        side = None

    # ---- player props (graded before team stats so "shots"/"fouls" aren't read as team) ----
    is_player = ("player" in market.lower()) or ("jogador" in text) or bool(subject)
    if is_player:
        player = _match_actual_player(f"{subject} {selection} {market}", actuals)
        if player is None:
            return None  # player didn't play / unmatched -> can't grade (don't fall through)
        if "card" in text or "cart" in text or "amarelo" in text:
            return (player.get("yellow") or 0) >= 1   # "recebe cartão"
        val = player.get("fouls") if ("foul" in text or "falta" in text) else player.get("shots")
        if val is None or line is None or side is None:
            return None
        return (val > line) if side == "over" else (val <= line)

    # both teams to score
    if any(w in text for w in ("both teams", "ambos marcam", "btts", "both score")):
        yes = "yes" in text or "sim" in text
        no = "no" in text or "não" in text or "nao" in text
        if yes and not no:
            return g["btts"]
        if no:
            return not g["btts"]
        return None
    # draw
    if "draw" in text or "empate" in text:
        return g["result"] == "draw"
    # team stat over/under (corners/cards/shots/SoT/fouls)
    stat = None
    if "corner" in text or "escanteio" in text:
        stat = "corners"
    elif "on target" in text or "no alvo" in text or "alvo" in text:
        stat = "shotsOnTarget"
    elif "shot" in text or "finaliz" in text or "chute" in text:
        stat = "shots"
    elif "card" in text or "cart" in text or "amarelo" in text:
        stat = "yellowCards"
    elif "foul" in text or "falta" in text:
        stat = "fouls"
    if stat and stat in ts and line is not None and side:
        scope = "home" if (hl and hl in text) else ("away" if (al and al in text) else "total")
        val = ts[stat].get(scope)
        if val is None:
            return None
        return (val > line) if side == "over" else (val <= line)
    # over/under goals (no stat keyword)
    if side and line is not None:
        return (g["total"] > line) if side == "over" else (g["total"] <= line)
    # team to win (name mentioned, no over/under)
    if hl and hl in text:
        return g["result"] == "home"
    if al and al in text:
        return g["result"] == "away"
    return None


def _new_acc() -> dict:
    return {"tiers": {t: {"hits": 0, "total": 0} for t in ("high", "medium", "low")},
            "byMarket": {}, "bets": [], "matches": 0, "ungraded": 0, "_calib": []}


def _grade_bet_list(bets: list, actuals: dict, home: str, away: str, acc: dict, label: str) -> bool:
    """Grade a list of AI picks into ``acc`` (tiers + byMarket + calibration + bets).
    Returns True if any pick graded."""
    graded = False
    for b in bets or []:
        conf = (b.get("confidence") or "medium").lower()
        if conf not in acc["tiers"]:
            conf = "medium"
        correct = _grade_ai_bet(b, actuals, home, away)
        if correct is not None:
            acc["tiers"][conf]["total"] += 1
            acc["tiers"][conf]["hits"] += 1 if correct else 0
            mkt = b.get("marketKey") or b.get("market") or "—"
            mm = acc["byMarket"].setdefault(mkt, {"hits": 0, "total": 0})
            mm["total"] += 1
            mm["hits"] += 1 if correct else 0
            mp = b.get("modelProbability")
            if isinstance(mp, (int, float)):
                acc["_calib"].append((float(mp), bool(correct)))
            graded = True
        else:
            acc["ungraded"] += 1
        acc["bets"].append({"match": label, "market": b.get("market"), "marketKey": b.get("marketKey"),
                            "selection": b.get("selection"), "confidence": conf, "correct": correct})
    return graded


def _finalize_acc(acc: dict) -> dict:
    for t in acc["tiers"].values():
        t["rate"] = round(t["hits"] / t["total"], 3) if t["total"] else None
    for mm in acc["byMarket"].values():
        mm["rate"] = round(mm["hits"] / mm["total"], 3) if mm["total"] else None
    oh = sum(t["hits"] for t in acc["tiers"].values())
    ot = sum(t["total"] for t in acc["tiers"].values())
    acc["overall"] = {"hits": oh, "total": ot, "rate": round(oh / ot, 3) if ot else None}
    # AI calibration + Brier over picks that carry a stated probability (individual analyses).
    calib = acc.pop("_calib", [])
    if calib:
        buckets = []
        for i in range(len(_CALIB_EDGES) - 1):
            lo, hi = _CALIB_EDGES[i], _CALIB_EDGES[i + 1]
            sub = [c for c in calib if lo <= c[0] < hi]
            if sub:
                buckets.append({"range": f"{int(lo * 100)}-{int(min(hi, 1.0) * 100)}%", "n": len(sub),
                                "predicted": round(sum(p for p, _ in sub) / len(sub), 3),
                                "actual": round(sum(1 for _, c in sub if c) / len(sub), 3)})
        acc["calibration"] = buckets
        acc["brier"] = round(sum((p - (1.0 if c else 0.0)) ** 2 for p, c in calib) / len(calib), 4)
    return acc


def _ai_performance() -> dict:
    """Grade ALL stored AI picks vs results — both the individual analyses and the
    consensus — so the user can see how each performs (and whether consensus adds value)."""
    days = fixtures.list_world_cup_matches()
    finished = [m for d in days for m in d["matches"] if m["statusType"] == "finished"]
    store = _gemini_load()
    consensus = _new_acc()
    individual = _new_acc()
    individual["analysesGraded"] = 0
    for m in finished:
        secs = _entry_sections(store.get(str(m["id"])))
        finals = [s.get("final") for s in secs if s.get("final") and s["final"].get("consensusBets")]
        analyses = [a for s in secs for a in (s.get("analyses") or []) if a.get("topBets")]
        if not finals and not analyses:
            continue
        try:
            md = endpoints.match_details(m["id"])
            actuals = accuracy.extract_actuals(md)
        except Exception:
            continue
        base = f"{m['home']} x {m['away']}"
        # individual analyses (each Gemini call's topBets)
        a_any = False
        for ai_i, a in enumerate(analyses):
            individual["analysesGraded"] += 1
            label = f"{base} — análise {ai_i + 1}"
            if _grade_bet_list(a.get("topBets"), actuals, m["home"], m["away"], individual, label):
                a_any = True
        if a_any:
            individual["matches"] += 1
        # consensus (final) picks
        c_any = False
        multi = len(finals) > 1
        for si, final in enumerate(finals):
            label = base + (f" (seção {si + 1})" if multi else "")
            if _grade_bet_list(final.get("consensusBets"), actuals, m["home"], m["away"], consensus, label):
                c_any = True
        if c_any:
            consensus["matches"] += 1
    _finalize_acc(consensus)
    _finalize_acc(individual)
    return {
        "consensus": consensus,
        "individual": individual,
        # Backward-compatible top-level keys (existing UI reads these as the consensus view).
        "tiers": consensus["tiers"], "overall": consensus["overall"],
        "bets": consensus["bets"], "matchesWithConsensus": consensus["matches"],
    }


@betstats.get("/api/ai-performance")
async def api_ai_performance():
    try:
        return await run_in_threadpool(_ai_performance)
    except SofaScoreError as exc:
        raise HTTPException(status_code=502, detail=f"Data source unavailable: {exc}")


# App served at the domain root (compubot.online is dedicated to it).
app = betstats
