#!/usr/bin/env python3
"""Build the lab's guides: docs/<doc>.{html,pdf} from docs/<doc>.md, where <doc> is srv6-lab-guide (the primer, lab tour
and walkthrough; the default) or srv6-in-depth (the overview and the deep dive).

`{{name}}` is replaced by the captured output docs/walkthrough/<name>.txt (`{{name|N}}` keeps its first N lines, with a
note), `{{meta}}` by the version and date. Captures come from the live lab (docs/walkthrough_capture.py), screenshots from
docs/guide_screenshots.py. Markdown is rendered with markdown-it, the contents page is generated from the ## headings, and
Chrome prints the PDF with page numbers. Run with the cat8000v-ipsec webapp venv (playwright); --html-only skips the PDF.

    python3 docs/walkthrough_capture.py                                  # refresh the outputs (the lab must be up)
    ~/cat8000v-ipsec/webapp/.venv/bin/python docs/guide_screenshots.py    # refresh the screenshots
    ~/cat8000v-ipsec/webapp/.venv/bin/python docs/build_lab_guide.py [srv6-in-depth]
    python3 docs/indepth_capture.py                                      # the extra outputs srv6-in-depth quotes"""
import datetime, html, re, sys
from pathlib import Path
from markdown_it import MarkdownIt

DOCS = {"srv6-lab-guide": ("SRv6 Lab Guide — SRv6 from first principles, a tour of the lab, and a walkthrough", "SRv6 Lab Guide"),
        "srv6-in-depth": ("SRv6 In Depth — the overview, then the details", "SRv6 In Depth")}
DOC = next((a for a in sys.argv[1:] if not a.startswith("--")), "srv6-lab-guide")
if DOC not in DOCS: sys.exit(f"unknown document {DOC!r}: one of {', '.join(DOCS)}")
D = Path(__file__).resolve().parent; SRC = D / f"{DOC}.md"; CAP = D / "walkthrough"
VERSION = (D.parent / "VERSION").read_text().strip()
md = SRC.read_text()


def capture(m):
    name, limit = m[1], m[2]
    f = CAP / f"{name}.txt"
    if not f.exists():
        sys.exit(f"missing capture: {name} (run docs/walkthrough_capture.py against the live lab)")
    lines = f.read_text().rstrip().splitlines()
    if limit and len(lines) > int(limit):
        lines = lines[:int(limit)] + [f"… ({len(lines) - int(limit)} more lines)"]
    return "\n".join(lines)


md = md.replace("{{meta}}", f"Lab version {VERSION} · {datetime.date.today():%-d %B %Y} · outputs captured from the running lab")
md = re.sub(r"\{\{([\w-]+)(?:\|(\d+))?\}\}", capture, md)
body = MarkdownIt("commonmark", {"html": True}).enable("table").render(md)

# numbered ## headings → ids and the contents page; a part banner opens a group in the contents
toc, n = [], 0
def heading(m):
    global n
    if "notoc" in m[1]:
        return m[0]
    n += 1; hid = f"s{n}"; toc.append(("h", hid, re.sub("<[^>]+>", "", m[2])))
    return f'<h2 id="{hid}"{m[1]}>{m[2]}</h2>'
body = re.sub(r'<div class="part-banner"><span>(.*?)</span>(.*?)</div>', lambda m: (toc.append(("p", m[1], m[2])) or m[0]), body)
body = re.sub(r"<h2([^>]*)>(.*?)</h2>", heading, body)
# the part banners were collected before the headings: put them back in document order
order = [(body.find(f'<span>{t[1]}</span>{t[2]}') if t[0] == "p" else body.find(f'id="{t[1]}"'), t) for t in toc]
toc_html = "".join(f'<div class="toc-part">{html.escape(t[1])} — {html.escape(t[2])}</div>' if t[0] == "p"
                   else f'<a class="toc-row" href="#{t[1]}"><span>{html.escape(t[2])}</span><i></i></a>'
                   for _, t in sorted(order))
body = body.replace('<div id="toc"></div>', f'<div id="toc">{toc_html}</div>')
body = re.sub(r"<p><img ([^>]*)alt=\"([^\"]*)\"([^>]*)></p>", r'<figure class="shot"><img \1alt="\2"\3><figcaption>\2</figcaption></figure>', body)

