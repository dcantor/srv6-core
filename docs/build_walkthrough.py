#!/usr/bin/env python3
"""Build docs/srv6-walkthrough.{html,pdf} from docs/srv6-walkthrough.md: every `{{name}}` is replaced by the captured
output docs/walkthrough/<name>.txt (captured from the live lab by walkthrough_capture.py), Markdown is rendered with
markdown-it, and Chrome prints the PDF. Run with the cat8000v-ipsec webapp venv (playwright); --html-only skips the PDF."""
import re, sys
from pathlib import Path
from markdown_it import MarkdownIt

D = Path(__file__).resolve().parent; SRC = D / "srv6-walkthrough.md"; CAP = D / "walkthrough"
md = SRC.read_text()
missing = [m for m in re.findall(r"\{\{([\w-]+)\}\}", md) if not (CAP / f"{m}.txt").exists()]
if missing: sys.exit(f"missing captures: {missing} (run walkthrough_capture.py against the live lab)")
md = re.sub(r"\{\{([\w-]+)\}\}", lambda m: (CAP / f"{m[1]}.txt").read_text().rstrip(), md)
(D / "srv6-walkthrough.expanded.md").write_text(md)
body = MarkdownIt("commonmark", {"html": True}).enable("table").render(md)
CSS = """
body { font: 11pt/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1a1a1a; max-width: 900px; margin: 30px auto; padding: 0 20px }
h1 { font-size: 26pt; margin-bottom: 4px } h2 { font-size: 16pt; margin-top: 34px; border-bottom: 2px solid #1f5fa8; padding-bottom: 3px; color: #1f5fa8 }
h3 { font-size: 12.5pt; margin-top: 22px } p > em:first-child { color: #555 }
pre { background: #f4f6f8; border: 1px solid #dfe3e8; border-radius: 4px; padding: 8px 10px; font: 7.6pt/1.35 "JetBrains Mono", Menlo, Consolas, monospace; overflow-x: auto; white-space: pre-wrap; word-break: break-all; page-break-inside: avoid }
code { font: 9.5pt "JetBrains Mono", Menlo, Consolas, monospace; background: #f4f6f8; padding: 0 3px; border-radius: 3px } pre code { background: none; padding: 0; font-size: inherit }
table { border-collapse: collapse; font-size: 9.5pt; margin: 10px 0 } th, td { border: 1px solid #d0d5db; padding: 4px 8px; text-align: left; vertical-align: top } th { background: #eef2f6 }
img { max-width: 100%; border: 1px solid #dfe3e8 } hr { border: 0; border-top: 1px solid #dfe3e8; margin: 28px 0 }
ol li, ul li { margin: 3px 0 } @page { size: A4; margin: 16mm 14mm }
@media print { h2 { page-break-after: avoid } }
"""
html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>SRv6 L3VPN, shown on real boxes</title><style>{CSS}</style></head><body>{body}</body></html>"
(D / "srv6-walkthrough.html").write_text(html); print("wrote docs/srv6-walkthrough.html")
if "--html-only" not in sys.argv:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True); pg = b.new_page()
        pg.goto((D / "srv6-walkthrough.html").as_uri()); pg.wait_for_load_state("networkidle")
        pg.pdf(path=str(D / "srv6-walkthrough.pdf"), format="A4", print_background=True, margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"}); b.close()
    print(f"wrote docs/srv6-walkthrough.pdf ({(D / 'srv6-walkthrough.pdf').stat().st_size // 1024} KB)")
