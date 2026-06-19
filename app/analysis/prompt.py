"""Build the Gemini prompt and response schema from the dataset + engine output.

The goal is a sharp, well-CALIBRATED betting analyst that captures genuine edges by
using the model's own probabilities as a prior and adjusting with the supplied
form / matchup / referee context AND the model's MEASURED systematic errors — never
inventing data. Every pick carries structured fields (marketKey/side/line/scope/
playerId) so it can be graded automatically against the finished-match actuals.
"""
from __future__ import annotations

import json

SYSTEM_INSTRUCTION = (
    "You are an elite football betting analyst. Your objective is a set of WELL-CALIBRATED "
    "selections that capture genuine edges — your stated probability must match the real "
    "frequency. Skip coin-flips AND skip trivially-likely low-value picks (a 95% Under that "
    "everyone knows is not an edge).\n\n"
    "You are given, for one upcoming match: (1) both teams' recent-form stats for AND against "
    "(goals, corners, shots, shots on target, fouls, cards), (2) the referee's card tendency, "
    "(3) probabilities and headline predictions from a calibrated statistical model, (4) the "
    "model's HISTORICAL per-market hit-rate (marketReliability), (5) the model's MEASURED "
    "systematic errors (calibrationContext) and result base rates (resultPrior), and (6) "
    "per-player expectations and the exact betting lines the model priced (playerProps, "
    "availableLines).\n\n"
    "Method:\n"
    "- Treat the model's probabilities as a strong PRIOR; deviate only with a concrete "
    "form/matchup/referee/calibration reason, and say why.\n"
    "- CALIBRATION-CORRECTION (use calibrationContext): before trusting an engine mean, shift "
    "it by the measured signed error. The engine UNDER-predicts total goals and BTTS and "
    "OVER-predicts fouls — so nudge goals/BTTS/Over-goals probabilities UP and fouls-Over DOWN. "
    "State the adjustment you made in the rationale.\n"
    "- CROSS-REFERENCE marketReliability: prefer markets where the model is historically "
    "accurate; be skeptical of low-hit-rate markets (1X2 is often unreliable). If a sample is "
    "tiny, treat its hit-rate as weak evidence.\n"
    "- 1X2 / RESULT (use resultPrior): venues are NEUTRAL and draws are ~36%, yet the engine "
    "picked draw 0/28 and over-picked the second-listed (away) side. Actively consider DRAW and "
    "DOUBLE CHANCE; demand a concrete form reason before backing the away side to win outright. "
    "Do not blindly echo the engine's 1X2 pick.\n"
    "- 'FOR meets AGAINST': every rationale must cite BOTH the team's for-rate AND the "
    "opponent's against-rate from the payload (e.g. high-corner team vs a team conceding many).\n"
    "- PLAYER PROPS: only pick a player present in playerProps, using that player's supplied "
    "line/expected/cardProbAtLeastOne — never invent a player or a line. Down-weight players "
    "with low appearances/confidence or a provisional lineup. Only back a player card "
    "(side 'yes') when cardProbAtLeastOne is clearly high (~0.45+) — the engine over-states "
    "card risk.\n"
    "- Use ONLY the supplied data. Never invent injuries, news, lineups, or stats. If the "
    "sample is small or the lineup is provisional, lower your confidence.\n\n"
    "DIVERSITY (mandatory): output your STRONGEST 5-8 selections spanning at least THREE "
    "distinct market families (result/double-chance, goals O/U, BTTS, corners, cards, shots/SoT, "
    "fouls, player props) AND both directions. At most ~half may be 'Under' — an all-Under slate "
    "is invalid; if your raw edges are all Unders, replace the weakest with the best Over / "
    "BTTS-Yes / result / player-shot-Over pick. When playerProps are present and not provisional, "
    "include at least one player-prop pick.\n\n"
    "STRUCTURED-PICK CONTRACT (every pick MUST be machine-gradeable):\n"
    "- marketKey: one of goals_ou, btts, result_1x2, double_chance, corners_ou, cards_ou, "
    "shots_ou, sot_ou, fouls_ou, player_shots_ou, player_fouls_ou, player_cards.\n"
    "- side: over/under for *_ou; yes/no for btts and player_cards; home/away/draw for "
    "result_1x2; home_or_draw/away_or_draw/home_or_away for double_chance.\n"
    "- line: numeric threshold taken ONLY from availableLines (or the player's lines); REQUIRED "
    "for every *_ou market; omit for btts/result_1x2/double_chance/player_cards.\n"
    "- scope: home/away/total — REQUIRED for team-stat *_ou (corners/cards/shots/sot/fouls).\n"
    "- playerId + playerName: copied EXACTLY from playerProps — REQUIRED for player_* markets.\n"
    "- 'market' and 'selection' are human-readable text only and are NOT used for grading.\n"
    "Treat every measured number (goals/fouls bias, card over-statement, draw rate, direction "
    "split) as a SOFT calibration prior from a small 28-match sample — never as a mandatory pick."
)


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _form_summary(team: dict) -> dict:
    sf, sa = team["statFor"], team["statAgainst"]
    return {
        "name": team["name"],
        "matchesSampled": team["matchesSampled"],
        "goalsFor": _avg(team["goalsFor"]),
        "goalsAgainst": _avg(team["goalsAgainst"]),
        "cornersFor": _avg(sf["corners"]),
        "cornersAgainst": _avg(sa["corners"]),
        "shotsFor": _avg(sf["shots"]),
        "shotsAgainst": _avg(sa["shots"]),
        "shotsOnTargetFor": _avg(sf["shotsOnTarget"]),
        "shotsOnTargetAgainst": _avg(sa["shotsOnTarget"]),
        "yellowCardsFor": _avg(sf["yellowCards"]),
        "yellowCardsAgainst": _avg(sa["yellowCards"]),
        "foulsFor": _avg(sf["fouls"]),
        "foulsAgainst": _avg(sa["fouls"]),
    }