CSS = """
:root { --ink:#0f172a; --muted:#475569; --line:#dbe3ec; --accent:#c2410c; --blue:#1d4ed8; --soft:#f5f7fa }
* { box-sizing:border-box }
body { font:10.5pt/1.55 "Inter", -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color:var(--ink); margin:0 }
h2 { font-size:17pt; line-height:1.25; margin:26px 0 10px; color:var(--ink); page-break-after:avoid; break-after:avoid }
h3 { font-size:12pt; margin:18px 0 6px; color:var(--blue); page-break-after:avoid }
p { margin:6px 0 9px } a { color:var(--blue); text-decoration:none }
pre { background:#0f172a; color:#e2e8f0; border-radius:6px; padding:9px 11px; font:7.3pt/1.42 "JetBrains Mono", Menlo, Consolas, monospace;
      white-space:pre-wrap; word-break:break-all; page-break-inside:avoid; break-inside:avoid; margin:8px 0 12px }
code { font:9pt "JetBrains Mono", Menlo, Consolas, monospace; background:#eef2f7; padding:0 3px; border-radius:3px } pre code { background:none; padding:0; font-size:inherit; color:inherit }
table { border-collapse:collapse; width:100%; font-size:9pt; margin:8px 0 14px; page-break-inside:avoid; break-inside:avoid }
th, td { border-bottom:1px solid var(--line); padding:5px 7px; text-align:left; vertical-align:top } th { background:var(--soft); font-weight:600; color:var(--muted); font-size:8.5pt }
ul, ol { margin:6px 0 10px; padding-left:22px } li { margin:3px 0 }
figure { margin:12px 0 16px; page-break-inside:avoid; break-inside:avoid } figure svg { width:100%; height:auto; display:block }
figcaption { font-size:8.5pt; color:var(--muted); margin-top:5px; font-style:italic }
figure.shot img { max-width:100%; max-height:125mm; width:auto; margin:0 auto; border:1px solid var(--line); border-radius:6px; display:block }
.callout { border-radius:8px; padding:10px 13px; margin:12px 0; font-size:9.5pt; page-break-inside:avoid; break-inside:avoid }
.summary { background:#f8fafc; border:1px solid var(--line); border-radius:10px; padding:12px 18px 8px; font-size:10.5pt } .summary ul { margin-top:4px }
.callout.note { background:#eff6ff; border-left:4px solid var(--blue) } .callout.tip { background:#fff7ed; border-left:4px solid var(--accent) }
.cover { height:254mm; display:flex; flex-direction:column; justify-content:center; page-break-after:always; border-radius:14px;
         background:linear-gradient(160deg, #0b1020 0%, #1e293b 62%, #7c2d12 140%); color:#f8fafc; margin:0; padding:0 16mm }
.cover .kicker { text-transform:uppercase; letter-spacing:.18em; font-size:9pt; color:#fdba74; font-weight:600 }
.cover .title { font-size:34pt; line-height:1.1; margin:14px 0 14px; font-weight:700; color:#fff }
.cover .subtitle { font-size:13pt; color:#cbd5e1; max-width:150mm; line-height:1.45 }
.cover .parts { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:34px 0 26px }
.cover .parts.two { grid-template-columns:repeat(2,1fr) }
.cover .parts div { background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.14); border-radius:10px; padding:12px 13px }
.cover .parts b { display:block; color:#fdba74; font-size:9pt; text-transform:uppercase; letter-spacing:.1em }
.cover .parts span { display:block; font-size:12pt; font-weight:600; margin:3px 0 5px; color:#fff } .cover .parts small { color:#cbd5e1; font-size:8.5pt; line-height:1.4 }
.cover .meta { color:#94a3b8; font-size:9pt }
.toc-page { page-break-after:always } .toc-page h2 { margin-top:0 }
.toc-part { font-weight:700; color:var(--accent); margin:14px 0 4px; font-size:10pt; text-transform:uppercase; letter-spacing:.06em }
.toc-row { display:flex; color:var(--ink); padding:3px 0; font-size:10.5pt } .toc-row i { flex:1; border-bottom:1px dotted #cbd5e1; margin:0 0 5px 8px }
.toc-row b { font-weight:400; color:var(--muted); min-width:18px; text-align:right }
.part-banner { page-break-before:always; break-before:page; background:linear-gradient(120deg,#0b1020,#1e293b); color:#fff; border-radius:10px; padding:20px 22px;
               font-size:22pt; font-weight:700; margin:0 0 6px }
.part-banner span { display:block; font-size:9pt; letter-spacing:.18em; text-transform:uppercase; color:#fdba74; margin-bottom:2px }
@page { size:A4; margin:16mm 14mm 18mm }
"""
title, doc_label = DOCS[DOC]
page = f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{title}</title><style>{CSS}</style></head><body>{body}</body></html>"
(D / f"{DOC}.html").write_text(page); print(f"wrote docs/{DOC}.html ({len(toc)} contents entries)")
if "--html-only" not in sys.argv:
    from playwright.sync_api import sync_playwright
    foot = ('<div style="width:100%;font:7.5pt Helvetica,Arial,sans-serif;color:#94a3b8;padding:0 14mm;display:flex;justify-content:space-between">'
            f'<span>{doc_label} · srv6-core {VERSION}</span><span class="pageNumber"></span></div>')
    import subprocess

    def render():
        with sync_playwright() as pw:
            b = pw.chromium.launch(channel="chrome", headless=True); pg = b.new_page()
            pg.goto((D / f"{DOC}.html").as_uri()); pg.wait_for_load_state("networkidle")
            pg.pdf(path=str(D / f"{DOC}.pdf"), format="A4", print_background=True, display_header_footer=True,
                   header_template="<span></span>", footer_template=foot, tagged=True, outline=True,
                   margin={"top": "16mm", "bottom": "18mm", "left": "14mm", "right": "14mm"}); b.close()
    render()
    # pass 2: find the page each numbered heading landed on, write it into the contents, print again (the contents is
    # one page either way, so the numbers do not move)
    text = subprocess.run(["pdftotext", "-layout", str(D / f"{DOC}.pdf"), "-"], capture_output=True, text=True).stdout.split("\f")
    norm = lambda t: re.sub(r"\s+", " ", html.unescape(t)).strip()
    pages = {}
    for kind, hid, label in toc:
        if kind == "h":
            pages[hid] = next((i + 1 for i, t in enumerate(text) if i >= 2 and norm(label) in norm(t)), None)
    for hid, pno in pages.items():
        page = page.replace(f'href="#{hid}"><span>', f'href="#{hid}" data-p="{pno}"><span>', 1)
    page = re.sub(r'data-p="(\d+)"><span>(.*?)</span><i></i>', r'><span>\2</span><i></i><b>\1</b>', page)
    (D / f"{DOC}.html").write_text(page); render()
    missing = [h for h, p in pages.items() if not p]
    if missing: print(f"  no page found for {missing}")
    print(f"wrote docs/{DOC}.pdf ({(D / f'{DOC}.pdf').stat().st_size // 1024} KB)")
