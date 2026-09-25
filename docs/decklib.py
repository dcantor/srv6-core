#!/usr/bin/env python3
"""The layout description the decks in docs/ are written against, and the two renderers it drives.

A deck is a list of slides, each a list of placed items — a box, a circle, a screenshot, a run of paragraphs — in inches
on a 13.333 x 7.5 canvas. `save_pptx` writes the PowerPoint; `save_preview` writes an HTML replica at the same
coordinates, because this host has no LibreOffice and a deck nobody has looked at is a deck nobody should send.

Used by build_deck.py (the portal and the looking glass together) and build_lg_deck.py (the looking glass in detail)."""
import html as htmlmod
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

W, H = 13.333, 7.5                      # LAYOUT_WIDE

# The lab's own colours: the ink of both headers, the teal the core is drawn in, and the green of the collector's
# sessions — the two dashed lines on every map in these decks.
INK, DEEP, TEAL = "07161C", "10303A", "0E7490"
GREEN, AMBER = "15803D", "B45309"
GROUND, CARD, LINE = "F6F8F9", "FFFFFF", "DCE3E7"
BODY, MUTED, PALE = "1F2937", "64748B", "BFD3DA"
HEAD_FONT, BODY_FONT = "Cambria", "Calibri"


def para(item, txt, size=None, color=None, bold=False, italic=False, space_after=6):
    """A paragraph inside a text item; size and colour default to the item's."""
    item.setdefault("paras", []).append({"txt": txt, "size": size or item["size"], "color": color or item["color"],
                                         "bold": bold, "italic": italic, "space_after": space_after})


def hexc(c): return RGBColor.from_string(c)


