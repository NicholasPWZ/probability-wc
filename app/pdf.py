"""Render a saved quote to a client-facing PDF (fpdf2).

The client PDF shows only what the customer should see: header (CompuJob logo, number,
date), an optional client name, an itemized table (Produto | Qtd | Preco unit. | Subtotal)
and the total. The reseller's COST/MARGIN/PROFIT are never printed.

fpdf2 core font (Helvetica) is Latin-1, which covers Portuguese accents; `_safe` maps the
few common non-Latin-1 glyphs that leak in from supplier product names (dashes, bullets,
™, ², curly quotes) and replaces anything else so a weird listing never crashes the export.
"""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

from app import quotes
from app.config import get_settings

_LOGO = Path(__file__).parent / "static" / "compujob-logo.png"

# accent (teal) + neutral grays
_ACCENT = (13, 148, 136)
_DARK = (31, 41, 55)
_MUTED = (107, 114, 128)
_HEAD_BG = (241, 245, 249)
_ZEBRA = (249, 250, 251)
_LINE = (226, 232, 240)

_MAP = {"–": "-", "—": "-", "•": "-", "·": "-", "‘": "'",
        "’": "'", "“": '"', "”": '"', "…": "...", "™": "(TM)",
        "®": "(R)", "₂": "2", "²": "2", "³": "3", "×": "x",
        "→": "->", " ": " "}


def _safe(s) -> str:
    s = str(s if s is not None else "")
    for k, v in _MAP.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def _brl(v) -> str:
    if v is None:
        return "-"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _date(ts) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%d/%m/%Y")
    except Exception:
        return ""


