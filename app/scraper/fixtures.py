"""List all World Cup matches, grouped by day in the display timezone (FotMob)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.scraper import endpoints

_PT_WEEKDAYS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
_PT_MONTHS = [
    "", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
    "Jul", "Ago", "Set", "Out", "Nov", "Dez",
]


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _status_type(status: dict) -> str:
    if status.get("cancelled"):
        return "cancelled"
    if status.get("finished"):
        return "finished"
    if status.get("started"):
        return "inprogress"
    return "notstarted"


def _summarize(match: dict, tz: ZoneInfo) -> dict | None:
    status = match.get("status", {}) or {}
    dt = _parse_utc(status.get("utcTime"))
    if dt is None:
        return None
    local = dt.astimezone(tz)
    home = match.get("home") or {}
    away = match.get("away") or {}
    st = _status_type(status)
    return {
        "id": match.get("id"),
        "home": home.get("name"),
        "away": away.get("name"),
        "homeId": home.get("id"),
        "awayId": away.get("id"),
        "startTimestamp": int(dt.timestamp()),
        "date": local.strftime("%Y-%m-%d"),
        "time": local.strftime("%H:%M"),
        "statusType": st,
        "statusText": status.get("scoreStr") if st in ("finished", "inprogress") else None,
        "round": match.get("roundName") or match.get("round"),
        "group": match.get("group"),
    }


def _day_label(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{_PT_WEEKDAYS[d.weekday()]}, {d.day:02d} {_PT_MONTHS[d.month]} {d.year}"


def list_world_cup_matches() -> list[dict]:
    """Return WC matches grouped by local day.

    Shape: ``[{"date","label","matches":[...]}, ...]`` sorted chronologically.
    """
    settings = get_settings()
    tz = ZoneInfo(settings.display_tz)

    data = endpoints.league(settings.league_id, settings.season)
    fixtures = ((data or {}).get("fixtures") or {}).get("allMatches", []) if isinstance(data, dict) else []

    summaries = [s for s in (_summarize(m, tz) for m in fixtures) if s]
    summaries.sort(key=lambda s: s["startTimestamp"])

    groups: dict[str, dict] = {}
    for s in summaries:
        g = groups.setdefault(s["date"], {"date": s["date"], "label": _day_label(s["date"]), "matches": []})
        g["matches"].append(s)

    return [groups[d] for d in sorted(groups)]