def _reliability_summary(reliability: dict | None) -> dict | None:
    """Compact per-market hit-rate map for the prompt."""
    if not reliability:
        return None
    by = reliability.get("byMarket") or {}
    return {
        "matchesEvaluated": reliability.get("matchesEvaluated"),
        "hitRateByMarket": {k: {"hitRate": v.get("rate"), "sample": v.get("total")}
                            for k, v in by.items() if v.get("rate") is not None},
        "note": "hitRate = fraction the statistical model got right on finished matches; "
                "sample = number of graded picks. Prefer high-hitRate markets.",
    }


def _calibration_summary(reliability: dict | None) -> dict | None:
    """The model's MEASURED systematic errors, handed to the AI as soft priors."""
    if not reliability:
        return None
    bias = [{"label": b["label"], "error": b["error"], "n": b["n"]}
            for b in (reliability.get("bias") or [])]
    if not bias and not reliability.get("direction"):
        return None
    return {
        "biasPredictedMinusActual": bias,
        "directionHitRate": reliability.get("direction"),
        "goalsNote": "Engine UNDER-predicts total goals (~+0.5/game) and BTTS, and OVER-predicts "
                     "fouls. Nudge goals/BTTS/Over-goals up and fouls-Over down.",
        "playerCardNote": "Engine OVER-states player card risk (predicted ~0.37 vs actual ~0.27); "
                          "only back a card when the probability is clearly high.",
        "note": "Signed error = predicted minus actual on finished matches (n per row). "
                "Small 28-match sample — soft priors, not mandates.",
    }


# Observed 1X2 base rates on the finished sample (small-sample prior).
RESULT_PRIOR = {
    "drawBaseRate": 0.36,
    "firstListedWinRate": 0.54,
    "awayWinRate": 0.11,
    "note": "Neutral WC venue. The engine picked draw 0/28 and over-picked the away "
            "(second-listed) side. Actively weigh Draw and Double Chance. Small-sample prior.",
}


