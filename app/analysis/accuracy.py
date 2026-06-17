"""Evaluate how accurate the pre-match predictions were for a finished match.

``extract_actuals`` reads what actually happened (score, team stats, per-player
shots/fouls/cards) from the match's own data. ``evaluate`` compares the model's
pre-match predictions against those actuals and scores them:
  * result (1X2): was the favorite right? probability assigned to the real outcome,
    and a Brier score.
  * over/under goals & BTTS: did the model's side (>50%) hit?
  * team props & player props: per-line over/under hits + expected-vs-actual error.
A headline hit-rate aggregates every binary line call.
"""
from __future__ import annotations

from app.scraper.aggregator import (
    _num, _parse_cards, _parse_player_stats, _parse_shotmap, _parse_team_stats,
)
from app.analysis.engine import STAT_LABELS


def extract_actuals(md: dict) -> dict:
    teams = (md.get("header") or {}).get("teams") or []
    home, away = teams[0], teams[1]
    hg = _num(home.get("score")) or 0
    ag = _num(away.get("score")) or 0
    total = hg + ag
    result = "home" if hg > ag else ("away" if ag > hg else "draw")

    stats = _parse_team_stats(md)
    team_stats = {k: {"home": v[0], "away": v[1], "total": v[0] + v[1]} for k, v in stats.items()}

    shotmap = _parse_shotmap(md)
    has_shotmap = bool(shotmap)
    pstats = _parse_player_stats(md)
    cards = _parse_cards(md)
    players: dict[int, dict] = {}
    for pid, p in pstats.items():
        if p["minutes"] <= 0:
            continue
        players[pid] = {
            "name": p["name"],
            "teamId": p["teamId"],
            "shots": (shotmap.get(pid, 0) if has_shotmap else None),
            "fouls": p["fouls"],
            "yellow": cards.get(pid, 0),
        }
    return {
        "goals": {"home": hg, "away": ag, "total": total, "result": result,
                  "btts": hg > 0 and ag > 0},
        "teamStats": team_stats,
        "players": players,
        "hasShotmap": has_shotmap,
    }


def _side_correct(over_prob: float, actual_over: bool) -> bool | None:
    if abs(over_prob - 0.5) < 1e-9:
        return None  # model has no lean
    return (over_prob > 0.5) == actual_over


def _eval_lines(lines: dict, actual_value: float) -> list[dict]:
    out = []
    for line_str, probs in lines.items():
        line = float(line_str)
        actual_over = actual_value > line
        correct = _side_correct(probs["over"], actual_over)
        out.append({
            "line": line_str,
            "predOver": probs["over"],
            "modelSide": "over" if probs["over"] > 0.5 else "under",
            "actualOver": actual_over,
            "correct": correct,
        })
    return out


