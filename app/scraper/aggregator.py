"""Build a normalized MatchDataset for one match from FotMob data.

For the two teams in a match we sample each team's recent finished matches and
extract, per match:
  * goals for / against
  * team stats for / against: corners, shots, shots on target, yellow cards, fouls
  * per-player samples: shots (from shotmap), fouls (from playerStats) and yellow
    cards (from match events)

The result feeds the source-agnostic statistical engine, which blends a team's
"for" rate with the opponent's "against" rate.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.scraper import endpoints


def _epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None

# FotMob stat title (lowercased) -> our key.
_STAT_MAP = {
    "corners": "corners",
    "total shots": "shots",
    "shots on target": "shotsOnTarget",
    "yellow cards": "yellowCards",
    "fouls committed": "fouls",
}
_STAT_KEYS = list(dict.fromkeys(_STAT_MAP.values()))


def _empty_stat_block() -> dict[str, list]:
    return {k: [] for k in _STAT_KEYS}


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        # strings like "313 (81%)" -> leading number
        try:
            return float(str(value).split()[0])
        except (ValueError, IndexError):
            return None


def _iid(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _team_index(md: dict, team_id: int) -> int | None:
    """0 if team is home, 1 if away (FotMob header.teams order)."""
    teams = (md.get("header") or {}).get("teams") or []
    for i, t in enumerate(teams[:2]):
        if _iid(t.get("id")) == team_id:
            return i
    return None


def _parse_team_stats(md: dict) -> dict[str, list[float]]:
    """Return {key: [home, away]} for the ALL period; None -> 0.0."""
    out: dict[str, list[float]] = {}
    periods = ((md.get("content") or {}).get("stats") or {}).get("Periods") or {}
    all_block = periods.get("All") or {}
    for group in all_block.get("stats", []) or []:
        for item in group.get("stats", []) or []:
            key = _STAT_MAP.get((item.get("title") or "").strip().lower())
            if not key:
                continue
            pair = item.get("stats")
            if not isinstance(pair, list) or len(pair) < 2:
                continue
            h, a = _num(pair[0]), _num(pair[1])
            out[key] = [h if h is not None else 0.0, a if a is not None else 0.0]
    return out


def _parse_shotmap(md: dict) -> dict[int, int]:
    """playerId -> total shot attempts (excluding own goals)."""
    out: dict[int, int] = {}
    shots = ((md.get("content") or {}).get("shotmap") or {}).get("shots") or []
    for sh in shots:
        if sh.get("isOwnGoal"):
            continue
        pid = _iid(sh.get("playerId"))
        if pid is not None:
            out[pid] = out.get(pid, 0) + 1
    return out


def _player_stat_value(stats_groups: list, wanted_key: str) -> float | None:
    for group in stats_groups or []:
        items = group.get("stats")
        if not isinstance(items, dict):
            continue
        for _title, val in items.items():
            if val.get("key") == wanted_key:
                stat = val.get("stat") or {}
                return _num(stat.get("value"))
    return None


def _parse_player_stats(md: dict) -> dict[int, dict]:
    """playerId -> {name, teamId, minutes, fouls}."""
    out: dict[int, dict] = {}
    ps = (md.get("content") or {}).get("playerStats") or {}
    for entry in ps.values():
        pid = _iid(entry.get("id"))
        if pid is None:
            continue
        groups = entry.get("stats") or []
        minutes = _player_stat_value(groups, "minutes_played") or 0
        out[pid] = {
            "name": entry.get("name"),
            "teamId": _iid(entry.get("teamId")),
            "minutes": minutes,
            "fouls": _player_stat_value(groups, "fouls") or 0,
        }
    return out


def _parse_cards(md: dict) -> dict[int, int]:
    """playerId -> yellow card count."""
    out: dict[int, int] = {}
    events = (((md.get("content") or {}).get("matchFacts") or {}).get("events") or {}).get("events") or []
    for e in events:
        if e.get("type") != "Card":
            continue
        if (e.get("card") or "").lower() != "yellow":
            continue
        pid = _iid(e.get("playerId"))
        if pid is not None:
            out[pid] = out.get(pid, 0) + 1
    return out


def _team_form(team_id: int, team_name: str, recent: int, before_ts: int | None = None) -> dict:
    data = endpoints.team(team_id)
    fixtures = (((data or {}).get("fixtures") or {}).get("allFixtures") or {}).get("fixtures", [])
    finished = [f for f in fixtures if (f.get("status") or {}).get("finished")]
    if before_ts is not None:
        # Only matches kicked off strictly before the cutoff -> genuine pre-match form.
        finished = [f for f in finished
                    if (_epoch((f.get("status") or {}).get("utcTime")) or 0) < before_ts]
    # newest first by utcTime
    finished.sort(key=lambda f: (f.get("status") or {}).get("utcTime") or "", reverse=True)
    finished = finished[:recent]

    form = {
        "teamId": team_id, "name": team_name, "matchesSampled": 0,
        "goalsFor": [], "goalsAgainst": [],
        "statFor": _empty_stat_block(), "statAgainst": _empty_stat_block(),
        "players": {}, "sampledMatches": [], "shotmapMatches": 0,
    }

    settings = get_settings()
    with ThreadPoolExecutor(max_workers=settings.max_concurrency) as pool:
        details = list(pool.map(lambda f: endpoints.match_details(f.get("id")), finished))

    for md in details:
        if not isinstance(md, dict):
            continue
        idx = _team_index(md, team_id)
        if idx is None:
            continue
        opp = 1 - idx
        teams = (md.get("header") or {}).get("teams") or []
        gf = _num(teams[idx].get("score"))
        ga = _num(teams[opp].get("score"))
        if gf is None or ga is None:
            continue

        form["matchesSampled"] += 1
        form["sampledMatches"].append(_iid(md.get("general", {}).get("matchId")))
        form["goalsFor"].append(gf)
        form["goalsAgainst"].append(ga)

        stats = _parse_team_stats(md)
        for key in _STAT_KEYS:
            if key in stats:
                form["statFor"][key].append(stats[key][idx])
                form["statAgainst"][key].append(stats[key][opp])

        shotmap = _parse_shotmap(md)
        has_shotmap = bool(shotmap)  # "ratings"-coverage matches lack shot data
        if has_shotmap:
            form["shotmapMatches"] += 1
        pstats = _parse_player_stats(md)
        cards = _parse_cards(md)
        for pid, p in pstats.items():
            if p["teamId"] != team_id or p["minutes"] <= 0:
                continue
            slot = form["players"].setdefault(
                pid, {"name": p["name"], "appearances": 0, "shots": [], "fouls": [], "yellow": 0})
            slot["appearances"] += 1
            # Only record shots when the match actually had shotmap coverage,
            # so a no-data match isn't counted as "0 shots".
            if has_shotmap:
                slot["shots"].append(float(shotmap.get(pid, 0)))
            slot["fouls"].append(float(p["fouls"]))
            slot["yellow"] += cards.get(pid, 0)

    return form


def _current_lineup(md: dict) -> dict:
    lu = (md.get("content") or {}).get("lineup") or {}
    lineup_type = (lu.get("lineupType") or "").lower()
    result = {"confirmed": False, "homeXI": None, "awayXI": None, "players": {}, "lineupType": lu.get("lineupType")}
    has_any = False
    for side, key in (("homeTeam", "homeXI"), ("awayTeam", "awayXI")):
        block = lu.get(side) or {}
        starters = block.get("starters") or []
        xi: list[int] = []
        for p in starters + (block.get("subs") or []):
            pid = _iid(p.get("id"))
            if pid is None:
                continue
            result["players"][pid] = {"name": p.get("name"), "position": p.get("positionId"),
                                      "side": "home" if side == "homeTeam" else "away"}
        ids = [_iid(p.get("id")) for p in starters if _iid(p.get("id")) is not None]
        if ids:
            result[key] = ids
            has_any = True
    # FotMob marks official lineups as "confirmed"/"lineup"; "predicted"/"probable" are provisional.
    result["confirmed"] = has_any and lineup_type in ("confirmed", "lineup", "standard")
    return result


def build_dataset(event_id: int, *, force_lineups: bool = False, before_ts: int | None = None) -> dict:
    settings = get_settings()
    md = endpoints.match_details(event_id, force=force_lineups)
    if not isinstance(md, dict) or not md.get("header"):
        raise ValueError(f"Match {event_id} not found")

    teams = (md.get("header") or {}).get("teams") or []
    if len(teams) < 2:
        raise ValueError(f"Match {event_id} has no team data")
    home, away = teams[0], teams[1]
    home_id, away_id = _iid(home.get("id")), _iid(away.get("id"))

    status = (md.get("header") or {}).get("status") or {}
    general = md.get("general") or {}
    lineup = _current_lineup(md)

    # Referee strictness -> card-market multiplier (lazy import avoids cycle).
    ref_box = (((md.get("content") or {}).get("matchFacts") or {}).get("infoBox") or {}).get("Referee") or {}
    ref_name = ref_box.get("text")
    try:
        from app.analysis import referee as _referee
        ref_factor, ref_info = _referee.factor_for(ref_name)
    except Exception:
        ref_factor, ref_info = 1.0, {"name": ref_name, "known": False, "factor": 1.0}

    home_form = _team_form(home_id, home.get("name"), settings.recent_matches, before_ts)
    away_form = _team_form(away_id, away.get("name"), settings.recent_matches, before_ts)

    status_type = "finished" if status.get("finished") else ("inprogress" if status.get("started") else "notstarted")

    return {
        "event": {
            "id": event_id,
            "home": {"id": home_id, "name": home.get("name")},
            "away": {"id": away_id, "name": away.get("name")},
            "startTimestamp": status.get("utcTime"),
            "status": status_type,
            "tournament": general.get("leagueName"),
            "venue": ((general.get("stadium") or {}) if isinstance(general.get("stadium"), dict) else {}).get("name"),
        },
        "lineupConfirmed": lineup["confirmed"],
        "lineupType": lineup["lineupType"],
        "homeXI": lineup["homeXI"],
        "awayXI": lineup["awayXI"],
        "lineupPlayers": lineup["players"],
        "referee": ref_info,
        "refereeFactor": ref_factor,
        "home": home_form,
        "away": away_form,
    }
