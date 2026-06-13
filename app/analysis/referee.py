"""Referee card tendencies.

FotMob exposes the referee name/country per match but no referee id or stats,
so we build the data ourselves: scan the finished World Cup matches, attribute
each match's total yellow cards to its referee, and average per referee. Small
samples are shrunk toward the tournament mean (Bayesian shrinkage) so a ref with
one match doesn't swing wildly.

``factor_for(name)`` returns a multiplier (clamped) applied to card markets:
factor > 1 = stricter-than-average referee.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from app.config import get_settings
from app.scraper import endpoints, fixtures
from app.scraper.aggregator import _num, _parse_team_stats

_SHRINK_K = 3.0       # pseudo-matches pulling a ref toward the league mean
_CLAMP = (0.6, 1.6)
_TTL = 6 * 3600
_DEFAULT_AVG = 4.0

_lock = threading.Lock()
_table: dict | None = None
_table_ts = 0.0


def _cache_file() -> Path:
    return Path(get_settings().cache_dir) / "referee_table.json"


def _scan() -> dict:
    days = fixtures.list_world_cup_matches()
    finished_ids = [m["id"] for d in days for m in d["matches"] if m["statusType"] == "finished"]

    refs: dict[str, dict] = {}
    totals: list[float] = []
    for mid in finished_ids:
        md = endpoints.match_details(mid)
        if not isinstance(md, dict):
            continue
        stats = _parse_team_stats(md)
        yc = stats.get("yellowCards")
        if not yc:
            continue
        total = (_num(yc[0]) or 0) + (_num(yc[1]) or 0)
        totals.append(total)
        ref = (((md.get("content") or {}).get("matchFacts") or {}).get("infoBox") or {}).get("Referee") or {}
        name = ref.get("text")
        if not name:
            continue
        r = refs.setdefault(name, {"name": name, "country": ref.get("country"),
                                   "matches": 0, "totalYellow": 0.0})
        r["matches"] += 1
        r["totalYellow"] += total

    league_avg = (sum(totals) / len(totals)) if totals else _DEFAULT_AVG
    for r in refs.values():
        r["avgYellow"] = round(r["totalYellow"] / r["matches"], 2)
    return {"referees": refs, "leagueAvg": round(league_avg, 2), "matchesScanned": len(totals)}


def get_table(force: bool = False) -> dict:
    global _table, _table_ts
    with _lock:
        if not force and _table is not None and (time.time() - _table_ts) < _TTL:
            return _table
        # try disk cache
        fp = _cache_file()
        if not force and fp.exists() and (time.time() - fp.stat().st_mtime) < _TTL:
            try:
                _table = json.loads(fp.read_text(encoding="utf-8"))
                _table_ts = time.time()
                return _table
            except (json.JSONDecodeError, OSError):
                pass
        _table = _scan()
        _table_ts = time.time()
        try:
            fp.write_text(json.dumps(_table), encoding="utf-8")
        except OSError:
            pass
        return _table


def factor_for(name: str | None) -> tuple[float, dict]:
    """Return (multiplier, info) for a referee. Unknown ref -> neutral 1.0."""
    table = get_table()
    league_avg = table.get("leagueAvg") or _DEFAULT_AVG
    info = {"name": name, "leagueAvg": league_avg, "known": False, "factor": 1.0}
    if not name or league_avg <= 0:
        return 1.0, info
    r = table["referees"].get(name)
    if not r:
        return 1.0, info
    shrunk = (r["totalYellow"] + _SHRINK_K * league_avg) / (r["matches"] + _SHRINK_K)
    factor = min(max(shrunk / league_avg, _CLAMP[0]), _CLAMP[1])
    info.update({
        "country": r.get("country"),
        "matches": r["matches"],
        "avgYellow": r["avgYellow"],
        "shrunkAvg": round(shrunk, 2),
        "factor": round(factor, 3),
        "known": True,
    })
    return factor, info
