# CLAUDE.md — Compubot (comparador de fornecedores)

App **web para revendedor** comparar o **preço de um produto em vários fornecedores** ao mesmo
tempo. O revendedor cola o **token/cookie de autenticação** de cada fornecedor (logado) para o
scraper enxergar o **preço de revenda** (que só aparece logado). Busca por **texto livre**, resultados
lado a lado por fornecedor. Stack: **Python/FastAPI** atrás de **nginx**, na mesma VPS/URL de antes.

> Histórico: este repo era um app de apostas da Copa 2026 (FotMob + engine estatística + Gemini).
> Foi **pivotado**; todo o código antigo está no histórico do git (não recriar). Reaproveitado:
> `app/scraper/client.py` (curl_cffi com impersonate Chrome), o esqueleto FastAPI, o shell da UI e o
> deploy (nginx + systemd + TLS na mesma URL).

## Como funciona
- **Login**: senha única (`APP_PASSWORD`) → cookie de sessão assinado por HMAC (`app/auth.py`).
- **Fornecedores**: cada site é um **adapter** em `app/suppliers/` que sabe (a) montar uma sessão
  autenticada a partir do token salvo, (b) buscar por texto e parsear produtos/preços, (c) checar se
  a sessão ainda está logada. Registrados em `app/suppliers/__init__.py`.
- **Token**: o revendedor cola o token (ex.: o header `cookie` inteiro). Guardado **criptografado**
  (Fernet) em `.cache/suppliers.json` (gitignored). Chave em `FERNET_KEY` ou auto-gerada em
  `.cache/fernet.key`.
- **Renovação sem novo login**: depois de cada uso, o adapter re-serializa o cookie jar e persiste
  qualquer token renovado (`dump_token`). Para sessões de cookie deslizante (ASP.NET/nopCommerce),
  isso mantém a sessão viva enquanto o app é usado. **On-demand**: renova no uso; sem scheduler.
- **Busca**: `GET /api/search?q=` faz fan-out concorrente (ThreadPoolExecutor) em todos os
  fornecedores ativos com token, cacheia por `SEARCH_TTL` (120s), devolve produtos por fornecedor.

## Adapter: pauta.com.br (`app/suppliers/pauta.py`) — validado ao vivo
- Loja **nopCommerce (ASP.NET Core)**. Auth = cookie **`.Nop.Authentication`** (sem JWT/refresh).
  Sessão **deslizante** → renova recapturando o `Set-Cookie`. Token pasteado funciona **do servidor**
  (sem bind de IP).
- **GOTCHA**: cookies têm que ir no **jar** (não só header) senão o 301 `www`→apex os derruba. Sempre
  usar o apex `https://pauta.com.br`.
- Busca: `GET /search?q=TERM` (server-rendered). Produtos em `div.product-item.cardProduct` com
  `data-productid`; título em `h3.title a[title]`; url no `href`; img `src`; marca/SKU/part-number.
  **Preço é uma TABELA em tiers**: linhas `UF | QTD | Preço | ST` — o parser pega o **menor preço** e
  soma o estoque. Formato de moeda BR (vírgula decimal).
- Detecção de expiração: se a busca volta produtos **sem preço**, provavelmente deslogou → UI mostra
  aviso e o botão "Testar" confirma via `GET /customer/info` (302→/login = expirado).

## Layout / rotas
- `app/main.py` (rotas), `app/auth.py` (senha+sessão), `app/store.py` (JSON + Fernet),
  `app/config.py` (env), `app/suppliers/` (base + adapters), `app/scraper/client.py` (cliente
  curl_cffi genérico, reaproveitado), `app/static/index.html` (SPA, JS puro, tema escuro).
- Rotas (todas exigem sessão exceto config/login): `GET /`, `GET /api/config`, `POST /api/login`,
  `POST /api/logout`, `GET /api/adapters`, `GET/PUT /api/suppliers`, `POST /api/suppliers/{key}/token`,
  `POST /api/suppliers/{key}/test`, `DELETE /api/suppliers/{key}`, `POST /api/sites` (criar/editar site
  genérico), `POST /api/sites/test` (parse a seco), `GET /api/search?q=`. `app = compubot`.
