"""Build the Gemini prompt and response schema from the dataset + engine output."""
from __future__ import annotations

import json

SYSTEM_INSTRUCTION = (
    "You are a football betting analyst. You are given (1) aggregated recent-form "
    "statistics for both teams in an upcoming match and (2) probabilities already "
    "computed by a statistical model. Your job is to sanity-check and contextualize "
    "those numbers using ONLY the data provided — never invent stats, injuries, or "
    "news you were not given. Always reason about how each team's strengths interact "
    "with the opponent's weaknesses. Be concise and concrete."
)


def _form_summary(team: dict) -> dict:
    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    return {
        "name": team["name"],
        "matchesSampled": team["matchesSampled"],
        "goalsForAvg": avg(team["goalsFor"]),
        "goalsAgainstAvg": avg(team["goalsAgainst"]),
        "cornersForAvg": avg(team["statFor"]["corners"]),
        "shotsForAvg": avg(team["statFor"]["shots"]),
        "shotsOnTargetForAvg": avg(team["statFor"]["shotsOnTarget"]),
        "yellowCardsForAvg": avg(team["statFor"]["yellowCards"]),
        "foulsForAvg": avg(team["statFor"]["fouls"]),
    }


def build_contents(dataset: dict, engine_output: dict) -> str:
    payload = {
        "match": engine_output["event"],
        "lineupConfirmed": dataset.get("lineupConfirmed", False),
        "homeForm": _form_summary(dataset["home"]),
        "awayForm": _form_summary(dataset["away"]),
        "modelProbabilities": {
            "goals": engine_output["goals"],
            "teamProps": engine_output["teamProps"],
        },
    }
    return (
        "Here is the data for the match. Review the model's probabilities, flag any that "
        "look mispriced given the form, and give your best betting read.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


# Structured-output schema (google-genai response_schema, OpenAPI subset).
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentence overall read of the match."},
        "keyFactors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Bullet points on how the teams' for/against profiles interact.",
        },
        "topBets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "market": {"type": "string"},
                    "selection": {"type": "string"},
                    "modelProbability": {"type": "number"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "rationale": {"type": "string"},
                },
                "required": ["market", "selection", "rationale", "confidence"],
            },
        },
        "cautions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Data limitations or risks (small samples, unconfirmed lineup, etc.).",
        },
    },
    "required": ["summary", "keyFactors", "topBets"],
}