def evaluate(pred: dict, actuals: dict) -> dict:
    a_goals = actuals["goals"]
    hits = 0
    total = 0
    calls: list[dict] = []  # one record per binary call, for the aggregate dashboard

    def add_call(market: str, conf: float, correct: bool | None, side: str | None = None):
        if correct is not None:
            calls.append({"market": market, "conf": conf, "correct": correct, "side": side})

    def tally(items, market: str | None = None):
        nonlocal hits, total
        for it in items:
            if it["correct"] is not None:
                total += 1
                hits += 1 if it["correct"] else 0
                if market:
                    add_call(market, max(it["predOver"], 1 - it["predOver"]), it["correct"], it["modelSide"])

    # --- result (1X2) ---
    res = pred["goals"]["result"]
    pick = max(res, key=res.get)
    actual_outcome = a_goals["result"]
    brier = sum((res[k] - (1.0 if k == actual_outcome else 0.0)) ** 2 for k in ("home", "draw", "away"))
    result_eval = {
        "predicted": pick,
        "predictedProb": res[pick],
        "actual": actual_outcome,
        "probOfActual": res[actual_outcome],
        "correct": pick == actual_outcome,
        "brier": round(brier, 4),
        "scoreStr": f"{int(a_goals['home'])}-{int(a_goals['away'])}",
    }

    add_call("1X2", result_eval["predictedProb"], result_eval["correct"], "pick")

    # --- over/under goals ---
    ou = _eval_lines(pred["goals"]["overUnder"], a_goals["total"])
    tally(ou, "Gols O/U")

    # --- BTTS ---
    btts_yes = pred["goals"]["btts"]["yes"]
    btts_correct = _side_correct(btts_yes, a_goals["btts"])
    if btts_correct is not None:
        total += 1
        hits += 1 if btts_correct else 0
    add_call("Ambos marcam", max(btts_yes, 1 - btts_yes), btts_correct, "yes" if btts_yes > 0.5 else "no")
    btts_eval = {"predYes": btts_yes, "actual": a_goals["btts"],
                 "modelSide": "yes" if btts_yes > 0.5 else "no", "correct": btts_correct}

    # --- team props ---
    team_props = {}
    for key, p in pred["teamProps"].items():
        actual = actuals["teamStats"].get(key)
        if not actual:
            continue
        entry = {"label": STAT_LABELS.get(key, key), "actual": actual, "scopes": {}}
        for scope in ("home", "away", "total"):
            if scope in p and "lines" in p[scope]:
                line_evals = _eval_lines(p[scope]["lines"], actual[scope])
                tally(line_evals, entry["label"])
                entry["scopes"][scope] = {
                    "expected": p[scope]["expected"],
                    "actual": actual[scope],
                    "absError": round(abs(p[scope]["expected"] - actual[scope]), 2),
                    "lines": line_evals,
                }
        team_props[key] = entry

    # --- player props ---
    player_hits = 0
    player_total = 0
    players_actual: dict[str, dict] = {}
    for side in ("home", "away"):
        for pp in pred["playerProps"].get(side, []):
            act = actuals["players"].get(pp["playerId"])
            if not act:
                continue
            players_actual[str(pp["playerId"])] = {
                "shots": act["shots"], "fouls": act["fouls"], "yellow": act["yellow"],
            }
            if pp.get("shots", {}).get("lines") and act["shots"] is not None:
                for it in _eval_lines(pp["shots"]["lines"], act["shots"]):
                    if it["correct"] is not None:
                        player_total += 1
                        player_hits += 1 if it["correct"] else 0
                        add_call("Chutes (jogador)", max(it["predOver"], 1 - it["predOver"]), it["correct"], it["modelSide"])
            if pp.get("fouls", {}).get("lines"):
                for it in _eval_lines(pp["fouls"]["lines"], act["fouls"]):
                    if it["correct"] is not None:
                        player_total += 1
                        player_hits += 1 if it["correct"] else 0
                        add_call("Faltas (jogador)", max(it["predOver"], 1 - it["predOver"]), it["correct"], it["modelSide"])
            card_prob = pp.get("card", {}).get("probAtLeastOne")
            # Score only the bettable "recebe cartão" side, and only when the model
            # flags a real card risk (>=30%). Otherwise the metric is dominated by the
            # trivial (and almost always correct) "won't be carded" majority -> fake 90%.
            if card_prob is not None and card_prob >= 0.30:
                got = act["yellow"] >= 1
                player_total += 1
                player_hits += 1 if got else 0
                add_call("Cartões (jogador)", card_prob, got, "yes")

    # --- grade the model's ACTUAL headline predictions, so the review shows exactly
    #     the same picks that were displayed pre-match (not re-picked lines). ---
    ev = pred.get("event") or {}
    home_name = (ev.get("home") or {}).get("name")
    away_name = (ev.get("away") or {}).get("name")
    label_to_key = {v: k for k, v in STAT_LABELS.items()}

    def _grade(p):
        cat, sel = p.get("category"), p.get("selection", "")
        if cat == "1X2":
            actual_name = {"home": home_name, "draw": "Empate", "away": away_name}.get(a_goals["result"])
            return f"{result_eval['scoreStr']} ({actual_name})", sel == actual_name
        if cat == "Gols O/U":
            parts = sel.split()
            side, line, tot = parts[0], float(parts[1]), a_goals["total"]
            return f"{int(tot)} gols", (side == "Over") == (tot > line)
        if cat == "Ambos marcam":
            actual = "Sim" if a_goals["btts"] else "Não"
            return actual, sel == actual
        key = label_to_key.get(cat)
        if key and key in actuals["teamStats"]:
            mkt = p.get("market", "")
            scope = "total" if mkt.endswith("Total") else ("home" if (home_name and home_name in mkt) else "away")
            val = actuals["teamStats"][key].get(scope)
            if val is None:
                return None, None
            parts = sel.split()
            side, line = parts[0], float(parts[1])
            return f"{val:g}", (side == "Over") == (val > line)
        return None, None

    prediction_results = []
    pc_hits = pc_total = 0
    for p in pred.get("predictions", []):
        actual_str, correct = _grade(p)
        if actual_str is None:
            continue
        if correct is not None:
            pc_total += 1
            pc_hits += 1 if correct else 0
        prediction_results.append({
            "market": p["market"], "selection": p["selection"], "prob": p.get("prob"),
            "expected": p.get("expected"), "actual": actual_str, "correct": correct,
        })

    return {
        "result": result_eval,
        "overUnderGoals": ou,
        "btts": btts_eval,
        "teamProps": team_props,
        "playersActual": players_actual,
        "predictionResults": prediction_results,
        "calls": calls,
        "summary": {
            "resultCorrect": result_eval["correct"],
            "brier": result_eval["brier"],
            "marketLineHits": hits,
            "marketLineTotal": total,
            "marketHitRate": round(hits / total, 3) if total else None,
            "predHits": pc_hits,
            "predTotal": pc_total,
            "predHitRate": round(pc_hits / pc_total, 3) if pc_total else None,
            "playerLineHits": player_hits,
            "playerLineTotal": player_total,
            "playerHitRate": round(player_hits / player_total, 3) if player_total else None,
        },
    }