- UI (SPA em `index.html`, tudo client-side): login → 3 abas.
  - **Buscar**: uma busca → **tabela unificada de TODOS os fornecedores** (thumb, produto c/ link,
    fornecedor, estoque, preço, "abrir"), **ordenável** (Preço ↑/↓, Nome, Fornecedor, Estoque). Botão
    "+ carrinho" por item.
  - **Carrinho** (quoting, `localStorage` — sem backend): itens com custo/qtd/margem editáveis,
    **margem % padrão** + override por item → calcula **preço ao cliente**, mostra custo/total/lucro,
    "Copiar orçamento" (texto pronto pro cliente).
  - **Fornecedores**: adicionar adapter embutido + token; **criar site por seletores** (com Testar);
    testar sessão; ativar/remover; editar sites genéricos.

## Sites configuráveis pelo usuário (sem código) — `app/suppliers/generic.py`
A UI (aba Fornecedores → "Criar / editar site por seletores") permite adicionar qualquer loja
**HTML server-rendered** informando: `searchUrl` (com `{q}`), modo de auth (cookie/header/none),
formato de preço (br/us) e **seletores CSS** (item, nome, preço, link, imagem, estoque). Salvo como
`kind:"generic"` com `config` no store (token criptografado). `GenericAdapter` faz o scraping via
BeautifulSoup (`bs4`, dep nova). Botão **Testar** (`POST /api/sites/test`) faz um parse a seco e mostra
os itens/preços pra ajustar os seletores antes de salvar (`POST /api/sites`).
- **Limite honesto**: só funciona em sites **server-rendered**. Sites JS-rendered (produtos via
  AJAX/JSON) precisam de adapter em código. Auth pelo modo cookie (jar) ou header ("Nome: valor").
- `_resolve_adapter(key)` em `main.py` devolve o adapter embutido OU um `GenericAdapter` do config.

## Adicionar um novo fornecedor — 2 caminhos
1. **Pela UI (fácil)**: criar um "site por seletores" (acima). Serve pra lojas server-rendered simples.
2. **Por código (robusto)**: criar `app/suppliers/<nome>.py` com uma `SupplierAdapter` (`build_session`,
   `dump_token`, `check_auth`, `search`), registrar em `app/suppliers/__init__.py`. Necessário quando o
   site é JS-rendered, tem preço em estrutura complexa (ex.: tabela em tiers como o pauta) ou auth
   especial. Descobrir auth/preço inspecionando o tráfego real (HAR / "Copy as cURL").

## Rodar local (Windows)
```
$env:APP_PASSWORD="uma-senha"; $env:SECRET_KEY="algo-aleatorio"
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```
`.env` (gitignored): `APP_PASSWORD`, `SECRET_KEY`, `FERNET_KEY` (opcional), `SEARCH_TTL` (opcional).

## Deploy (mesma VPS/URL)
VPS RHEL, systemd (gunicorn+uvicorn) atrás do nginx em `127.0.0.1:8001`, TLS certbot, mesma URL.
`git pull && sudo systemctl restart <serviço>`. Definir `APP_PASSWORD`/`SECRET_KEY`/`FERNET_KEY` no
`.env` da VPS (senão os tokens salvos não descriptografam entre restarts — sem `FERNET_KEY` fixo, use
o `.cache/fernet.key` gerado, que deve persistir).

## Segurança / realidade
- Tokens de fornecedor são **secretos** → criptografados em repouso; `.cache/` é gitignored. Nunca
  commitar `.cache/`, `.env`, nem tokens.
- Scraping por-fornecedor é **frágil** e específico: quebra quando o site muda; captcha/2FA/anti-bot
  podem impedir. curl_cffi (impersonate Chrome) ajuda com Cloudflare/TLS, não com captcha.
- É acesso à **conta do próprio revendedor** (autorizado). Cada fornecedor novo = manutenção contínua.

## Convenções de commit
Commitar **como o usuário (Nicholas)**, sem `Co-Authored-By`/referência a Claude. **O push é do
usuário.** Mensagens em português sem acento (evita encoding no shell). Não commitar scratch/`.env`/`.cache/`.
