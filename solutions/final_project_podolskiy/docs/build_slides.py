"""slides.md -> slides.pdf (16:9) для защиты итогового проекта. Запуск: python docs/build_slides.py slides.md mart slides.pdf"""
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Frame, Paragraph, Spacer, Table, TableStyle, KeepInFrame

SRC, FIG_DIR, OUT = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
W, H = 960, 540
FD = "/usr/share/fonts/truetype/dejavu/"
pdfmetrics.registerFont(TTFont("Sans", FD + "DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Bold", FD + "DejaVuSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Oblique", FD + "DejaVuSans-Oblique.ttf"))
pdfmetrics.registerFont(TTFont("Mono", FD + "DejaVuSansMono.ttf"))
from reportlab.pdfbase.pdfmetrics import registerFontFamily
registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans-Oblique", boldItalic="Sans-Bold")

INK, MUTED, ACCENT, RULE = colors.HexColor("#1f2937"), colors.HexColor("#6b7280"), colors.HexColor("#2a6fdb"), colors.HexColor("#d1d5db")
body = ParagraphStyle("b", fontName="Sans", fontSize=15, leading=20, textColor=INK, alignment=TA_LEFT)
bullet = ParagraphStyle("li", parent=body, leftIndent=16, bulletIndent=2, spaceAfter=5)
lead = ParagraphStyle("lead", parent=body, fontName="Sans-Bold", fontSize=18, leading=24, spaceAfter=10)
cell = ParagraphStyle("c", parent=body, fontSize=13, leading=17)
cellb = ParagraphStyle("cb", parent=cell, fontName="Sans-Bold")


def inline(md: str) -> str:
    s = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    codes = []
    def keep(m):
        codes.append(m.group(1)); return f"\x00{len(codes) - 1}\x00"
    s = re.sub(r"`([^`]+)`", keep, s)                      # код защищаем от разметки * и **
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<i>\1</i>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: f'<font name="Mono" size="12" color="#374151">{codes[int(m.group(1))]}</font>', s)


def parse(md: str):
    slides = []
    for block in re.split(r"\n---\n", md):
        m = re.search(r"^## (Слайд \d+)\. (.+)$", block, re.M)
        if not m:
            continue
        lines = block[m.end():].strip("\n").splitlines()
        items, cur, table = [], None, None
        for ln in lines:
            if ln.startswith("*Говорить:*"):
                cur = None
                continue                                   # заметки докладчика в PDF не выводим
            if ln.startswith("|"):
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                if all(re.fullmatch(r":?-+:?", c) for c in cells):
                    continue
                if table is None:
                    table = []
                    items.append(("table", table))
                table.append(cells)
                cur = None
                continue
            table = None
            if ln.startswith("- ") or re.match(r"^\d+\. ", ln):
                cur = ["li", ln.split(" ", 1)[1], "•" if ln.startswith("- ") else ln.split(".")[0] + "."]
                items.append(cur)
            elif ln.startswith("  ") and cur is not None:
                cur[1] += " " + ln.strip()
            elif ln.startswith("**") and ln.endswith("**"):
                items.append(["lead", ln.strip("*")]); cur = None
            elif ln.startswith("Графики:"):
                cur = ["skip", ""]                          # графики показаны на самом слайде
            elif ln.strip():
                if cur is not None and cur[0] in ("p", "skip"):
                    cur[1] += " " + ln.strip()
                else:
                    cur = ["p", ln.strip()]; items.append(cur)
            else:
                cur = None
        slides.append((m.group(1), m.group(2), items))
    return slides


FIGS = {"Слайд 5": ["fig1_stars_by_topic.png", "fig2_size_vs_stars.png"], "Слайд 3": ["fig3_activity_by_topic_size.png"]}


def draw(c, num, title, items, total):
    c.setFillColor(colors.white); c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(ACCENT); c.rect(0, H - 6, W, 6, fill=1, stroke=0)
    c.setFont("Sans", 11); c.setFillColor(MUTED)
    c.drawString(40, H - 34, f"{num} из {total}  ·  Итоговый проект · Подольский Ф. А., ПМИ-3-2025")
    c.setFont("Sans-Bold", 26); c.setFillColor(INK); c.drawString(40, H - 72, title)
    c.setStrokeColor(RULE); c.line(40, H - 84, W - 40, H - 84)
    c.setFont("Sans", 10); c.setFillColor(MUTED); c.drawRightString(W - 40, 18, "Данные: реальный прогон 15.10.2026 · GitHub Topics + GitHub REST API")

    figs = [FIG_DIR / f for f in FIGS.get(num, [])]
    text_w = W - 80 if not figs else 430
    flow = []
    for it in items:
        if it[0] == "lead":
            flow.append(Paragraph(inline(it[1]), lead))
        elif it[0] == "li":
            flow.append(Paragraph(inline(it[1]), bullet, bulletText=it[2]))
        elif it[0] == "p":
            flow.append(Paragraph(inline(it[1]), body)); flow.append(Spacer(1, 6))
        elif it[0] == "table":
            rows = [[Paragraph(inline(x), cellb if j == 0 else cell) for j, x in enumerate(r)] for r in it[1]]
            t = Table(rows, colWidths=[text_w * 0.32, text_w * 0.68])
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4fd")),
                                   ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
            flow.append(t); flow.append(Spacer(1, 8))
    frame = Frame(40, 34, text_w, H - 130, showBoundary=0, leftPadding=0, rightPadding=0, topPadding=6)
    frame.addFromList([KeepInFrame(text_w, H - 140, flow, mode="shrink")], c)

    if figs:
        x0, avail_w, top = 490, W - 490 - 30, H - 96
        slot_h = (H - 96 - 34) / len(figs)
        for i, f in enumerate(figs):
            img = ImageReader(str(f)); iw, ih = img.getSize()
            s = min(avail_w / iw, (slot_h - 8) / ih)
            w, h = iw * s, ih * s
            c.drawImage(img, x0 + (avail_w - w) / 2, top - (i + 1) * slot_h + (slot_h - h) / 2, w, h)
    c.showPage()


slides = parse(SRC.read_text(encoding="utf-8"))
c = canvas.Canvas(str(OUT), pagesize=(W, H))
c.setTitle("Итоговый проект: популярность и активность Python-репозиториев")
c.setAuthor("Подольский Ф. А.")
for num, title, items in slides:
    draw(c, num, title, items, len(slides))
c.save()
print(f"{OUT}: {len(slides)} слайдов")
