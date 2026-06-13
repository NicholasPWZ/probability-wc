"""Thin wrappers over the FotMob /api/data endpoints we use."""
from __future__ import annotations

from typing import Any

from app.scraper.client import get_client


def league(league_id: int, season: str | None = None) -> Any:
    path = f"leagues?id={league_id}"
    if season:
        path += f"&season={season}"
    return get_client().get(path, ttl=900)  # fixtures change rarely


def team(team_id: int) -> Any:
    return get_client().get(f"teams?id={team_id}", ttl=1800)


def match_details(match_id: int | str, *, force: bool = False) -> Any:
    # Force-refresh (ttl=0) is used when pulling updated lineups near kickoff.
    return get_client().get(
        f"matchDetails?matchId={match_id}",
        force=force,
        ttl=0 if force else None,
    )