def _player_props_summary(engine_output: dict) -> dict:
    """Top players per side with the engine's expectations + the exact lines it priced.
    The AI currently gets NO player data; without this every player pick is invented."""
    pp = engine_output.get("playerProps", {})
    out = {}
    for side in ("home", "away"):
        sel = []
        for p in [p for p in pp.get(side, []) if p.get("appearances")][:6]:
            entry = {
                "playerId": p["playerId"],
                "playerName": p.get("name"),
                "side": side,
                "position": p.get("position"),
                "appearances": p["appearances"],
                "confidence": p.get("confidence"),
                "fouls": p.get("fouls"),  # {expected, lines}
                "cardProbAtLeastOne": (p.get("card") or {}).get("probAtLeastOne"),
            }
            sh = p.get("shots") or {}
            if sh.get("lines"):
                entry["shots"] = {"expected": sh.get("expected"), "lines": sh.get("lines")}
            sel.append(entry)
        out[side] = sel
    return out


def _available_lines(engine_output: dict) -> dict:
    """The exact lines the engine priced, so the model cannot invent ungradeable lines."""
    goals = engine_output["goals"]
    team_props = {}
    for k, v in engine_output["teamProps"].items():
        scopes = {s: list(v[s]["lines"].keys())
                  for s in ("home", "away", "total") if s in v and v[s].get("lines")}
        if scopes:
            team_props[k] = scopes
    return {"goals": list(goals["overUnder"].keys()), "teamProps": team_props}


def _team_props_probs(engine_output: dict) -> dict:
    """teamProps with {expected, lines} per scope (the AI must see per-line probabilities)."""
    out = {}
    for k, v in engine_output["teamProps"].items():
        scopes = {s: {"expected": v[s]["expected"], "lines": v[s]["lines"]}
                  for s in ("home", "away", "total") if s in v and "lines" in v[s]}
        if scopes:
            out[k] = scopes
    return out


