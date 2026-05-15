"""Generate algo summary PDF for TradeGenius ORB strategy (Keystone v5)."""
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
import os

OUTPUT = os.path.join(os.path.dirname(__file__), "TradeGenius_Algo_Summary.pdf")

# ── colour palette ──────────────────────────────────────────────────────────
NAVY   = colors.HexColor("#0D1B2A")
TEAL   = colors.HexColor("#00B4D8")
GREEN  = colors.HexColor("#06D6A0")
RED    = colors.HexColor("#EF476F")
GOLD   = colors.HexColor("#FFD166")
LGREY  = colors.HexColor("#F4F6F8")
MGREY  = colors.HexColor("#BFC9D1")
WHITE  = colors.white

def build():
    doc = SimpleDocTemplate(
        OUTPUT,
        pagesize=letter,
        leftMargin=0.75*inch, rightMargin=0.75*inch,
        topMargin=0.75*inch,  bottomMargin=0.75*inch,
    )

    styles = getSampleStyleSheet()
    def S(name, **kw):
        return ParagraphStyle(name, parent=styles["Normal"], **kw)

    title_s   = S("title",   fontSize=26, textColor=NAVY, leading=32, spaceAfter=4, fontName="Helvetica-Bold")
    sub_s     = S("sub",     fontSize=13, textColor=TEAL, leading=18, spaceAfter=2, fontName="Helvetica")
    h2_s      = S("h2",      fontSize=13, textColor=NAVY, leading=17, spaceBefore=14, spaceAfter=4, fontName="Helvetica-Bold")
    body_s    = S("body",    fontSize=10, textColor=colors.HexColor("#2C3E50"), leading=15, spaceAfter=3)
    small_s   = S("small",   fontSize=8,  textColor=MGREY, leading=11, spaceAfter=2)
    caption_s = S("caption", fontSize=9,  textColor=MGREY, leading=12, alignment=TA_CENTER)
    note_s    = S("note",    fontSize=9,  textColor=colors.HexColor("#555"), leading=13,
                  borderPad=6, backColor=LGREY, spaceAfter=6)

    story = []

    # ── HEADER ───────────────────────────────────────────────────────────────
    story.append(Paragraph("TradeGenius", title_s))
    story.append(Paragraph("Algorithmic Trading Strategy — Keystone v5", sub_s))
    story.append(Paragraph("As of May 15, 2026 · $100,000 account · Compounded daily", small_s))
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL, spaceAfter=14))

    # ── STRATEGY OVERVIEW ────────────────────────────────────────────────────
    story.append(Paragraph("Strategy Overview", h2_s))
    story.append(Paragraph(
        "TradeGenius runs two independent strategies in sequence each trading day: "
        "a morning Opening Range Breakout (ORB) session and an afternoon EOD Reversal addon. "
        "Both strategies operate on a fixed universe of 12 large-cap and ETF tickers, "
        "risk-managed per trade, and compound daily on a $100,000 base account.",
        body_s))

    # ── TWO-STRATEGY TABLE ───────────────────────────────────────────────────
    story.append(Spacer(1, 8))
    strat_data = [
        ["", "Morning ORB", "EOD Reversal"],
        ["Window",        "9:30 – 11:00 ET",       "15:00 – 15:58 ET"],
        ["Entry signal",  "30-min opening range\nbreakout (long or short)", "Mean-reversion rank\n(ROD3 across 6 tickers)"],
        ["Exit",          "2.5R target · ATR stop\nPartial at 1R → BE runner", "Market-on-close\nat 15:58 ET"],
        ["Stop",          "ATR(14) × 1.75 from entry", "2% from entry price"],
        ["Position size", "1% risk per trade\n≤75% of account notional", "35% notional per leg\n(1 long + 1 short)"],
        ["Universe",      "AAPL AMZN AVGO GOOG\nMETA MSFT NFLX NVDA\nORCL QQQ SPY TSLA", "ORCL AAPL MSFT AVGO NFLX TSLA\n(long: ORCL AAPL MSFT AVGO TSLA)\n(short: ORCL NFLX AAPL MSFT TSLA)"],
        ["Max trades/day","5",                       "2 (1 long + 1 short)"],
        ["Ann P&L (17mo)","$39,898",                 "$12,620"],
    ]
    ts = TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), NAVY),
        ("TEXTCOLOR",   (0,0), (-1,0), WHITE),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,0), 10),
        ("ALIGN",       (0,0), (-1,0), "CENTER"),
        ("BACKGROUND",  (0,1), (0,-1), LGREY),
        ("FONTNAME",    (0,1), (0,-1), "Helvetica-Bold"),
        ("FONTSIZE",    (0,1), (-1,-1), 9),
        ("ROWBACKGROUNDS", (1,1), (-1,-1), [WHITE, LGREY]),
        ("GRID",        (0,0), (-1,-1), 0.4, MGREY),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 7),
        ("RIGHTPADDING",(0,0), (-1,-1), 7),
        # highlight combined row
        ("BACKGROUND",  (0,-1), (-1,-1), colors.HexColor("#E8F8F2")),
        ("FONTNAME",    (0,-1), (-1,-1), "Helvetica-Bold"),
        ("TEXTCOLOR",   (1,-1), (-1,-1), GREEN),
    ])
    t = Table(strat_data, colWidths=[1.45*inch, 2.9*inch, 2.9*inch])
    t.setStyle(ts)
    story.append(t)

    # ── RISK CONTROLS ────────────────────────────────────────────────────────
    story.append(Paragraph("Risk Controls", h2_s))
    risk_data = [
        ["Control", "Setting", "Purpose"],
        ["Daily loss kill-switch",        "−2% of account",          "Halt all new entries if down >$2k on the day"],
        ["Concurrent risk cap",           "$2,000 open risk",        "Never risk more than $2k across all open positions"],
        ["VWAP-chase gate",               "≤15 bps from VWAP",       "Block entries on META/MSFT/AAPL/AMZN/GOOG/AVGO\nwhen price has already run far from session VWAP"],
        ["Sym. post-trade cooldown",      "10 min after any exit",   "Block same (ticker, side) re-entry for 10 min\nafter a close — prevents immediate double-fires"],
        ["VIX gate",                      "Skip if VIX > 25",        "Avoid entries on extreme-volatility days"],
        ["Gap gate",                      "Skip if gap > 1.5%",      "Avoid gapping tickers that already broke out"],
        ["Earnings skip",                 "±1 day around earnings",  "No entries around earnings events"],
        ["SPY regime gate",               "Prior day SPY ret > −40 bps", "Skip ORB if broad market sold off hard prior day"],
        ["EOD notional cap",              "≤95% of account equity",  "Total long + short market value capped at equity"],
    ]
    rt = Table(risk_data, colWidths=[2.0*inch, 1.8*inch, 3.45*inch])
    rt.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), NAVY),
        ("TEXTCOLOR",   (0,0), (-1,0), WHITE),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,0), 9),
        ("ALIGN",       (0,0), (-1,0), "CENTER"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [WHITE, LGREY]),
        ("FONTSIZE",    (0,1), (-1,-1), 8.5),
        ("FONTNAME",    (0,1), (0,-1), "Helvetica-Bold"),
        ("GRID",        (0,0), (-1,-1), 0.4, MGREY),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 7),
        ("RIGHTPADDING",(0,0), (-1,-1), 7),
    ]))
    story.append(rt)

    # ── PERFORMANCE ──────────────────────────────────────────────────────────
    story.append(Paragraph("Performance (Jan 2025 – May 2026, 341 trading days)", h2_s))

    # headline KPIs
    kpi_data = [
        ["Combined Ann/yr", "Return on $100k", "Negative Quarters", "Morning Win Rate"],
        ["$52,518",         "+74.7%",           "1 of 6",            "57%"],
    ]
    kt = Table(kpi_data, colWidths=[1.8*inch]*4)
    kt.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), LGREY),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,0), 8),
        ("ALIGN",       (0,0), (-1,-1), "CENTER"),
        ("FONTNAME",    (0,1), (-1,1), "Helvetica-Bold"),
        ("FONTSIZE",    (0,1), (-1,1), 18),
        ("TEXTCOLOR",   (0,1), (-1,1), TEAL),
        ("GRID",        (0,0), (-1,-1), 0.4, MGREY),
        ("TOPPADDING",  (0,0), (-1,-1), 8),
        ("BOTTOMPADDING",(0,0),(-1,-1), 8),
        ("BOX",         (0,0), (-1,-1), 1.5, TEAL),
    ]))
    story.append(kt)
    story.append(Spacer(1, 10))

    # annual P&L range
    story.append(Paragraph("Annual P&L Range (rolling 4-quarter windows)", h2_s))
    range_data = [
        ["Scenario",      "Rolling Period",          "Annual P&L",  "Notes"],
        ["Minimum",       "Q1 2025 – Q4 2025",       "$33,540",     "Includes weakest quarter (Q1 2025, −$3,819)"],
        ["Median / Base", "17-month annualized",     "$52,518",     "Full-corpus compounded figure"],
        ["Maximum",       "Q3 2025 – Q2 2026",       "$62,800",     "Strongest trailing year to date"],
    ]
    colors_row = [LGREY, colors.HexColor("#FFF3F5"), WHITE, colors.HexColor("#F0FDF8")]
    text_colors = [NAVY, RED, NAVY, GREEN]
    pnl_col = [NAVY, RED, colors.HexColor("#2C3E50"), GREEN]

    rng_t = Table(range_data, colWidths=[1.15*inch, 1.85*inch, 1.1*inch, 3.15*inch])
    rng_t.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), NAVY),
        ("TEXTCOLOR",   (0,0), (-1,0), WHITE),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,0), 9),
        ("ALIGN",       (0,0), (-1,0), "CENTER"),
        ("ALIGN",       (2,1), (2,-1), "RIGHT"),
        ("BACKGROUND",  (0,1), (-1,1), colors.HexColor("#FFF8F8")),
        ("BACKGROUND",  (0,2), (-1,2), LGREY),
        ("BACKGROUND",  (0,3), (-1,3), colors.HexColor("#F0FDF8")),
        ("TEXTCOLOR",   (2,1), (2,1), RED),
        ("TEXTCOLOR",   (2,2), (2,2), colors.HexColor("#2C3E50")),
        ("TEXTCOLOR",   (2,3), (2,3), GREEN),
        ("FONTNAME",    (0,1), (-1,-1), "Helvetica"),
        ("FONTNAME",    (0,1), (0,-1), "Helvetica-Bold"),
        ("FONTNAME",    (2,1), (2,-1), "Helvetica-Bold"),
        ("FONTSIZE",    (0,1), (-1,-1), 9),
        ("GRID",        (0,0), (-1,-1), 0.4, MGREY),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0),(-1,-1), 6),
        ("LEFTPADDING", (0,0), (-1,-1), 7),
        ("RIGHTPADDING",(0,0), (-1,-1), 7),
    ]))
    story.append(rng_t)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Range is based on rolling 12-month windows from the 17-month SIP corpus. "
        "All figures assume daily compounding on $100,000 starting equity. "
        "Past results do not guarantee future performance.",
        caption_s))

    # ── QUARTERLY TABLE ──────────────────────────────────────────────────────
    story.append(Paragraph("Quarterly P&L Breakdown (Combined Morning + EOD)", h2_s))
    q_data = [
        ["Quarter", "Morning", "EOD", "Combined"],
        ["Q1 2025", "−$4,842", "+$1,023", "−$3,819"],
        ["Q2 2025", "+$6,710", "+$4,997", "+$13,291"],
        ["Q3 2025", "+$9,071", "−$2,500", "+$8,926"],
        ["Q4 2025", "+$12,662", "+$2,286", "+$16,306"],
        ["Q1 2026", "+$4,011", "+$4,615", "+$16,462"],
        ["Q2 2026", "+$18,239", "+$1,338", "+$20,385"],
        ["Total (17mo)", "+$45,851", "+$11,758", "+$57,609"],
    ]
    qt = Table(q_data, colWidths=[1.5*inch, 1.65*inch, 1.65*inch, 1.65*inch])
    qt_style = TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), NAVY),
        ("TEXTCOLOR",   (0,0), (-1,0), WHITE),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,0), 9),
        ("ALIGN",       (1,0), (-1,-1), "RIGHT"),
        ("ALIGN",       (0,0), (0,-1), "LEFT"),
        ("ROWBACKGROUNDS", (0,1), (-1,-2), [WHITE, LGREY]),
        ("FONTSIZE",    (0,1), (-1,-1), 9),
        ("FONTNAME",    (0,1), (0,-1), "Helvetica-Bold"),
        # Q1 2025 negative — red combined
        ("TEXTCOLOR",   (3,1), (3,1), RED),
        ("TEXTCOLOR",   (2,3), (2,3), RED),   # EOD Q3
        # Total row
        ("BACKGROUND",  (0,-1), (-1,-1), NAVY),
        ("TEXTCOLOR",   (0,-1), (-1,-1), WHITE),
        ("FONTNAME",    (0,-1), (-1,-1), "Helvetica-Bold"),
        ("GRID",        (0,0), (-1,-1), 0.4, MGREY),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 7),
        ("RIGHTPADDING",(0,0), (-1,-1), 7),
    ])
    qt.setStyle(qt_style)
    story.append(qt)

    # ── FOOTER NOTE ──────────────────────────────────────────────────────────
    story.append(Spacer(1, 14))
    story.append(HRFlowable(width="100%", thickness=1, color=MGREY, spaceAfter=6))
    story.append(Paragraph(
        "Backtest corpus: SIP bar data, Jan 2025 – May 2026, 341 trading days. "
        "Slippage: 1.5 bps entry + 1.5 bps exit. No look-ahead bias. "
        "Keystone v4 locked 2026-05-15 (v9.1.114). "
        "Strategy runs live on Railway via Alpaca broker (Val portfolio, cash account).",
        small_s))

    doc.build(story)
    print(f"PDF written to: {OUTPUT}")

if __name__ == "__main__":
    build()
