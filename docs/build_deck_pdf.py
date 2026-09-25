#!/usr/bin/env python3
"""Print docs/srv6-workflows-preview.html to docs/srv6-workflows.pdf, one slide per landscape page.

A read-only companion for anyone without PowerPoint — and the only way to look at the deck on this host, which has no
LibreOffice to convert the .pptx itself.  ~/cat8000v-ipsec/webapp/.venv/bin/python docs/build_deck_pdf.py"""
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SRC = HERE / "srv6-workflows-preview.html"
OUT = HERE / "srv6-workflows.pdf"
PAGE = "@page { size: 13.333in 7.5in; margin: 0 } .s { margin: 0 !important; page-break-after: always } body { background: #fff }"

with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 720})
    pg.goto(SRC.as_uri(), wait_until="load"); pg.wait_for_timeout(2500)
    pg.add_style_tag(content=PAGE)
    pg.emulate_media(media="print")
    pg.pdf(path=str(OUT), width="13.333in", height="7.5in", print_background=True,
           margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
    n = pg.evaluate("document.querySelectorAll('.s').length")
    b.close()
print(f"{OUT.name}: {n} slides, {OUT.stat().st_size / 1e6:.1f} MB")