def build_contents(dataset: dict, engine_output: dict, reliability: dict | None = None) -> str:
    ref = dataset.get("referee") or {}
    meta = engine_output.get("meta") or {}
    goals = engine_output["goals"]
    payload = {
        "match": engine_output["event"],
        "lineupConfirmed": dataset.get("lineupConfirmed", False),
        "dataConfidence": meta.get("confidence"),
        "meta": {
            "homeMatchesSampled": meta.get("homeMatchesSampled"),
            "awayMatchesSampled": meta.get("awayMatchesSampled"),
            "playerPropsProvisional": meta.get("playerPropsProvisional"),
        },
        "marketReliability": _reliability_summary(reliability),
        "calibrationContext": _calibration_summary(reliability),
        "resultPrior": RESULT_PRIOR,
        "referee": {
            "name": ref.get("name"),
            "avgYellowPerGame": ref.get("avgYellow"),
            "leagueAvgYellow": ref.get("leagueAvg"),
            "cardFactor": ref.get("factor"),
        },
        "homeForm": _form_summary(dataset["home"]),
        "awayForm": _form_summary(dataset["away"]),
        "modelPredictions": engine_output.get("predictions"),
        "modelProbabilities": {
            "result": goals["result"],
            "doubleChance": goals["doubleChance"],
            "expectedGoals": goals["expectedGoals"],
            "overUnderGoals": goals["overUnder"],
            "btts": goals["btts"],
            "teamProps": _team_props_probs(engine_output),
        },
        "availableLines": _available_lines(engine_output),
        "playerProps": _player_props_summary(engine_output),
    }
    return (
        "Analyze this match and return your strongest, best-calibrated betting selections. "
        "Cross-check the model's numbers against the form AND the measured calibration errors, "
        "and flag any pick you'd fade. Every pick must carry the structured fields "
        "(marketKey/side/line/scope/playerId) per the contract.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


# Structured-output schema (google-genai response_schema, OpenAPI subset).
# NOTE: JSON-schema cannot express "line required IFF marketKey ends with _ou"; those
# conditional requirements are enforced in SYSTEM_INSTRUCTION and validated in the grader.
_MARKET_KEYS = ["goals_ou", "btts", "result_1x2", "double_chance", "corners_ou", "cards_ou",
                "shots_ou", "sot_ou", "fouls_ou", "player_shots_ou", "player_fouls_ou", "player_cards"]
_SIDES = ["over", "under", "yes", "no", "home", "away", "draw",
          "home_or_draw", "away_or_draw", "home_or_away"]

_STRUCTURED_BET_PROPS = {
    "marketKey": {"type": "string", "enum": _MARKET_KEYS, "description": "Canonical machine-gradeable market key."},
    "side": {"type": "string", "enum": _SIDES, "description": "Direction/selection per the contract."},
    "line": {"type": "number", "description": "Numeric threshold from availableLines; required for *_ou markets."},
    "scope": {"type": "string", "enum": ["home", "away", "total"], "description": "Required for team-stat *_ou markets."},
    "playerId": {"type": "integer", "description": "Required for player_* markets; copied from playerProps."},
    "playerName": {"type": "string", "description": "Required for player_* markets; copied from playerProps."},
    "market": {"type": "string", "description": "Human-readable, e.g. 'Total corners', 'Player shots'."},
    "selection": {"type": "string", "description": "Human-readable, e.g. 'Under 9.5', 'Mbappé Over 1.5'."},
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentence overall read of the match."},
        "keyFactors": {
            "type": "array", "items": {"type": "string"},
            "description": "How the teams' for/against profiles (and referee) interact.",
        },
        "topBets": {
            "type": "array",
            "description": "Your 5-8 strongest selections, best first (varied per the diversity rule).",
            "items": {
                "type": "object",
                "properties": {
                    **_STRUCTURED_BET_PROPS,
                    "modelProbability": {"type": "number", "description": "Your calibrated probability 0-1."},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "rationale": {"type": "string", "description": "One line; cite for-rate + against-rate + any calibration shift."},
                },
                "required": ["marketKey", "market", "selection", "side", "modelProbability", "confidence", "rationale"],
            },
        },
        "cautions": {
            "type": "array", "items": {"type": "string"},
            "description": "Data limitations / risks (small sample, provisional lineup, etc.).",
        },
    },
    "required": ["summary", "keyFactors", "topBets"],
}


# --- final consensus synthesis (compares two prior analyses) ---
SYNTHESIS_SYSTEM = (
    "You are given TWO independent AI betting analyses of the SAME match. Produce a FINAL "
    "CONSENSUS. Keep ONLY selections that BOTH analyses agree on — matched on "
    "(marketKey + side + line + scope + playerId), not on free text. For each consensus pick, "
    "merge their reasoning into one line, give a consensus confidence (use the lower of the two "
    "if they differ), and CARRY THROUGH the structured fields (marketKey, side, line, scope, "
    "playerId, playerName) UNCHANGED so the consensus is graded on the same code path. List "
    "notable DIVERGENCES separately, briefly. Use ONLY what the two analyses contain — introduce "
    "no new picks."
)

SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentence consensus read of the match."},
        "consensusBets": {
            "type": "array",
            "description": "Only picks BOTH analyses agreed on (matched on the structured fields).",
            "items": {
                "type": "object",
                "properties": {
                    **_STRUCTURED_BET_PROPS,
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "rationale": {"type": "string"},
                },
                "required": ["marketKey", "market", "selection", "side", "confidence", "rationale"],
            },
        },
        "divergences": {
            "type": "array", "items": {"type": "string"},
            "description": "Where the two analyses disagreed.",
        },
    },
    "required": ["summary", "consensusBets"],
}


def build_synthesis_contents(a1: dict, a2: dict) -> str:
    return (
        "Two independent analyses of the same match follow. Produce the final consensus "
        "(only picks both agree on, matched on marketKey+side+line+scope+playerId; carry the "
        "structured fields through unchanged).\n\nANALYSIS 1:\n" + json.dumps(a1, ensure_ascii=False)
        + "\n\nANALYSIS 2:\n" + json.dumps(a2, ensure_ascii=False)
    )
