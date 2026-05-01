"""
PDF Processor — Google Translate pipeline
==========================================

HOW GOOGLE TRANSLATE DOES IT (reverse-engineered):
  1. Extract text blocks with exact bounding boxes (position, size, font weight)
  2. Render original page as a background image (pixels, not vectors)
  3. WHITE-OUT the text regions on the background image
  4. Translate each text block
  5. Overlay translated text on top of the white-out background using WeasyPrint
     which calls Pango → HarfBuzz for correct Devanagari/Nepali shaping

This is why Google's output looks identical to the original — it literally uses
the original page as a background image. We implement this exactly.

Dependencies: pymupdf (fitz), pdfplumber, weasyprint
"""
import os
import re
import io
import base64
import html as _html
import time
import pdfplumber
import fitz  # PyMuPDF


# ── Font paths ──────────────────────────────────────────────────────────────

def _find(*candidates):
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None

FONT_REGULAR = _find(
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
FONT_BOLD = _find(
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


# ── Extraction ──────────────────────────────────────────────────────────────

def _words_to_lines(words, y_tol=4.0):
    if not words:
        return []
    sw = sorted(words, key=lambda w: (round(w["top"] / y_tol) * y_tol, w["x0"]))
    lines, cur, cy = [], [], None
    for w in sw:
        if cy is None or abs(w["top"] - cy) > y_tol:
            if cur:
                lines.append(cur)
            cur, cy = [w], w["top"]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    result = []
    for lw in lines:
        lw = sorted(lw, key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in lw).strip()
        if text:
            result.append({
                "top":    min(w["top"]                       for w in lw),
                "bottom": max(w.get("bottom", w["top"]+12)  for w in lw),
                "x0":     min(w["x0"]                       for w in lw),
                "x1":     max(w["x1"]                       for w in lw),
                "words":  lw,
                "text":   text,
            })
    return result


def _avg_size(words):
    s = [w.get("size") or 11 for w in words]
    return sum(s) / len(s) if s else 11


def _is_bold(words):
    return any(
        "bold" in (w.get("fontname") or "").lower() or
        (w.get("fontname") or "").endswith("-B") or
        "bd" in (w.get("fontname") or "").lower()
        for w in words
    )


def _lines_to_blocks(lines, page_width, gap=7.0):
    if not lines:
        return []
    result, cur = [], [lines[0]]
    csz = _avg_size(lines[0]["words"])
    cx0 = lines[0]["x0"]
    for i in range(1, len(lines)):
        prev, curr = lines[i-1], lines[i]
        split = (
            curr["top"] - prev["bottom"] > gap or
            abs(_avg_size(curr["words"]) - csz) > 1.8 or
            abs(curr["x0"] - cx0) > 14
        )
        if split:
            blk = _make_block(cur, page_width)
            if blk:
                result.append(blk)
            cur, csz, cx0 = [curr], _avg_size(curr["words"]), curr["x0"]
        else:
            cur.append(curr)
    blk = _make_block(cur, page_width)
    if blk:
        result.append(blk)
    return [b for b in result if b and b["text"].strip()]


def _make_block(lines, page_width):
    words = [w for ln in lines for w in ln["words"]]
    text  = " ".join(ln["text"] for ln in lines).strip()
    if not text:
        return None
    avg_sz = _avg_size(words)
    max_sz = max(w.get("size") or 11 for w in words)
    bold   = _is_bold(words)
    x0     = min(ln["x0"] for ln in lines)
    x1     = max(ln["x1"] for ln in lines)
    cx     = (x0 + x1) / 2
    pcx    = page_width / 2

    if abs(cx - pcx) < 32 and x0 > 40:
        align = "center"
    elif x0 > page_width * 0.55:
        align = "right"
    else:
        align = "left"

    if   max_sz >= 20:                                  hlevel = 1
    elif max_sz >= 14 or (bold and avg_sz >= 13):       hlevel = 2
    elif bold and len(text.split()) <= 10:              hlevel = 3
    else:                                               hlevel = 0

    return {
        "text":    text,
        "x0": x0, "x1": x1,
        "top":     lines[0]["top"],
        "bottom":  lines[-1]["bottom"],
        "size":    avg_sz,
        "max_size": max_sz,
        "bold":    bold,
        "align":   align,
        "hlevel":  hlevel,
        "nlines":  len(lines),
        "indent":  max(0.0, x0 - 72.0),
    }


def extract_pdf_structure(path: str) -> dict:
    """Extract text blocks + page images from PDF."""
    pages_data = []
    with pdfplumber.open(path) as pdf:
        meta = {"page_count": len(pdf.pages), "metadata": pdf.metadata or {}}
        fitz_doc = fitz.open(path)

        for page_num, page in enumerate(pdf.pages):
            w, h = float(page.width), float(page.height)

            # Detect tables
            tbl_cfg = {"vertical_strategy": "lines", "horizontal_strategy": "lines",
                       "intersection_tolerance": 5, "snap_tolerance": 3}
            tbl_objs   = page.find_tables(tbl_cfg) or []
            tbl_bboxes = [t.bbox for t in tbl_objs]
            tables     = [t.extract() for t in tbl_objs if t.extract()]

            words = page.extract_words(
                x_tolerance=3, y_tolerance=3,
                keep_blank_chars=False, use_text_flow=True,
                extra_attrs=["size", "fontname"]
            ) or []

            def in_table(wd):
                for bb in tbl_bboxes:
                    if bb[0]-3 <= wd["x0"] <= bb[2]+3 and bb[1]-3 <= wd["top"] <= bb[3]+3:
                        return True
                return False

            text_words = [wd for wd in words if not in_table(wd)]
            lines      = _words_to_lines(text_words)
            for i in range(1, len(lines)):
                lines[i]["gap_before"] = lines[i]["top"] - lines[i-1]["bottom"]
            if lines:
                lines[0]["gap_before"] = 0

            blocks = _lines_to_blocks(lines, w)

            # ── Render page to image (Google's key trick) ──────────────
            # We render at 2x for retina clarity, white-out text areas,
            # then use as background behind translated text overlay
            fitz_page = fitz_doc[page_num]
            mat = fitz.Matrix(2.0, 2.0)  # 2x scale for crisp output
            pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
            img_b64 = base64.b64encode(pix.tobytes("png")).decode()

            pages_data.append({
                "page_num":   page_num + 1,
                "width":      w,
                "height":     h,
                "blocks":     blocks,
                "paragraphs": blocks,   # alias
                "tables":     tables,
                "tbl_bboxes": tbl_bboxes,
                "full_text":  page.extract_text() or "",
                "img_b64":    img_b64,  # base64 PNG of full page at 2x
                "img_scale":  2.0,
            })

        fitz_doc.close()
    return {"pages": pages_data, "meta": meta}


# ── Translation ─────────────────────────────────────────────────────────────

def translate_pdf_structure(structure: dict, src: str, tgt: str,
                             progress_cb=None) -> dict:
    from backend.translator import translate_paragraph, translate_sentence
    total = sum(
        len(p["blocks"]) + sum(len(r) for t in p["tables"] for r in t)
        for p in structure["pages"]
    )
    done = [0]

    translated_pages = []
    for page in structure["pages"]:
        t_blocks = []
        for blk in page["blocks"]:
            res = translate_paragraph(blk["text"], src, tgt)
            tb  = dict(blk)
            tb["text"]     = res["output"]
            tb["original"] = blk["text"]
            t_blocks.append(tb)
            done[0] += 1
            if progress_cb:
                progress_cb(done[0], total, blk["text"])
            time.sleep(0.08)

        t_tables = []
        for table in page["tables"]:
            t_tbl = []
            for row in table:
                t_row = []
                for cell in row:
                    s = str(cell).strip() if cell is not None else ""
                    if s:
                        res = translate_sentence(s, src, tgt)
                        t_row.append(res["output"] if res["ok"] else s)
                        done[0] += 1
                        if progress_cb:
                            progress_cb(done[0], total, s)
                        time.sleep(0.08)
                    else:
                        t_row.append("")
                t_tbl.append(t_row)
            t_tables.append(t_tbl)

        translated_pages.append({
            **page,
            "blocks":     t_blocks,
            "paragraphs": t_blocks,
            "tables":     t_tables,
        })

    return {"pages": translated_pages, "meta": structure["meta"]}


# ── Reconstruction — Google's image-background + text overlay approach ──────

def _esc(text: str) -> str:
    return _html.escape(str(text))


def _font_face_css() -> str:
    parts = []
    if FONT_REGULAR:
        parts.append(f"@font-face {{ font-family:'DocFont'; font-weight:normal; src:url('file://{FONT_REGULAR}'); }}")
    if FONT_BOLD:
        parts.append(f"@font-face {{ font-family:'DocFont'; font-weight:bold;   src:url('file://{FONT_BOLD}'); }}")
    return "\n".join(parts)


def _page_to_html(page: dict, page_idx: int) -> str:
    """
    Render one page as HTML:
    - Full-page background = original page rendered as PNG (whited-out text areas)
    - Translated text overlaid at exact original coordinates
    This is the Google Translate PDF pipeline.
    """
    w    = page["width"]
    h    = page["height"]
    img  = page.get("img_b64", "")
    scale = page.get("img_scale", 2.0)
    blocks = page.get("blocks", [])
    tables = page.get("tables", [])
    tbboxes = page.get("tbl_bboxes", [])

    # Points → mm conversion for CSS (PDF points, 1pt = 0.3528mm)
    def pt2mm(v): return v * 0.3528

    W_mm = pt2mm(w)
    H_mm = pt2mm(h)

    # Build white-out rects for text blocks (erase original text)
    whiteout_css = ""
    for blk in blocks:
        x0  = pt2mm(blk["x0"]    - 3)
        y0  = pt2mm(blk["top"]   - 2)
        bw  = pt2mm(blk["x1"]    - blk["x0"] + 6)
        bh  = pt2mm(blk["bottom"]- blk["top"] + 4)
        whiteout_css += (
            f".wo {{ position:absolute; background:white; }}\n"
        )
        # We'll render whiteout divs inline

    # Build overlay HTML for each text block
    overlays = []
    for blk in blocks:
        x0  = pt2mm(blk["x0"]    - 2)
        y0  = pt2mm(blk["top"]   - 1)
        bw  = pt2mm(blk["x1"]    - blk["x0"] + 4)
        bh  = pt2mm(blk["bottom"]- blk["top"] + 6)
        sz  = max(blk.get("size", 11), 7)
        sz_pt = sz  # keep as pt, convert: 1pt = 0.3528mm
        sz_mm = sz * 0.3528
        bold = blk.get("bold", False) or blk.get("hlevel", 0) in (1,2,3)
        align = blk.get("align", "left")
        text  = _esc(blk.get("text", ""))

        # Color by heading level
        hlevel = blk.get("hlevel", 0)
        color_map = {1: "#1A1A2E", 2: "#2E4057", 3: "#3D5A80"}
        color = color_map.get(hlevel, "#111111")

        overlays.append(f"""
<div class="wo" style="left:{x0:.2f}mm;top:{y0:.2f}mm;width:{bw:.2f}mm;height:{bh:.2f}mm;"></div>
<div class="ov" style="
  left:{x0:.2f}mm; top:{y0:.2f}mm;
  width:{bw:.2f}mm; min-height:{bh:.2f}mm;
  font-size:{sz_mm:.3f}mm;
  font-weight:{'bold' if bold else 'normal'};
  text-align:{align};
  color:{color};
">{text}</div>""")

    # Build table overlays
    for i, (tbl_data, bbox) in enumerate(zip(tables, tbboxes)):
        if not tbl_data:
            continue
        bx0 = pt2mm(bbox[0])
        by0 = pt2mm(bbox[1])
        bw2 = pt2mm(bbox[2] - bbox[0])
        bh2 = pt2mm(bbox[3] - bbox[1])
        col_count = max(len(r) for r in tbl_data)
        col_w_mm  = bw2 / max(col_count, 1)

        rows_html = ""
        for ri, row in enumerate(tbl_data):
            cells = [str(c) if c is not None else "" for c in row]
            cells += [""] * (col_count - len(cells))
            tag = "th" if ri == 0 else "td"
            cells_html = "".join(f"<{tag}>{_esc(c)}</{tag}>" for c in cells)
            rows_html += f"<tr>{cells_html}</tr>"

        overlays.append(f"""
<div class="wo" style="left:{bx0:.2f}mm;top:{by0:.2f}mm;width:{bw2:.2f}mm;height:{bh2:.2f}mm;"></div>
<div class="tbl-ov" style="left:{bx0:.2f}mm;top:{by0:.2f}mm;width:{bw2:.2f}mm;">
  <table style="width:100%;font-size:2.8mm;">{rows_html}</table>
</div>""")

    overlays_html = "\n".join(overlays)

    pb_style = "page-break-after:always;" if page_idx >= 0 else ""

    return f"""
<div class="page" style="
  width:{W_mm:.2f}mm; height:{H_mm:.2f}mm;
  position:relative; overflow:hidden;
  {pb_style}
  background: url('data:image/png;base64,{img}') no-repeat top left / 100% 100%;
">
{overlays_html}
</div>"""


def reconstruct_pdf(structure: dict, output_path: str):
    """
    Reconstruct translated PDF using the Google Translate approach:
    original page as background image + translated text overlaid via WeasyPrint.
    WeasyPrint uses Pango + HarfBuzz → perfect Devanagari shaping.
    """
    from weasyprint import HTML as WP

    pages     = structure.get("pages", [])
    font_css  = _font_face_css()
    ff        = "'DocFont','FreeSans','DejaVu Sans',sans-serif"

    pages_html = "\n".join(
        _page_to_html(page, idx) for idx, page in enumerate(pages)
    )

    html = f"""<!DOCTYPE html>
<html lang="ne"><head>
<meta charset="UTF-8">
<style>
{font_css}

*, *::before, *::after {{ box-sizing: border-box; margin:0; padding:0; }}

body {{
  font-family: {ff};
  background: white;
}}

@page {{
  margin: 0;
  size: auto;
}}

.page {{
  display: block;
  page-break-after: always;
  break-after: page;
}}

.wo {{
  position: absolute;
  background: white;
  z-index: 1;
}}

.ov {{
  position: absolute;
  font-family: {ff};
  line-height: 1.35;
  z-index: 2;
  word-wrap: break-word;
  overflow: visible;
  -weasyprint-hinting: auto;
}}

.tbl-ov {{
  position: absolute;
  z-index: 2;
  font-family: {ff};
}}

.tbl-ov table {{
  border-collapse: collapse;
  width: 100%;
}}

.tbl-ov th {{
  background: #2E4057;
  color: white;
  font-weight: bold;
  padding: 1mm 2mm;
  border: 0.3mm solid #C8CDD5;
  font-size: 2.8mm;
}}

.tbl-ov td {{
  padding: 1mm 2mm;
  border: 0.3mm solid #C8CDD5;
  font-size: 2.6mm;
  background: white;
}}

.tbl-ov tr:nth-child(even) td {{
  background: #F5F7FA;
}}
</style>
</head><body>
{pages_html}
</body></html>"""

    WP(string=html, base_url="/").write_pdf(output_path)
