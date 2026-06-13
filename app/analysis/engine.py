"""Statistical probability engine.

This is the primary analyzer (Gemini is optional and layered on top). Every
market blends a team's own "for" rate with the opponent's "against" rate.

Models:
  * Goals -> independent-Poisson score matrix with a Dixon-Coles low-score
    correction -> 1X2, double chance, over/under, BTTS, correct score, Asian
    handicap (half lines).
  * Team count props -> blended mean; Poisson, or Negative Binomial when the
    sample is overdispersed -> over/under per line.
  * Player props -> per-player blended mean (with a mild opponent adjustment)
    -> over/under for shots & fouls, and P(>=1 card).
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

MAX_GOALS = 10
DC_RHO = -0.05  # Dixon-Coles correction strength.

# Default over/under lines per market.
GOAL_LINES = [0.5, 1.5, 2.5, 3.5, 4.5]
HANDICAP_LINES = [-2.5, -1.5, -0.5, 0.5, 1.5, 2.5]
TEAM_PROP_LINES = {
    "corners": {"team": [3.5, 4.5, 5.5], "total": [8.5, 9.5, 10.5]},
    "shots": {"team": [10.5, 12.5, 14.5], "total": [21.5, 24.5, 27.5]},
    "shotsOnTarget": {"team": [3.5, 4.5, 5.5], "total": [6.5, 7.5, 8.5]},
    "yellowCards": {"team": [1.5, 2.5], "total": [3.5, 4.5, 5.5]},
    "fouls": {"team": [10.5, 12.5, 14.5], "total": [20.5, 23.5, 26.5]},
}
PLAYER_SHOT_LINES = [0.5, 1.5, 2.5]
PLAYER_FOUL_LINES = [0.5, 1.5, 2.5]

STAT_LABELS = {
    "corners": "Corners",
    "shots": "Total shots",
    "shotsOnTarget": "Shots on target",
    "yellowCards": "Yellow cards",
    "fouls": "Fouls",
}


# --------------------------------------------------------------------------
# small stats helpers
# --------------------------------------------------------------------------
def _mean(xs: Sequence[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _variance(xs: Sequence[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def _blend(*values: float | None) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _round(p: float) -> float:
    return round(max(0.0, min(1.0, p)), 4)


# --------------------------------------------------------------------------
# count distributions
# --------------------------------------------------------------------------
def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def _negbin_pmf(k: int, mean: float, var: float) -> float:
    # mean < var. p = mean/var, r = mean^2/(var-mean).
    p = mean / var
    r = mean * mean / (var - mean)
    return math.exp(
        math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
        + r * math.log(p) + k * math.log(1 - p)
    )


class CountDist:
    """A discrete count distribution with a target mean and optional variance.

    Uses Poisson unless ``var > mean`` (overdispersion), in which case a
    Negative Binomial with the same mean & variance is used.
    """

    def __init__(self, mean: float, var: float | None = None) -> None:
        self.mean = max(mean, 1e-6)
        self.var = var
        self._overdispersed = var is not None and var > self.mean * 1.05

    def pmf(self, k: int) -> float:
        if self._overdispersed:
            return _negbin_pmf(k, self.mean, self.var)
        return _poisson_pmf(k, self.mean)

    def over(self, line: float, cap: int = 60) -> float:
        """P(X > line) for a .5 line."""
        k0 = math.floor(line)
        cdf = sum(self.pmf(k) for k in range(0, k0 + 1))
        return _round(1.0 - cdf)

    def top(self, n: int = 3, cap: int = 40) -> list[dict]:
        """The n most probable exact counts: [{value, prob}, ...]."""
        probs = [(k, self.pmf(k)) for k in range(cap + 1)]
        probs.sort(key=lambda kv: kv[1], reverse=True)
        return [{"value": k, "prob": _round(p)} for k, p in probs[:n]]


def _ou_block(dist: CountDist, lines: Iterable[float]) -> dict:
    out = {}
    for line in lines:
        over = dist.over(line)
        out[str(line)] = {"over": over, "under": _round(1.0 - over)}
    return out


# --------------------------------------------------------------------------
# goals / result
# --------------------------------------------------------------------------
def _dc_tau(i: int, j: int, lh: float, la: float, rho: float) -> float:
    if i == 0 and j == 0:
        return 1 - lh * la * rho
    if i == 0 and j == 1:
        return 1 + lh * rho
    if i == 1 and j == 0:
        return 1 + la * rho
    if i == 1 and j == 1:
        return 1 - rho
    return 1.0


def _score_matrix(lh: float, la: float) -> list[list[float]]:
    hp = [_poisson_pmf(i, lh) for i in range(MAX_GOALS + 1)]
    ap = [_poisson_pmf(j, la) for j in range(MAX_GOALS + 1)]
    matrix = [[hp[i] * ap[j] * _dc_tau(i, j, lh, la, DC_RHO) for j in range(MAX_GOALS + 1)]
              for i in range(MAX_GOALS + 1)]
    total = sum(sum(row) for row in matrix)
    if total > 0:
        matrix = [[c / total for c in row] for row in matrix]
    return matrix


def _goals_market(home: dict, away: dict, hfa: float = 1.0) -> dict:
    h_gf, h_ga = _mean(home["goalsFor"]), _mean(home["goalsAgainst"])
    a_gf, a_ga = _mean(away["goalsFor"]), _mean(away["goalsAgainst"])

    baseline = _mean(list(home["goalsFor"]) + list(away["goalsFor"])) or 1.35
    baseline = max(baseline, 0.3)

    def strength(v):
        return (v / baseline) if v is not None else 1.0

    lh = strength(h_gf) * strength(a_ga) * baseline * hfa
    la = strength(a_gf) * strength(h_ga) * baseline / hfa
    lh = min(max(lh, 0.1), 6.0)
    la = min(max(la, 0.1), 6.0)

    m = _score_matrix(lh, la)

    home_win = sum(m[i][j] for i in range(MAX_GOALS + 1) for j in range(i))
    draw = sum(m[i][i] for i in range(MAX_GOALS + 1))
    away_win = sum(m[i][j] for j in range(MAX_GOALS + 1) for i in range(j))

    # over/under & btts
    ou = {}
    for line in GOAL_LINES:
        over = sum(m[i][j] for i in range(MAX_GOALS + 1) for j in range(MAX_GOALS + 1) if i + j > line)
        ou[str(line)] = {"over": _round(over), "under": _round(1 - over)}
    p_h0 = sum(m[0][j] for j in range(MAX_GOALS + 1))
    p_a0 = sum(m[i][0] for i in range(MAX_GOALS + 1))
    btts_yes = _round(1 - p_h0 - p_a0 + m[0][0])

    # correct score top 7
    cells = [((i, j), m[i][j]) for i in range(7) for j in range(7)]
    cells.sort(key=lambda c: c[1], reverse=True)
    correct_score = [{"score": f"{i}-{j}", "prob": _round(p)} for (i, j), p in cells[:7]]

    # most-likely exact outcomes (top 3)
    def _top_from(weights: list[float]) -> list[dict]:
        idx = sorted(range(len(weights)), key=lambda k: weights[k], reverse=True)
        return [{"value": k, "prob": _round(weights[k])} for k in idx[:3]]

    total_dist = [0.0] * (2 * MAX_GOALS + 1)
    home_marg = [0.0] * (MAX_GOALS + 1)
    away_marg = [0.0] * (MAX_GOALS + 1)
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            total_dist[i + j] += m[i][j]
            home_marg[i] += m[i][j]
            away_marg[j] += m[i][j]
    most_likely = {
        "totalGoals": _top_from(total_dist),
        "homeGoals": _top_from(home_marg),
        "awayGoals": _top_from(away_marg),
        "score": [{"label": c["score"], "prob": c["prob"]} for c in correct_score[:3]],
    }
    # over/under: best side per line, top 3 by confidence
    ou_calls = []
    for line, v in ou.items():
        if v["over"] >= v["under"]:
            ou_calls.append({"label": f"Over {line}", "prob": v["over"]})
        else:
            ou_calls.append({"label": f"Under {line}", "prob": v["under"]})
    ou_calls.sort(key=lambda x: x["prob"], reverse=True)
    most_likely["overUnder"] = ou_calls[:3]

    # asian handicap (half lines, no push) — home perspective
    handicap = {}
    for line in HANDICAP_LINES:
        home_cov = sum(m[i][j] for i in range(MAX_GOALS + 1) for j in range(MAX_GOALS + 1) if (i - j) + line > 0)
        handicap[f"{line:+g}"] = {"home": _round(home_cov), "away": _round(1 - home_cov)}

    return {
        "expectedGoals": {"home": round(lh, 2), "away": round(la, 2), "total": round(lh + la, 2)},
        "result": {"home": _round(home_win), "draw": _round(draw), "away": _round(away_win)},
        "doubleChance": {
            "1X": _round(home_win + draw),
            "12": _round(home_win + away_win),
            "X2": _round(draw + away_win),
        },
        "overUnder": ou,
        "btts": {"yes": btts_yes, "no": _round(1 - btts_yes)},
        "correctScore": correct_score,
        "asianHandicap": handicap,
        "mostLikely": most_likely,
    }


# --------------------------------------------------------------------------
# team count props
# --------------------------------------------------------------------------
def _prop_dist(for_samples: Sequence[float], against_samples: Sequence[float]) -> CountDist | None:
    f, a = _mean(for_samples), _mean(against_samples)
    mean = _blend(f, a)
    if mean is None:
        return None
    # dispersion taken from the "for" samples, scaled to the predicted mean.
    var = _variance(for_samples)
    pred_var = None
    if var is not None and f and f > 0:
        pred_var = max(var * (mean / f), mean)
    return CountDist(mean, pred_var)


def _scaled(dist: CountDist, factor: float) -> CountDist:
    if factor == 1.0:
        return dist
    return CountDist(dist.mean * factor, dist.var * factor if dist.var is not None else None)


def _team_props(home: dict, away: dict, ref_factor: float = 1.0) -> dict:
    out = {}
    for key in STAT_LABELS:
        lines = TEAM_PROP_LINES[key]
        home_dist = _prop_dist(home["statFor"][key], away["statAgainst"][key])
        away_dist = _prop_dist(away["statFor"][key], home["statAgainst"][key])
        if home_dist is None and away_dist is None:
            continue
        # Referee strictness scales card markets.
        if key == "yellowCards" and ref_factor != 1.0:
            home_dist = _scaled(home_dist, ref_factor) if home_dist else None
            away_dist = _scaled(away_dist, ref_factor) if away_dist else None
        entry = {"label": STAT_LABELS[key]}
        if home_dist:
            entry["home"] = {"expected": round(home_dist.mean, 2),
                             "lines": _ou_block(home_dist, lines["team"]), "mostLikely": home_dist.top(3)}
        if away_dist:
            entry["away"] = {"expected": round(away_dist.mean, 2),
                             "lines": _ou_block(away_dist, lines["team"]), "mostLikely": away_dist.top(3)}
        if home_dist and away_dist:
            total_mean = home_dist.mean + away_dist.mean
            total_var = None
            if home_dist.var is not None and away_dist.var is not None:
                total_var = home_dist.var + away_dist.var
            total_dist = CountDist(total_mean, total_var)
            entry["total"] = {"expected": round(total_mean, 2),
                              "lines": _ou_block(total_dist, lines["total"]), "mostLikely": total_dist.top(3)}
        out[key] = entry
    return out


# --------------------------------------------------------------------------
# player props
# --------------------------------------------------------------------------
def _opp_factor(opp_against: Sequence[float], league_avg: float | None) -> float:
    a = _mean(opp_against)
    if a is None or not league_avg:
        return 1.0
    return min(max(a / league_avg, 0.7), 1.4)


def _select_players(team: dict, xi: list[int] | None, limit: int = 11) -> list[int]:
    players = team["players"]
    if xi:
        return [pid for pid in xi if pid in players] or _select_players(team, None, limit)
    ranked = sorted(players.items(), key=lambda kv: kv[1]["appearances"], reverse=True)
    return [pid for pid, _ in ranked[:limit]]


def _player_props_for_team(team: dict, opp: dict, xi: list[int] | None,
                           lineup_players: dict, all_shot_avg: float | None,
                           all_foul_avg: float | None, ref_factor: float = 1.0) -> list[dict]:
    shot_factor = _opp_factor(opp["statAgainst"]["shots"], all_shot_avg)
    foul_factor = _opp_factor(team["statFor"]["fouls"], all_foul_avg)  # team aggression
    out = []
    for pid in _select_players(team, xi):
        p = team["players"].get(pid)
        name = (p or {}).get("name") or (lineup_players.get(pid) or {}).get("name") or f"#{pid}"
        if not p or p["appearances"] == 0:
            out.append({"playerId": pid, "name": name, "appearances": 0,
                        "confidence": "none", "note": "no sampled appearances"})
            continue
        apps = p["appearances"]
        foul_mean = (_mean(p["fouls"]) or 0) * foul_factor
        card_rate = (p["yellow"] / apps) * ref_factor
        entry = {
            "playerId": pid,
            "name": name,
            "position": p.get("position"),
            "appearances": apps,
            "confidence": "low" if apps < 3 else ("medium" if apps < 6 else "high"),
            "fouls": {"expected": round(foul_mean, 2),
                      "lines": _ou_block(CountDist(foul_mean, _variance(p["fouls"])), PLAYER_FOUL_LINES)},
            "card": {"yellowRatePerGame": round(card_rate, 3),
                     "probAtLeastOne": _round(1 - math.exp(-card_rate)) if card_rate > 0 else 0.0},
        }
        # Shots only when the team's samples had shotmap coverage.
        if p["shots"]:
            shot_mean = (_mean(p["shots"]) or 0) * shot_factor
            entry["shots"] = {"expected": round(shot_mean, 2),
                              "lines": _ou_block(CountDist(shot_mean, _variance(p["shots"])), PLAYER_SHOT_LINES)}
        else:
            entry["shots"] = {"available": False}
        out.append(entry)
    # most relevant first
    out.sort(key=lambda x: (x.get("shots", {}).get("expected", 0) or 0), reverse=True)
    return out


def _player_props(dataset: dict, ref_factor: float = 1.0) -> dict:
    home, away = dataset["home"], dataset["away"]
    lineup_players = dataset.get("lineupPlayers", {})
    all_shots = list(home["statFor"]["shots"]) + list(away["statFor"]["shots"])
    all_fouls = list(home["statFor"]["fouls"]) + list(away["statFor"]["fouls"])
    league_shot_avg = _mean(all_shots)
    league_foul_avg = _mean(all_fouls)
    return {
        "home": _player_props_for_team(home, away, dataset.get("homeXI"), lineup_players,
                                       league_shot_avg, league_foul_avg, ref_factor),
        "away": _player_props_for_team(away, home, dataset.get("awayXI"), lineup_players,
                                       league_shot_avg, league_foul_avg, ref_factor),
    }


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------
def analyze(dataset: dict) -> dict:
    home, away = dataset["home"], dataset["away"]
    sample_n = min(home["matchesSampled"], away["matchesSampled"])
    confidence = "low" if sample_n < 3 else ("medium" if sample_n < 6 else "high")
    ref_factor = dataset.get("refereeFactor", 1.0)
    return {
        "event": dataset["event"],
        "lineupConfirmed": dataset.get("lineupConfirmed", False),
        "referee": dataset.get("referee"),
        "meta": {
            "homeMatchesSampled": home["matchesSampled"],
            "awayMatchesSampled": away["matchesSampled"],
            "confidence": confidence,
            "playerPropsProvisional": not dataset.get("lineupConfirmed", False),
            "refereeFactor": ref_factor,
        },
        "goals": _goals_market(home, away),
        "teamProps": _team_props(home, away, ref_factor),
        "playerProps": _player_props(dataset, ref_factor),
    }
