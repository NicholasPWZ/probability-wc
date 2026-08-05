"""Persistent quote store (JSON on disk).

A *quote* (orcamento) is a saved snapshot of the cart: client/title, a global markup
and a list of items ({name, supplier, url, cost, qty, markup}). Unlike supplier tokens
these are NOT secret, so they're plain JSON (no Fernet) in <cache_dir>/quotes.json.

Client-facing math lives in `client_unit`/`totals` and is mirrored by the frontend and
the PDF renderer — the reseller's cost/margin are stored but never shown to the client.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

from app.config import get_settings

_lock = threading.Lock()


def _store_file() -> Path:
    p = Path(get_settings().cache_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / "quotes.json"


def _load() -> dict:
    fp = _store_file()
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(data: dict) -> None:
    fp = _store_file()
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(fp)


# --- quote math (kept in sync with clientUnit/renderCart in index.html) -----
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def eff_markup(item: dict, global_markup: float) -> float:
    m = _num(item.get("markup"))
    return m if m is not None else global_markup


def client_unit(item: dict, global_markup: float):
    """Client unit price. A manual override (`cprice`, the reseller's clean value) wins;
    otherwise cost * (1 + effective_markup/100). None if neither is available."""
    o = _num(item.get("cprice"))
    if o is not None:
        return round(o, 2)
    c = _num(item.get("cost"))
    if c is None:
        return None
    return round(c * (1 + eff_markup(item, global_markup) / 100.0), 2)


def totals(quote: dict) -> dict:
    """Client total + reseller cost/profit (profit is internal, for the list view)."""
    gm = _num(quote.get("markup")) or 0.0
    tot_client = 0.0
    tot_cost = 0.0
    n = 0
    for it in quote.get("items", []):
        qty = int(_num(it.get("qty")) or 1)
        cu = client_unit(it, gm)
        c = _num(it.get("cost"))
        if cu is not None:
            tot_client += cu * qty
        if c is not None:
            tot_cost += c * qty
        n += qty
    return {"total": round(tot_client, 2), "cost": round(tot_cost, 2),
            "profit": round(tot_client - tot_cost, 2), "units": n}


# --- public API -------------------------------------------------------------
def _summary(q: dict) -> dict:
    t = totals(q)
    return {"id": q["id"], "number": q.get("number"), "title": q.get("title") or "",
            "createdAt": q.get("createdAt"), "updatedAt": q.get("updatedAt"),
            "items": len(q.get("items", [])), "total": t["total"], "profit": t["profit"]}


def list_quotes() -> list[dict]:
    with _lock:
        data = _load()
    rows = data.get("quotes", [])
    rows = sorted(rows, key=lambda q: q.get("updatedAt") or q.get("createdAt") or 0, reverse=True)
    return [_summary(q) for q in rows]


def get_quote(qid: str) -> dict | None:
    with _lock:
        for q in _load().get("quotes", []):
            if q["id"] == qid:
                return q
    return None


def save_quote(payload: dict) -> dict:
    """Create (no id) or update (existing id). Returns the stored quote."""
    now = int(time.time())
    title = (payload.get("title") or "").strip()
    seller = (payload.get("seller") or "").strip()
    seller_email = (payload.get("sellerEmail") or "").strip()
    notes = (payload.get("notes") or "").strip()
    markup = _num(payload.get("markup")) or 0.0
    final_only = bool(payload.get("finalOnly"))
    final_name = (payload.get("finalName") or "").strip()
    items = []
    for it in (payload.get("items") or []):
        items.append({
            "name": (it.get("name") or "").strip() or "(sem nome)",
            "dname": (it.get("dname") or "").strip(),   # nome no PDF (opcional; vazio = usa o original)
            "supplier": (it.get("supplier") or "").strip(),
            "url": (it.get("url") or "").strip(),
            "cost": _num(it.get("cost")),
            "qty": int(_num(it.get("qty")) or 1),
            "markup": ("" if it.get("markup") in (None, "") else _num(it.get("markup"))),
            "cprice": ("" if it.get("cprice") in (None, "") else _num(it.get("cprice"))),
        })
    with _lock:
        data = _load()
        rows = data.setdefault("quotes", [])
        qid = payload.get("id")
        row = next((q for q in rows if q["id"] == qid), None) if qid else None
        if row is None:
            seq = int(data.get("seq") or 0) + 1
            data["seq"] = seq
            row = {"id": secrets.token_hex(6), "number": f"{seq:04d}", "createdAt": now}
            rows.append(row)
        row["title"] = title
        row["seller"] = seller
        row["sellerEmail"] = seller_email
        row["notes"] = notes
        row["markup"] = markup
        row["finalOnly"] = final_only
        row["finalName"] = final_name
        row["items"] = items
        row["updatedAt"] = now
        _save(data)
        return row


def delete_quote(qid: str) -> None:
    with _lock:
        data = _load()
        data["quotes"] = [q for q in data.get("quotes", []) if q["id"] != qid]
        _save(data)
