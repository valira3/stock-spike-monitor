"""Render ARCHITECTURE.md to trade_genius_algo.pdf.

Usage:
    pip install weasyprint markdown
    python scripts/build_algo_pdf.py

Output: trade_genius_algo.pdf at the repo root (overwritten in place).

Designed to track BOT_VERSION automatically: the cover page reads the
constant from trade_genius.py at run time, so a future doc refresh only
needs to re-run this script after editing ARCHITECTURE.md.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import markdown
from weasyprint import HTML, CSS

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCH_MD = REPO_ROOT / "ARCHITECTURE.md"
OUT_PDF = REPO_ROOT / "trade_genius_algo.pdf"
TG_PY = REPO_ROOT / "trade_genius.py"


def read_bot_version() -> str:
    src = TG_PY.read_text(encoding="utf-8")
    m = re.search(r'^BOT_VERSION\s*=\s*"([^"]+)"', src, flags=re.M)
    if not m:
        raise SystemExit("Could not find BOT_VERSION in trade_genius.py")
    return m.group(1)


def build_toc(md_text: str) -> list[tuple[int, str, str]]:
    """Return a list of (level, title, anchor) for every ## or ### heading."""
    out: list[tuple[int, str, str]] = []
    for line in md_text.splitlines():
        m = re.match(r"^(#{2,3})\s+(.*?)\s*$", line)
        if not m:
            continue
        level = len(m.group(1))
        title = m.group(2).strip()
        anchor = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        out.append((level, title, anchor))
    return out