class Deck:
    """One deck. Every helper takes the slide it places into and returns what the caller needs to place the next thing."""

    def __init__(self, shots):
        self.slides, self.shots = [], Path(shots)

    # ---- primitives ------------------------------------------------------------------------------------------
    def slide(self, bg=GROUND):
        self.slides.append({"bg": bg, "items": []}); return self.slides[-1]

    def text(self, s, x, y, w, h, size=16, color=BODY, bold=False, font=BODY_FONT, align="l", anchor="t",
             spacing=1.15, italic=False):
        s["items"].append({"k": "text", "x": x, "y": y, "w": w, "h": h, "size": size, "color": color, "bold": bold,
                           "font": font, "align": align, "anchor": anchor, "spacing": spacing, "italic": italic})
        return s["items"][-1]

    def box(self, s, x, y, w, h, fill=CARD, line=LINE, radius=0.06, width=1):
        s["items"].append({"k": "box", "x": x, "y": y, "w": w, "h": h, "fill": fill, "line": line,
                           "radius": radius, "lw": width})

    def circle(self, s, x, y, d, fill=TEAL):
        s["items"].append({"k": "circle", "x": x, "y": y, "w": d, "h": d, "fill": fill})

    def size_of(self, name):
        return Image.open(self.shots / f"{name}.png").size

    def shot(self, s, name, x, y, w=None, h=None, frame=True):
        """Place a screenshot, scaled to fit the given width or height without distorting it."""
        iw, ih = self.size_of(name)
        ratio = iw / ih
        if w and not h: h = w / ratio
        elif h and not w: w = h * ratio
        elif w and h:
            if w / h > ratio: w = h * ratio
            else: h = w / ratio
        if frame: self.box(s, x - 0.06, y - 0.06, w + 0.12, h + 0.12, fill=CARD, line=LINE, radius=0.04)
        s["items"].append({"k": "img", "src": str(self.shots / f"{name}.png"), "x": x, "y": y, "w": w, "h": h})
        return w, h

    def caption(self, s, txt, x, y, w, size=11):
        it = self.text(s, x, y, w, 0.35, size=size, color=MUTED)
        para(it, txt, space_after=0)

    def side_note(self, s, x, items, y=1.85, w=None, title=None, h=4.5):
        w = w or (W - x - 0.75)
        self.box(s, x, y, w, h)
        it = self.text(s, x + 0.28, y + 0.25, w - 0.56, h - 0.5, size=12.5, color=BODY, spacing=1.3)
        if title: para(it, title, bold=True, size=14, color=INK, space_after=10)
        for i, t in enumerate(items):
            para(it, t, space_after=0 if i == len(items) - 1 else 9)

    # ---- whole slides ----------------------------------------------------------------------------------------
    def head(self, s, title, sub, tx=0.75, size=30):
        h = self.text(s, tx, 0.5 if size >= 30 else 0.48, W - tx - 0.75, 0.8, size=size, color=INK, bold=True, font=HEAD_FONT)
        para(h, title, space_after=0)
        sb = self.text(s, tx, 1.28 if size >= 30 else 1.15, W - tx - 0.75, 0.45, size=13.5, color=MUTED)
        para(sb, sub, space_after=0)

    def shot_slide(self, title, sub, img, note, img_w=8.2, img_h=4.7, badge=None):
        """A screenshot on the left with room for a card on the right. `badge` numbers a step inside one workflow."""
        s = self.slide()
        tx = 0.75
        if badge:
            self.circle(s, 0.75, 0.5, 0.62)
            num = self.text(s, 0.75, 0.62, 0.62, 0.45, size=17, color="FFFFFF", bold=True, align="c", font=HEAD_FONT)
            para(num, str(badge), space_after=0)
            tx = 1.55
        self.head(s, title, sub, tx=tx, size=28)
        w, hh = self.shot(s, img, 0.75, 1.85, w=img_w, h=img_h)
        self.caption(s, note, 0.75, 1.85 + hh + 0.16, w)
        return s, 0.75 + w + 0.45, hh

    def wide_slide(self, title, sub, img, note, cols, img_h=3.5):
        """For a picture much wider than it is tall: full width, with the commentary in columns underneath (or one line)."""
        s = self.slide()
        self.head(s, title, sub)
        iw, ih = self.size_of(img)                        # a picture narrower than the slide is centred, not left-hung
        ix = 0.75 + max(0.0, (11.83 - min(11.83, img_h * iw / ih)) / 2)
        w, hh = self.shot(s, img, ix, 1.9, w=11.83 - 2 * (ix - 0.75), h=img_h)
        self.caption(s, note, ix, 1.9 + hh + 0.14, w)
        y = 1.9 + hh + 0.62
        if isinstance(cols, str):                         # a picture worth the whole slide gets one line under it
            it = self.text(s, 0.75, y, 11.83, 7.3 - y, size=13, color=BODY, spacing=1.3)
            para(it, cols, space_after=0)
            return s
        cw = (11.83 - 0.4 * (len(cols) - 1)) / len(cols)
        bh = 6.95 - y                                     # the cards run to the bottom margin, so nothing dead is left
        x = 0.75
        for t, b in cols:
            self.box(s, x, y, cw, bh)
            ht = self.text(s, x + 0.28, y + 0.28, cw - 0.56, 0.45, size=14, color=INK, bold=True)
            para(ht, t, space_after=0)
            bt = self.text(s, x + 0.28, y + 0.78, cw - 0.56, bh - 1.06, size=12.5, color=BODY, spacing=1.35)
            para(bt, b, space_after=0)
            x += cw + 0.4
        return s

    def two_up_slide(self, title, sub, left, right, lcap, rcap, notes, band_h=3.4):
        s = self.slide()
        self.head(s, title, sub)

        def fit(name, w, h):
            iw, ih = self.size_of(name)
            r = iw / ih
            return (h * r, h) if w / h > r else (w, w / r)

        lw, lh = fit(left, 5.85, band_h); rw, rh = fit(right, 5.6, band_h)
        band = max(lh, rh)
        self.shot(s, left, 0.75, 1.95 + (band - lh) / 2, w=lw, h=lh)
        self.caption(s, lcap, 0.75, 1.95 + band + 0.14, lw)
        self.shot(s, right, 6.95, 1.95 + (band - rh) / 2, w=rw, h=rh)
        self.caption(s, rcap, 6.95, 1.95 + band + 0.14, rw)
        y = 1.95 + band + 0.62
        it = self.text(s, 0.75, y, 11.8, 7.2 - y, size=12.5, color=BODY, spacing=1.3)
        for i, t in enumerate(notes):
            para(it, t, space_after=0 if i == len(notes) - 1 else 7)
        return s

    def stack_slide(self, title, sub, imgs, caps, notes, y=1.95, gap=0.62):
        """Two wide strips one above the other — for a before/after where the pictures are the comparison."""
        s = self.slide()
        self.head(s, title, sub)
        for img, cap in zip(imgs, caps):
            w, hh = self.shot(s, img, 0.75, y, w=11.83)
            self.caption(s, cap, 0.75, y + hh + 0.13, w)
            y += hh + gap
        it = self.text(s, 0.75, y + 0.05, 11.83, 7.25 - y, size=12.5, color=BODY, spacing=1.3)
        for i, t in enumerate(notes):
            para(it, t, space_after=0 if i == len(notes) - 1 else 8)
        return s

    def part_slide(self, kicker, title, lines):
        """A divider between the halves of a deck."""
        s = self.slide(DEEP)
        k = self.text(s, 1.1, 2.35, 10.5, 0.45, size=13, color=TEAL, bold=True)
        para(k, kicker, space_after=0)
        t = self.text(s, 1.1, 2.85, 10.8, 1.1, size=40, color="FFFFFF", bold=True, font=HEAD_FONT, spacing=1.0)
        para(t, title, space_after=0)
        b = self.text(s, 1.1, 4.2, 10.8, 1.6, size=15, color=PALE, spacing=1.4)
        for i, ln in enumerate(lines):
            para(b, ln, space_after=0 if i == len(lines) - 1 else 8)
        return s

    def columns_slide(self, title, sub, cols, bg=INK, y=2.15, h=2.75, foot=None):
        """Three or four cards across a dark slide — the opening statement, or the closing one."""
        s = self.slide(bg)
        ht = self.text(s, 0.9, 0.9, 11.5, 0.9, size=32, color="FFFFFF", bold=True, font=HEAD_FONT)
        para(ht, title, space_after=0)
        if sub:
            sb = self.text(s, 0.9, 1.72, 11.5, 0.5, size=14.5, color=PALE)
            para(sb, sub, space_after=0)
        cw = (11.52 - 0.2 * (len(cols) - 1)) / len(cols)
        x = 0.9
        for t, b in cols:
            self.box(s, x, y, cw, h, fill=DEEP, line=DEEP)
            tt = self.text(s, x + 0.3, y + 0.27, cw - 0.6, 0.6, size=16, color="FFFFFF", bold=True, font=HEAD_FONT)
            para(tt, t, space_after=0)
            bt = self.text(s, x + 0.3, y + 0.95, cw - 0.6, h - 1.2, size=12, color=PALE, spacing=1.3)
            para(bt, b, space_after=0)
            x += cw + 0.2
        if foot:
            f = self.text(s, 0.9, y + h + 0.6, 11.5, 1.1, size=13, color="7E97A1", spacing=1.35)
            para(f, foot, space_after=0)
        return s

    # ---- renderers -------------------------------------------------------------------------------------------
    def save_pptx(self, out):
        prs = Presentation(); prs.slide_width, prs.slide_height = Inches(W), Inches(H)
        blank = prs.slide_layouts[6]
        for sp in self.slides:
            sl = prs.slides.add_slide(blank)
            bg = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(W), Inches(H))
            bg.fill.solid(); bg.fill.fore_color.rgb = hexc(sp["bg"]); bg.line.fill.background(); bg.shadow.inherit = False
            for it in sp["items"]:
                if it["k"] == "box":
                    sh = sl.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if it["radius"] else MSO_SHAPE.RECTANGLE,
                                             Inches(it["x"]), Inches(it["y"]), Inches(it["w"]), Inches(it["h"]))
                    if it["radius"]: sh.adjustments[0] = min(0.5, it["radius"] * 2 / max(it["w"], it["h"]))
                    sh.fill.solid(); sh.fill.fore_color.rgb = hexc(it["fill"])
                    sh.line.color.rgb = hexc(it["line"]); sh.line.width = Pt(it["lw"]); sh.shadow.inherit = False
                elif it["k"] == "circle":
                    sh = sl.shapes.add_shape(MSO_SHAPE.OVAL, Inches(it["x"]), Inches(it["y"]), Inches(it["w"]), Inches(it["h"]))
                    sh.fill.solid(); sh.fill.fore_color.rgb = hexc(it["fill"]); sh.line.fill.background(); sh.shadow.inherit = False
                elif it["k"] == "img":
                    sl.shapes.add_picture(it["src"], Inches(it["x"]), Inches(it["y"]), Inches(it["w"]), Inches(it["h"]))
                elif it["k"] == "text":
                    tb = sl.shapes.add_textbox(Inches(it["x"]), Inches(it["y"]), Inches(it["w"]), Inches(it["h"]))
                    tf = tb.text_frame; tf.word_wrap = True
                    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
                    tf.vertical_anchor = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE}[it["anchor"]]
                    for i, p in enumerate(it.get("paras", [])):
                        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                        par.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER}[it["align"]]
                        par.line_spacing = it["spacing"]; par.space_after = Pt(p["space_after"])
                        run = par.add_run(); run.text = p["txt"]
                        f = run.font; f.name = it["font"]; f.size = Pt(p["size"]); f.bold = p["bold"]; f.italic = p["italic"]
                        f.color.rgb = hexc(p["color"])
        prs.save(out)
        return Path(out)

    def save_preview(self, out, title="deck preview"):
        """The same geometry as HTML, so a browser can show what the deck looks like."""
        px = 96
        o = [f"<!doctype html><meta charset='utf-8'><title>{htmlmod.escape(title)}</title><style>",
             "body{margin:0;background:#33383f;font-family:Calibri,Carlito,sans-serif}",
             f".s{{position:relative;width:{W * px}px;height:{H * px}px;margin:18px auto;overflow:hidden}}",
             ".i{position:absolute}", ".t{white-space:pre-wrap}", "</style>"]
        for n, sp in enumerate(self.slides, 1):
            o.append(f"<div class='s' style='background:#{sp['bg']}' id='s{n}'>")
            for it in sp["items"]:
                st = f"left:{it['x'] * px}px;top:{it['y'] * px}px;width:{it['w'] * px}px;height:{it['h'] * px}px"
                if it["k"] == "box":
                    o.append(f"<div class='i' style='{st};background:#{it['fill']};border:{it['lw']}px solid #{it['line']};"
                             f"border-radius:{it['radius'] * px}px;box-sizing:border-box'></div>")
                elif it["k"] == "circle":
                    o.append(f"<div class='i' style='{st};background:#{it['fill']};border-radius:50%'></div>")
                elif it["k"] == "img":
                    o.append(f"<img class='i' style='{st}' src='{it['src']}'>")
                elif it["k"] == "text":
                    al = {"l": "left", "c": "center"}[it["align"]]
                    fam = "Cambria, 'Caladea', serif" if it["font"] == HEAD_FONT else "Calibri, Carlito, sans-serif"
                    inner = "".join(
                        f"<div style='font-size:{p['size']}pt;color:#{p['color']};font-weight:{700 if p['bold'] else 400};"
                        f"font-style:{'italic' if p['italic'] else 'normal'};line-height:{it['spacing']};"
                        f"margin-bottom:{p['space_after']}pt'>{htmlmod.escape(p['txt'])}</div>" for p in it.get("paras", []))
                    o.append(f"<div class='i t' style='{st};text-align:{al};font-family:{fam};display:flex;"
                             f"flex-direction:column;justify-content:{'center' if it['anchor'] == 'm' else 'flex-start'}'>{inner}</div>")
            o.append(f"<div style='position:absolute;right:10px;bottom:6px;font-size:9pt;color:#9aa4b2'>{n}</div></div>")
        Path(out).write_text("\n".join(o), encoding="utf-8")
        return Path(out)