class _Quote(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", size=8)
        self.set_text_color(*_MUTED)
        self.cell(0, 6, _safe("Gerado por Compubot - CompuJob"), align="L")
        self.cell(0, 6, _safe(f"Página {self.page_no()}/{{nb}}"), align="R")


def render_quote_pdf(quote: dict) -> bytes:
    gm = quotes.totals(quote)
    pdf = _Quote(orientation="P", unit="mm", format="A4")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_margins(15, 14, 15)
    pdf.add_page()

    cfg = get_settings()

    # ---- header: logo + empresa (esq) | ORCAMENTO/No/data (dir) -----------
    top = pdf.get_y()
    if _LOGO.exists():
        try:
            pdf.image(str(_LOGO), x=15, y=top, h=16)
        except Exception:
            pass
    pdf.set_xy(41, top)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*_DARK)
    pdf.cell(0, 8, _safe(cfg.company_name or "CompuJob"), ln=1)
    # linhas de contato da empresa (so as preenchidas no .env)
    info = []
    if (cfg.company_address or "").strip():
        info.append(cfg.company_address.strip())
    phones = " - ".join(x for x in [
        ("Tel: " + cfg.company_phone.strip()) if (cfg.company_phone or "").strip() else "",
        ("Cel: " + cfg.company_mobile.strip()) if (cfg.company_mobile or "").strip() else "",
    ] if x)
    if phones:
        info.append(phones)
    if (cfg.company_cnpj or "").strip():
        info.append("CNPJ: " + cfg.company_cnpj.strip())
    pdf.set_font("Helvetica", size=8.5)
    pdf.set_text_color(*_MUTED)
    iy = top + 8.5
    for line in info:
        pdf.set_xy(41, iy)
        pdf.cell(74, 4, _safe(line))
        iy += 4

    pdf.set_xy(120, top)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*_ACCENT)
    pdf.cell(75, 8, "ORÇAMENTO", align="R", ln=1)
    pdf.set_x(120)
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(*_DARK)
    pdf.cell(75, 5, _safe(f"Nº {quote.get('number','')}"), align="R", ln=1)
    pdf.set_x(120)
    pdf.set_text_color(*_MUTED)
    pdf.cell(75, 5, _safe("Data: " + _date(quote.get("updatedAt") or quote.get("createdAt"))), align="R", ln=1)

    div_y = max(top + 20, iy + 1)
    pdf.set_y(div_y)
    pdf.set_draw_color(*_LINE)
    pdf.line(15, div_y, 195, div_y)
    pdf.ln(4)

    # ---- cliente | vendedor (blocos opcionais) ---------------------------
    client = (quote.get("title") or "").strip()
    seller = (quote.get("seller") or "").strip()
    seller_email = (quote.get("sellerEmail") or "").strip()
    if client or seller or seller_email:
        by = pdf.get_y()
        if client:
            pdf.set_xy(15, by)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*_MUTED)
            pdf.cell(90, 4, _safe("CLIENTE"))
            pdf.set_xy(15, by + 4.5)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(*_DARK)
            pdf.cell(90, 6, _safe(client))
        if seller or seller_email:
            pdf.set_xy(110, by)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*_MUTED)
            pdf.cell(85, 4, _safe("VENDEDOR"))
            yy = by + 4.5
            if seller:
                pdf.set_xy(110, yy)
                pdf.set_font("Helvetica", "B", 10.5)
                pdf.set_text_color(*_DARK)
                pdf.cell(85, 5.5, _safe(seller))
                yy += 5.5
            if seller_email:
                pdf.set_xy(110, yy)
                pdf.set_font("Helvetica", size=9)
                pdf.set_text_color(*_MUTED)
                pdf.cell(85, 5, _safe(seller_email))
        pdf.set_y(by + 16)

    # ---- itens ----------------------------------------------------------
    # "exibir apenas preco final" esconde o preco unitario. Com um "nome do conjunto" (final_name)
    # vira modo KIT: uma linha "NOME -> R$ total" e os componentes listados SEM preco.
    final_only = bool(quote.get("finalOnly"))
    final_name = (quote.get("finalName") or "").strip() if final_only else ""

    if final_name:
        kit_qty = gm["kitQty"]
        yb = pdf.get_y()
        pdf.set_fill_color(*_HEAD_BG)
        pdf.rect(15, yb, 180, 13, style="F")
        pdf.set_xy(18, yb + (2.5 if kit_qty > 1 else 3))
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(*_DARK)
        pdf.cell(115, 7, _safe(final_name))
        if kit_qty > 1:   # o conjunto virou produto: mostra qtd x preco unitario
            pdf.set_xy(18, yb + 8.3)
            pdf.set_font("Helvetica", size=8.5)
            pdf.set_text_color(*_MUTED)
            pdf.cell(100, 4, _safe(f"{kit_qty} x {_brl(gm['unit'])}"))
        pdf.set_xy(115, yb + 2.5)
        pdf.set_font("Helvetica", "B", 15)
        pdf.set_text_color(*_ACCENT)
        pdf.cell(77, 8, _brl(gm["total"]), align="R")
        pdf.set_xy(15, yb + 16)
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_text_color(*_MUTED)
        pdf.cell(0, 5, _safe("ITENS INCLUSOS"), ln=1)
        pdf.set_font("Helvetica", size=10)
        pdf.set_text_color(*_DARK)
        for it in quote.get("items", []):
            qty = int(it.get("qty") or 1)
            nm = (it.get("dname") or "").strip() or it.get("name") or "(sem nome)"
            prefix = f"{qty}x " if qty > 1 else ""
            pdf.set_x(17)
            pdf.multi_cell(178, 5.5, _safe("-  " + prefix + nm))
        pdf.ln(1)
    else:
        if final_only:
            widths = (120, 25, 35)
            heads = ("Produto", "Qtd", "Preço")
            aligns = ("L", "C", "R")
        else:
            widths = (95, 20, 32, 33)
            heads = ("Produto", "Qtd", "Preço unit.", "Subtotal")
            aligns = ("L", "C", "R", "R")
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_fill_color(*_HEAD_BG)
        pdf.set_text_color(*_DARK)
        pdf.set_draw_color(*_LINE)
        for w, h, a in zip(widths, heads, aligns):
            pdf.cell(w, 8, h, border="B", align=a, fill=True)
        pdf.ln(8)

        pdf.set_font("Helvetica", size=9.5)
        zebra = False
        for it in quote.get("items", []):
            qty = int(it.get("qty") or 1)
            cu = quotes.client_unit(it, quotes._num(quote.get("markup")) or 0.0)
            sub = round(cu * qty, 2) if cu is not None else None
            # nome que o CLIENTE ve: override do vendedor (dname) OU o nome original.
            # O fornecedor NAO aparece no PDF (vai direto pro cliente).
            name = _safe((it.get("dname") or "").strip() or it.get("name") or "(sem nome)")

            # measure wrapped product name to size the row height
            x0, y0 = pdf.get_x(), pdf.get_y()
            line_h = 5
            lines = pdf.multi_cell(widths[0], line_h, name, dry_run=True, output="LINES")
            n_name = max(1, len(lines))
            row_h = max(9, n_name * line_h + 2)

            if y0 + row_h > pdf.page_break_trigger:
                pdf.add_page()
                y0 = pdf.get_y()
                x0 = pdf.get_x()

            fill = _ZEBRA if zebra else (255, 255, 255)
            pdf.set_fill_color(*fill)
            pdf.rect(x0, y0, sum(widths), row_h, style="F")
            pdf.set_xy(x0, y0 + 1)
            pdf.set_text_color(*_DARK)
            pdf.multi_cell(widths[0], line_h, name, align="L")
            pdf.set_text_color(*_DARK)
            cy = y0 + (row_h - line_h) / 2
            pdf.set_xy(x0 + widths[0], cy)
            pdf.cell(widths[1], line_h, str(qty), align="C")
            if final_only:
                pdf.cell(widths[2], line_h, _brl(sub), align="R")   # so o preco final da linha
            else:
                pdf.cell(widths[2], line_h, _brl(cu), align="R")
                pdf.cell(widths[3], line_h, _brl(sub), align="R")
            pdf.set_draw_color(*_LINE)
            pdf.line(x0, y0 + row_h, x0 + sum(widths), y0 + row_h)
            pdf.set_xy(x0, y0 + row_h)
            zebra = not zebra

        # ---- total ------------------------------------------------------
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(*_DARK)
        pdf.cell(sum(widths[:-1]), 10, "TOTAL", align="R")
        pdf.set_text_color(*_ACCENT)
        pdf.cell(widths[-1], 10, _brl(gm["total"]), align="R", ln=1)

    # ---- notes -----------------------------------------------------------
    notes = (quote.get("notes") or "").strip()
    if notes:
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*_MUTED)
        pdf.cell(0, 5, _safe("Observações"), ln=1)
        pdf.set_font("Helvetica", size=9)
        pdf.set_text_color(*_DARK)
        pdf.multi_cell(0, 5, _safe(notes))

    out = pdf.output()
    return bytes(out)
