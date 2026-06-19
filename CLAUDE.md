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

**Medido em 28 jogos finalizados e MANTIDO** (validado no `/api/dashboard` ao vivo):
- `COUNT_CAL_K=0.90` — encolhe a confiança das O/U de contagem (time/jogador) em direção a 0.5
  **depois** de escolher o lado (não muda hit-rate; só calibra). Corrige superconfiança medida no
  topo (90-100% previa .93/acertava .88). callBrier .1862→.1855. Risco ~zero.
- `GOALS_CORRECTION=1.08` — gols eram **sub-previstos** (média prevista ~2.63 vs real ~3.18, ~17%).
  Escala as duas médias de gols. **BTTS subiu .46→.64**, Brier do 1X2 .618→.607, Gols O/U estável.
  FLAG: remedir conforme mais jogos terminam e no mata-mata.
- `HFA=1.04` — vantagem do time listado primeiro (sede neutra na Copa → na real captura seeding que
  falta). Modelo escolhia empate 0/28 e exagerava no "away". Conservador (1.04) pega o ganho de
  Brier do 1X2 sem overfit do artefato de ordem-de-listagem. FLAG: arrumar de verdade com feature
  de ranking/seeding, não multiplicador posicional.

**Testado e NÃO ajudou / NÃO manter** (revertido): Elo de seleções e xG (pioram na amostra caótica
de fase de grupos — talvez ajudem no mata-mata, remedir depois). Resfriar cartão de **jogador**
(`PLAYER_CARD_SHRINK_K`) esvazia o mercado (34→12 calls abaixo do limiar de 30%; o "ganho" .27→.50 é
artefato de denominador móvel) — **confirmado, não fazer** (knob fica no código como no-op).
Corrigir o viés de **faltas** (~-7%) corrigia o viés mas **baixava** o acerto de faltas e o geral —
não manter.

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

### Gemini — dados, prompt e avaliação (enriquecido)
- O prompt (`prompt.py` `build_contents`) agora envia **muito mais dado**: `playerProps` (top 6/lado,
  com linhas que a engine precificou — antes a IA não recebia NADA de jogador, então inventava),
  `calibrationContext` (viés medido previsto-vs-real + split over/under como **priors soft**),
  `resultPrior` (taxa de empate ~36%), `availableLines`, taxas **contra** do adversário por stat,
  `doubleChance` e `teamProps` com `{expected, lines}` por escopo. Prompt ~26KB.
- Cada palpite carrega campos **estruturados gradeáveis**: `marketKey` (enum), `side`, `line`,
  `scope`, `playerId`/`playerName`. `_grade_ai_bet` usa eles (fallback regex/nome só p/ registros
  antigos sem `marketKey`). **`/api/ai-performance` agora avalia TODAS as análises individuais E o
  consenso** (não só o consenso), com breakdown por mercado + calibração/Brier da IA + contagem de
  não-graváveis. UI mostra dois painéis (análises individuais + consenso).
- **GOTCHA**: `response_schema` é subset OpenAPI. Após mudar o schema, **rodar 1 call real do Gemini**
  pra confirmar que ele aceita os enums/`playerId` int — se rejeitar, o call **500a** (histórico).
  Não dá pra testar local sem `GEMINI_API_KEY`.

## Convenções de commit
Commitar **como o usuário (Nicholas)**, **sem `Co-Authored-By` / sem referência a Claude**.
**O push é do usuário** — não dar `git push`. Mensagens em português, sem acentos (evita problema de
encoding no shell). Não commitar arquivos de scratch/debug (`*.json` temporários) nem `.env`/`.cache/`.
