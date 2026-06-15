"""Build the Gemini prompt and response schema from the dataset + engine output.

The goal is a sharp, well-calibrated betting analyst that maximizes hit-rate by
using the model's own probabilities as a prior and adjusting with the supplied
form / matchup / referee context — never inventing data.
"""
from __future__ import annotations

import json

SYSTEM_INSTRUCTION = (
    "You are an elite football betting analyst whose only objective is the highest "
    "possible hit-rate on your selections. You are given, for one upcoming match: "
    "(1) both teams' recent-form stats (goals, corners, shots, shots on target, "
    "fouls, cards) for AND against, (2) the assigned referee's card tendency, "
    "(3) probabilities already computed by a calibrated statistical model, plus its "
    "headline predictions, and (4) the model's HISTORICAL hit-rate per market "
    "(marketReliability) measured on already-finished matches.\n\n"
    "Method:\n"
    "- Treat the statistical model's probabilities as a strong PRIOR. Only deviate "
    "when the form/matchup/referee context gives a concrete reason, and say why.\n"
    "- CROSS-REFERENCE every pick with marketReliability: strongly prefer markets "
    "where the model is historically accurate (high hit-rate), and be skeptical of "
    "markets with a low hit-rate (e.g. match result / 1X2 is often unreliable) — "
    "lower your confidence there even if the probability looks high. If a market's "
    "reliability sample is tiny, treat the hit-rate as weak evidence.\n"
    "- Always reason about how one team's 'for' rate meets the opponent's 'against' "
    "rate (e.g. a high-corner team vs a team that concedes many corners).\n"
    "- Use ONLY the data provided. Never invent injuries, news, lineups, or stats. "
    "If the sample is small or the lineup is provisional, lower your confidence.\n"
    "- Prefer markets with a clear edge AND good historical reliability. Skip coin-flips.\n"
    "- Output your STRONGEST 5-8 selections, each with YOUR probability (0-1, "
    "calibrated — don't inflate), a confidence level, and a one-line data-grounded "
    "rationale that, when relevant, cites the market's historical hit-rate."
)


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _form_summary(team: dict) -> dict:
    return {
        "name": team["name"],
        "matchesSampled": team["matchesSampled"],
        "goalsFor": _avg(team["goalsFor"]),
        "goalsAgainst": _avg(team["goalsAgainst"]),
        "cornersFor": _avg(team["statFor"]["corners"]),
        "cornersAgainst": _avg(team["statAgainst"]["corners"]),
        "shotsFor": _avg(team["statFor"]["shots"]),
        "shotsAgainst": _avg(team["statAgainst"]["shots"]),
        "shotsOnTargetFor": _avg(team["statFor"]["shotsOnTarget"]),
        "yellowCardsFor": _avg(team["statFor"]["yellowCards"]),
        "foulsFor": _avg(team["statFor"]["fouls"]),
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


def build_contents(dataset: dict, engine_output: dict, reliability: dict | None = None) -> str:
    ref = dataset.get("referee") or {}
    payload = {
        "match": engine_output["event"],
        "lineupConfirmed": dataset.get("lineupConfirmed", False),
        "marketReliability": _reliability_summary(reliability),
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
            "result": engine_output["goals"]["result"],
            "expectedGoals": engine_output["goals"]["expectedGoals"],
            "overUnderGoals": engine_output["goals"]["overUnder"],
            "btts": engine_output["goals"]["btts"],
            "teamProps": {k: {s: v.get(s, {}).get("expected")
                              for s in ("home", "away", "total")}
                          for k, v in engine_output["teamProps"].items()},
        },
        "dataConfidence": engine_output["meta"]["confidence"],
    }
    return (
        "Analyze this match and return your strongest, best-calibrated betting "
        "selections. Cross-check the model's numbers against the form and flag any "
        "you'd fade.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    )


# Structured-output schema (google-genai response_schema, OpenAPI subset).
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
            "description": "Your 5-8 strongest selections, best first.",
            "items": {
                "type": "object",
                "properties": {
                    "market": {"type": "string", "description": "e.g. 'Total corners', 'Match result', 'Player shots'"},
                    "selection": {"type": "string", "description": "e.g. 'Under 9.5', 'Switzerland', 'Over 1.5'"},
                    "modelProbability": {"type": "number", "description": "Your calibrated probability 0-1."},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "rationale": {"type": "string", "description": "One line, cite the numbers."},
                },
                "required": ["market", "selection", "modelProbability", "confidence", "rationale"],
            },
        },
        "cautions": {
            "type": "array", "items": {"type": "string"},
            "description": "Data limitations / risks (small sample, provisional lineup, etc.).",
        },
    },
    "required": ["summary", "keyFactors", "topBets"],
}
