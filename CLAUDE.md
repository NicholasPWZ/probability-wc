# CLAUDE.md — Contexto do projeto

App de **pesquisa para apostas** na **Copa do Mundo 2026**. A página lista todos os jogos
agrupados por dia (horário de Brasília); ao expandir um jogo, mostra **probabilidades calculadas
por uma engine estatística própria** para todos os mercados. Stack: **Python/FastAPI** atrás de
**nginx**, em VPS. Rodando em produção: **https://compubot.online**.

## Princípio central
A **engine estatística própria** (`app/analysis/engine.py`) é o analista primário. **O Gemini é
opcional** — só roda quando o usuário clica "deixar a IA analisar" (botão por jogo), como segunda
opinião sobre os números calculados. O app funciona 100% sem `GEMINI_API_KEY`. Lógica de análise
nova vai na engine, não no prompt.

## Fonte de dados
**FotMob `/api/data`** (matchDetails, leagues?id=77 temporada 2026, teams?id=) via `curl_cffi`
(impersonate Chrome). **SofaScore, FBref e RedScores são bloqueados** (Cloudflare/IP de datacenter)
— não tentar voltar pra eles. Cache em disco em `.cache/` (gitignored).

## Mercados (todos calculados pela engine)
- **Gols/resultado**: 1X2, dupla chance, over/under, ambos marcam, placar exato, handicap asiático
  (matriz de placar Poisson + Dixon-Coles, `DC_RHO=-0.05`).
- **Mercados de time**: escanteios, finalizações, chutes no alvo, cartões, faltas (Poisson/NegBin O/U).
- **Mercados de jogador**: chutes, faltas (O/U), cartões (P>=1). XI confirmado quando a escalação
  é confirmada; senão, seleção por form recente (provisório).
- Todo cálculo **combina o "contra" do adversário** com o "a favor" do time.

## Engine — calibração atual (o que ficou, e ajudou)
Constantes em `app/analysis/engine.py`: `STRENGTH_SHRINK_K=5` (encolhe força à média),
`DISPERSION_FLOOR=1.4` (piso de superdispersão NegBin), `PROB_CAP=0.93` (teto de confiança),
`GOALS_CAL_K=0.85` (calibração das binárias de gols),
`STAT_CORRECTION={"shots": 1.12, "yellowCards": 0.80}` (correção estrutural: chutes +12%,
**resfriamento de cartões de time -20%**). Cartão de jogador pontuado de forma honesta (só o lado
"recebe cartão" para risco >=30%).

**Testado e NÃO ajudou** (revertido): Elo de seleções e xG (pioram na amostra caótica de fase de
grupos — talvez ajudem no mata-mata, remedir depois). Resfriar cartão de **jogador** esvazia o
mercado (joga todos abaixo do limiar de 30%) — não fazer.

## Como melhorar o modelo (regra)
Toda mudança na engine deve ser **medida antes de manter**: rodar local → reiniciar →
`GET /api/dashboard` (avalia jogos finalizados: acerto por mercado, calibração, **viés
previsto-vs-real**, Brier, geral) → comparar antes/depois → **manter só se melhorar** sem degradar
o resto. Se não melhora, reverter. (O dashboard em frio retorna 0 avaliados; aquecer `/api/matches`
primeiro. Chaves de mercado têm acento — buscar por substring, não string literal, em scripts.)

## Layout / endpoints
- `app/scraper/` (client, endpoints, fixtures, aggregator), `app/analysis/` (engine, gemini, prompt,
  accuracy, referee), `app/runtime.py` (override de config em runtime → `.cache/runtime.json`),
  `app/main.py` (rotas), `app/static/index.html` (UI single-page, JS puro).
- Rotas: `/api/matches`, `/api/analyze/{id}`, `/api/refresh/{id}`, `/api/gemini/{id}` (GET+POST),
  `/api/dashboard`, `/api/reliability`, `/api/best-bets`, `/api/ai-performance`, `/api/config`,
  `/api/settings/gemini`. `app = betstats`, servido na **raiz**.
- Features na UI: lista por dia + atualizar escalação; detalhe em sub-abas (Resumo/Gols/Mercados/
  Jogadores/IA) + destaques (top-3 + combos) + copiar; melhores palpites (mercados/jogadores,
  ordenável, valor/EV com odds manuais, caps de variedade); confiabilidade por mercado; desempenho
  (calibração/viés/Brier/por jogo); IA Gemini (2 análises + 1 consenso persistido) + desempenho da
  IA por confiança; painel de config (editor de chave/modelo guardado por `ADMIN_TOKEN`). Tema escuro
  enterprise, ícones SVG, mobile (tabelas viram cards).

## Rodar local (Windows)
```
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## Deploy
VPS RHEL, código em `/opt/probability-wc`, systemd `probability-wc` (gunicorn+uvicorn worker) em
`127.0.0.1:8001`, nginx proxia `/` → `:8001`, TLS certbot. Repo:
https://github.com/NicholasPWZ/probability-wc.
Atualizar: `cd /opt/probability-wc && git pull && sudo systemctl restart probability-wc`.
`.env` é gitignored (criado na VPS): `GEMINI_API_KEY`, `GEMINI_MODEL`, `ADMIN_TOKEN`.

### Gemini — config e gotchas
- `GEMINI_MODEL` válido: `gemini-2.5-flash` (default), `gemini-2.0-flash`, `gemini-1.5-flash`.
  **`gemini-3.5-flash` NÃO existe** → causava **HTTP 500** em todo call. `gemini.py` agora captura
  o erro real da API e devolve 503 com mensagem clara que a UI mostra.
- `ADMIN_TOKEN` (default `""`): vazio = **editor de chave na UI desativado** (`/api/settings/gemini`
  → 403). Definir um segredo no `.env` pra liberar; sem ele, trocar a chave só via `.env` + restart.

## Convenções de commit
Commitar **como o usuário (Nicholas)**, **sem `Co-Authored-By` / sem referência a Claude**.
**O push é do usuário** — não dar `git push`. Mensagens em português, sem acentos (evita problema de
encoding no shell). Não commitar arquivos de scratch/debug (`*.json` temporários) nem `.env`/`.cache/`.