def render_html(md_text: str, version: str, toc: list[tuple[int, str, str]]) -> str:
    body_html = markdown.markdown(
        md_text,
        extensions=[
            "fenced_code",
            "tables",
            "toc",
            "sane_lists",
        ],
        extension_configs={
            "toc": {"toc_depth": "2-3", "permalink": False},
        },
    )

    today = date.today().strftime("%B %Y")
    toc_html = "\n".join(
        f'<div class="toc-l{lvl}"><a href="#{anc}">{title}</a></div>'
        for lvl, title, anc in toc
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TradeGenius — System Architecture</title>
</head>
<body>

<section class="cover">
  <div class="cover-mark">TradeGenius</div>
  <h1 class="cover-title">System Architecture</h1>
  <div class="cover-meta">
    <div>v{version}</div>
    <div>{today}</div>
  </div>
  <div class="cover-foot">
    Repo: valira3/stock-spike-monitor<br>
    Service: tradegenius.up.railway.app
  </div>
</section>

<section class="toc">
  <h1>Contents</h1>
  {toc_html}
</section>

<section class="content">
{body_html}
</section>

</body>
</html>
"""


PRINT_CSS = """
@page {
    size: Letter;
    margin: 22mm 18mm 18mm 18mm;
    @bottom-center {
        content: "TradeGenius — System Architecture · v" string(version);
        font-family: 'Helvetica', sans-serif;
        font-size: 8pt;
        color: #6b7280;
    }
    @bottom-right {
        content: counter(page) " / " counter(pages);
        font-family: 'Helvetica', sans-serif;
        font-size: 8pt;
        color: #6b7280;
    }
}
@page :first {
    margin: 0;
    @bottom-center { content: none; }
    @bottom-right  { content: none; }
}
@page toc-page {
    @bottom-center { content: "Contents"; }
}

html { string-set: version "VERSION_PLACEHOLDER"; }
body {
    font-family: 'Helvetica', 'Arial', sans-serif;
    font-size: 10pt;
    line-height: 1.45;
    color: #111827;
}

/* Cover page */
.cover {
    page: cover;
    page-break-after: always;
    height: 100vh;
    padding: 38mm 22mm 22mm 22mm;
    background: linear-gradient(180deg, #0a0d12 0%, #10151c 60%, #161d27 100%);
    color: #e7ecf3;
    display: block;
    box-sizing: border-box;
}
.cover-mark {
    font-size: 13pt;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: #7dd3fc;
    margin-bottom: 14mm;
}
.cover-title {
    font-size: 36pt;
    font-weight: 700;
    line-height: 1.05;
    margin: 0 0 12mm 0;
    color: #e7ecf3;
    border: none;
}
.cover-meta {
    font-size: 13pt;
    color: #8a96a7;
    margin-top: 28mm;
}
.cover-meta div { margin-bottom: 3mm; }
.cover-foot {
    position: absolute;
    bottom: 22mm;
    left: 22mm;
    font-size: 9pt;
    color: #5b6572;
    letter-spacing: 0.04em;
}

/* TOC page */
.toc { page-break-after: always; }
.toc h1 {
    font-size: 22pt;
    margin: 0 0 8mm 0;
    border-bottom: 2px solid #111827;
    padding-bottom: 3mm;
}
.toc-l2 {
    font-size: 11pt;
    margin: 2.4mm 0;
    font-weight: 600;
}
.toc-l3 {
    font-size: 9.5pt;
    margin: 1.2mm 0 1.2mm 7mm;
    color: #4b5563;
}
.toc a { color: #111827; text-decoration: none; }

/* Content body */
.content h1 {
    font-size: 20pt;
    margin: 8mm 0 4mm 0;
    padding-bottom: 2mm;
    border-bottom: 2px solid #111827;
    page-break-before: always;
}
.content h1:first-of-type { page-break-before: auto; }
.content h2 {
    font-size: 14pt;
    margin: 7mm 0 3mm 0;
    color: #111827;
    border-bottom: 1px solid #d1d5db;
    padding-bottom: 1.4mm;
    page-break-after: avoid;
}
.content h3 {
    font-size: 11.5pt;
    margin: 5mm 0 2mm 0;
    color: #1f2937;
    page-break-after: avoid;
}
.content h4 {
    font-size: 10.5pt;
    margin: 4mm 0 1.5mm 0;
    color: #374151;
    page-break-after: avoid;
}
.content p { margin: 0 0 3mm 0; }
.content ul, .content ol { margin: 0 0 3mm 5mm; padding: 0; }
.content li { margin-bottom: 1mm; }

.content a { color: #1d4ed8; text-decoration: none; }

/* Code */
.content code {
    font-family: 'Menlo', 'Consolas', monospace;
    font-size: 8.8pt;
    background: #f3f4f6;
    padding: 0.5mm 1.2mm;
    border-radius: 1mm;
    color: #111827;
}
.content pre {
    background: #0f172a;
    color: #e2e8f0;
    padding: 3mm 4mm;
    border-radius: 1.5mm;
    font-size: 8.4pt;
    line-height: 1.4;
    overflow-x: hidden;
    page-break-inside: avoid;
}
.content pre code {
    background: transparent;
    color: inherit;
    padding: 0;
    font-size: inherit;
}

/* Tables */
.content table {
    width: 100%;
    border-collapse: collapse;
    margin: 3mm 0 4mm 0;
    font-size: 9pt;
    page-break-inside: avoid;
}
.content th, .content td {
    border: 1px solid #d1d5db;
    padding: 1.4mm 2.2mm;
    text-align: left;
    vertical-align: top;
}
.content th {
    background: #f3f4f6;
    font-weight: 600;
    color: #111827;
}

/* Blockquote */
.content blockquote {
    border-left: 3px solid #7dd3fc;
    background: #f0f9ff;
    margin: 3mm 0;
    padding: 2mm 4mm;
    color: #0c4a6e;
    font-size: 9.5pt;
}

/* Horizontal rule */
.content hr {
    border: none;
    border-top: 1px solid #e5e7eb;
    margin: 6mm 0;
}

/* Avoid orphans on key blocks */
.content table, .content pre, .content blockquote {
    page-break-inside: avoid;
}
"""


def main() -> None:
    if not ARCH_MD.exists():
        raise SystemExit(f"missing {ARCH_MD}")
    md_text = ARCH_MD.read_text(encoding="utf-8")
    version = read_bot_version()
    toc = build_toc(md_text)

    html_text = render_html(md_text, version, toc)
    css_text = PRINT_CSS.replace("VERSION_PLACEHOLDER", version)

    HTML(string=html_text, base_url=str(REPO_ROOT)).write_pdf(
        target=str(OUT_PDF),
        stylesheets=[CSS(string=css_text)],
    )
    size_kb = OUT_PDF.stat().st_size / 1024
    print(f"wrote {OUT_PDF} ({size_kb:.1f} KB) — version v{version}")


if __name__ == "__main__":
    main()
