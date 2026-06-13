# Copa do Mundo 2026 — Match Analyzer

A web app for **sports-betting research** on the FIFA World Cup 2026. It lists every
World Cup match (from **FotMob**), grouped by day with kickoff times in
**Brazilian time**. Expanding a match gathers both teams' recent form and computes
probabilities for a wide set of markets using a built-in **statistical engine**.
**Gemini** is an optional per-match layer that adds a narrative read on top.

> **Data source note:** the original target was SofaScore, but it **IP-blocks
> datacenter hosts** (returns a 403 "challenge" that neither TLS impersonation nor a
> headless browser bypasses), and FBref sits behind Cloudflare. **FotMob's public
> `/api/data` JSON** returns the same depth of data (shots, corners, cards, fouls,
> lineups, per-player shotmap) and works from a server without IP games.

## Markets computed

| Group | Markets |
|-------|---------|
| **Goals / result** | 1X2, double chance, over/under (0.5–4.5), both teams to score, correct score, Asian handicap |
| **Team props** | corners, total shots, shots on target, yellow cards, fouls — per team & total, over/under lines |
| **Player props** | player shots, player fouls (over/under), player cards (P ≥ 1) |
| **Most likely** | top-3 exact outcomes per section (total goals, each team's goals/corners/shots/cards/fouls, O/U, scoreline) |

Each finished match also gets an **accuracy check** (pre-match predictions vs reality,
with hit-rate and Brier score), and a **model dashboard** (`/bet-stats`, "Desempenho")
aggregates accuracy + calibration across all finished matches. **Referee** card tendency
(computed from finished WC matches, shrunk to the tournament mean) scales the card markets.

Every calculation **blends a team's "for" rate with the opponent's "against" rate**,
so the opponent always factors in. Goals use an independent-Poisson score matrix with a
Dixon-Coles low-score correction; count props use Poisson or Negative Binomial
(when the sample is overdispersed).

## How it works

1. **Match list** — `GET /api/matches` reads FotMob's World Cup fixtures
   (`leagues?id=77`) and groups them by day in `America/Sao_Paulo`.
2. **Analysis** — `GET /api/analyze/{id}` samples each team's last `RECENT_MATCHES`
   finished games (`teams?id=` → `matchDetails?matchId=`), parsing team stats, the
   per-player shotmap, playerStats (fouls), and card events; builds a normalized
   dataset and runs the engine. Heavy work is **lazy** (only on expand) and cached on disk.
3. **Refresh** — `POST /api/refresh/{id}` force-refetches the lineup (cache-bypassed).
   Pre-match lineups are provisional; once `confirmed`, player props are computed on the
   actual starting XI and the UI's Refresh button is disabled.
4. **Gemini (optional)** — `POST /api/gemini/{id}` sends the form + model probabilities to
   `gemini-2.5-flash` for a structured second opinion. Requires `GEMINI_API_KEY`.

Data comes from FotMob's public JSON API via `curl_cffi` (Chrome TLS impersonation).
No HTML scraping. Some lower-tier matches have `coverageLevel: "ratings"` (no shotmap),
so player **shot** props show "no data" for those teams until higher-coverage (World Cup)
matches accumulate — fouls and cards still work.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    |    Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optionally set GEMINI_API_KEY
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/bet-stats/** (the root `/` redirects there). The app works
without a Gemini key — the key only enables the "Analisar com Gemini" button.

> **Routing:** everything is namespaced under **`/bet-stats`** so it coexists with the
> domain's other endpoints (UI at `/bet-stats/`, APIs at `/bet-stats/api/...`). The
> top-level FastAPI app mounts the sub-app at that prefix.

## Configuration (`.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `GEMINI_API_KEY` | _(empty)_ | Enables the optional Gemini button |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model id |
| `LEAGUE_ID` / `SEASON` | `77` / `2026` | World Cup 2026 on FotMob |
| `RECENT_MATCHES` | `6` | Recent games sampled per team |
| `MAX_CONCURRENCY` | `4` | Concurrent SofaScore requests |
| `CACHE_TTL` / `CACHE_DIR` | `3600` / `.cache` | On-disk response cache |
| `DISPLAY_TZ` | `America/Sao_Paulo` | Timezone for day grouping & kickoff times |

## Production deploy (nginx + systemd)

```bash
pip install gunicorn
# 1. Put the repo at /opt/sofascore-scraper, create .venv, install requirements, set .env
# 2. systemd service:
sudo cp deploy/wc-analyzer.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wc-analyzer
# 3. nginx (edit server_name first):
sudo cp deploy/nginx.conf /etc/nginx/sites-available/wc-analyzer
sudo ln -s /etc/nginx/sites-available/wc-analyzer /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
# 4. TLS:
sudo certbot --nginx -d your-domain.com
```

## Notes & limitations

- **Betting involves risk.** These are model estimates from limited samples, not guarantees.
- National-team history is sparse; thin samples are flagged with a confidence level in the UI.
- Player props before lineup confirmation are **provisional** (most-frequent recent XI).
- Be respectful of FotMob: keep `RECENT_MATCHES`/`MAX_CONCURRENCY` modest; the cache makes repeats cheap.
