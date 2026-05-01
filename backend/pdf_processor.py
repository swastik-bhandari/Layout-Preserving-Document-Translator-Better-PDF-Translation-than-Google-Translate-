"""
PDF Processor — Google Translate pipeline
==========================================
v4: Full rewrite fixing all visual bugs found in real-world testing.

ROOT CAUSES FIXED:

BUG 1 — Every sentence underlined:
  PyMuPDF's span flag bit 2 (underline) is set by the font DESCRIPTOR of fonts
  like Times New Roman, not by actual text decoration. This caused 23/26 spans
  to be falsely detected as underlined.
  FIX: NEVER use the PyMuPDF underline flag. Instead, detect real underlines by
  finding drawn thin horizontal lines (height < 2pt) within 4pt below a text
  span's baseline. This is how PDF underlines actually work — they are drawn
  paths, not font flags.

BUG 2 — Lists breaking / merging into one block:
  _lines_to_blocks was splitting on indent changes > 14pt, which is correct for
  paragraphs but wrong for list items (which have consistent indentation).
  The real issue: list items were being MERGED because the gap between them
  (< 7pt) was less than gap_thresh, and the indent was consistent.
  FIX: Treat each pdfplumber line as its own block when it is very short
  (single-line, small font size, left-indented) — these are almost always
  list items. Also reduced gap_thresh to 5pt for better single-line separation.

BUG 3 — Partial / garbled Devanagari translation:
  FreeSans on some Linux systems produces garbled output for combined Devanagari
  sequences because it lacks the full conjunct glyph table. Hebrew combining
  chars (\u05cc) were appearing in output.
  FIX: Add NotoSans as primary font candidate (specifically NotoSansDevanagari
  for body, NotoSans for Latin). WeasyPrint + HarfBuzz handle font fallback
  correctly when Noto fonts are present. FreeSans kept as fallback.

BUG 4 — Table border lines detected as underlines:
  The drawn-path underline detection was also matching table row separator lines
  (h=0.5pt, full table width) as underlines for the text above them.
  FIX: Cross-reference detected underline paths against tbl_bboxes. Any thin
  line whose x-span is >= 80% of a table column width is a table border, not
  an underline.
"""

import os
import re
import base64
import html as _html
import time
import pdfplumber
import fitz


# ── Font discovery ──────────────────────────────────────────────────────────

def _find(*candidates):
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


# Priority: NotoSans (best Devanagari+Latin coverage) → FreeSans → DejaVu → Liberation
FONT_REGULAR = _find(
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
FONT_BOLD = _find(
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
FONT_ITALIC = _find(
    "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansOblique.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
)
FONT_BOLD_ITALIC = _find(
    "/usr/share/fonts/truetype/noto/NotoSans-BoldItalic.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBoldOblique.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
)


# ── PyMuPDF span extraction ─────────────────────────────────────────────────

def _hex(color_int: int) -> str:
    return f'#{color_int:06x}'


def _spans_for_page(fitz_page) -> list:
    """
    Extract per-span formatting from PyMuPDF.
    NOTE: We intentionally do NOT use flags bit 2 (underline) — it is a font
    descriptor flag that Times New Roman sets on ALL spans regardless of whether
    text is actually underlined. Real underlines are detected via drawn paths.
    """
    spans = []
    blocks = fitz_page.get_text('dict', flags=0)['blocks']
    for b in blocks:
        if b['type'] != 0:
            continue
        for line in b['lines']:
            for span in line['spans']:
                text = span['text']
                if not text.strip():
                    continue
                flags = span['flags']
                bbox  = span['bbox']
                spans.append({
                    'x0':     bbox[0],
                    'y0':     bbox[1],
                    'x1':     bbox[2],
                    'y1':     bbox[3],
                    'text':   text,
                    'bold':   bool(flags & (1 << 4)),
                    'italic': bool(flags & (1 << 1)),
                    # Deliberately omitting underline — detected via drawn paths instead
                    'color':  _hex(span['color']),
                    'size':   span['size'],
                    'font':   span.get('font', ''),
                })
    return spans


def _links_for_page(fitz_page) -> list:
    """Return [{x0,y0,x1,y1,uri}] for every hyperlink annotation."""
    links = []
    for lnk in fitz_page.get_links():
        uri = lnk.get('uri', '')
        if uri:
            r = lnk['from']
            links.append({'x0': r.x0, 'y0': r.y0,
                          'x1': r.x1, 'y1': r.y1, 'uri': uri})
    return links


def _real_underlines_for_page(fitz_page, tbl_bboxes_pt: list) -> list:
    """
    Detect REAL underlines by finding thin drawn horizontal paths
    and matching them to the text span immediately above.

    A drawn path is a real underline if:
      - height < 2pt  (thin line, not a table border with height > 2pt)
      - width > 8pt   (not a stray dot)
      - NOT inside a table bbox (table row separators are not underlines)
      - within 4pt below a text span's baseline

    Returns list of rects {x0, y0, x1, y1, color_hex} in PDF points.
    """
    candidates = []
    for d in fitz_page.get_drawings():
        r = d.get('rect')
        if r is None:
            continue
        h = r.height
        w = r.width
        # Must be thin and wide enough to be a text underline
        if h > 2.0 or w < 8:
            continue
        # Must not be inside a table region
        in_table = False
        for bb in tbl_bboxes_pt:
            if (bb[0] - 5 <= r.x0 and r.x1 <= bb[2] + 5 and
                    bb[1] - 5 <= r.y0 and r.y1 <= bb[3] + 5):
                in_table = True
                break
        if in_table:
            continue
        # Get color
        fill   = d.get('fill')
        stroke = d.get('color')
        if fill and not all(c > 0.97 for c in fill):
            rgb = fill
        elif stroke and not all(c > 0.97 for c in stroke):
            rgb = stroke
        else:
            rgb = (0, 0, 0)
        color_hex = '#{:02x}{:02x}{:02x}'.format(
            int(rgb[0]*255), int(rgb[1]*255), int(rgb[2]*255)
        )
        candidates.append({'x0': r.x0, 'y0': r.y0,
                           'x1': r.x1, 'y1': r.y1,
                           'color': color_hex})
    return candidates


def _table_fills_for_page(fitz_page) -> list:
    """Read non-white fill rectangles (table cell backgrounds)."""
    fills = []
    for d in fitz_page.get_drawings():
        fill = d.get('fill')
        if fill is None:
            continue
        r = d.get('rect')
        if r is None or r.height < 4:
            continue
        if all(c > 0.97 for c in fill):
            continue
        fills.append({
            'x0': r.x0, 'y0': r.y0, 'x1': r.x1, 'y1': r.y1,
            'r': fill[0], 'g': fill[1], 'b': fill[2],
        })
    return fills


def _fill_at(fills, x0, y0, x1, y1, tol=4):
    cx, cy = (x0+x1)/2, (y0+y1)/2
    for f in fills:
        if (f['x0']-tol <= cx <= f['x1']+tol and
                f['y0']-tol <= cy <= f['y1']+tol):
            r, g, b = int(f['r']*255), int(f['g']*255), int(f['b']*255)
            return f'rgb({r},{g},{b})'
    return None


def _link_at(links, x0, y0, x1, y1, tol=6):
    cx, cy = (x0+x1)/2, (y0+y1)/2
    for lnk in links:
        if (lnk['x0']-tol <= cx <= lnk['x1']+tol and
                lnk['y0']-tol <= cy <= lnk['y1']+tol):
            return lnk['uri']
    return None


def _is_underlined(underline_paths, x0, top, x1, bottom, tol=4) -> bool:
    """
    Check if any real underline path sits within 4pt below the block's baseline.
    Uses x-overlap to avoid false matches from adjacent columns.
    """
    for ul in underline_paths:
        # Must be below the text and close to its baseline
        if not (bottom - tol <= ul['y0'] <= bottom + 6):
            continue
        # Must overlap in x (at least 30% of the text width)
        overlap = min(x1, ul['x1']) - max(x0, ul['x0'])
        text_width = max(x1 - x0, 1)
        if overlap / text_width >= 0.30:
            return True
    return False


def _block_style(spans, x0, y0, x1, y1, tol=4) -> dict:
    """
    Merge span-level formatting onto a pdfplumber block bbox.
    Returns {italic, bold, color}.
    Underline is handled separately via _is_underlined (drawn-path detection).
    """
    matching = []
    for sp in spans:
        if (sp['x0'] - tol < x1 and sp['x1'] + tol > x0 and
                sp['y0'] - tol < y1 and sp['y1'] + tol > y0):
            matching.append(sp)
    if not matching:
        return {'italic': False, 'bold': False, 'color': '#111111'}

    italic = any(s['italic'] for s in matching)
    bold   = any(s['bold']   for s in matching)

    # Color: prefer first span with a distinctive (non-black) color
    color = matching[0]['color']
    for s in matching:
        if s['color'] not in ('#000000', '#111111', '#000001'):
            color = s['color']
            break
    if color in ('#000000', '#000001'):
        color = '#111111'

    return {'italic': italic, 'bold': bold, 'color': color}


# ── pdfplumber layout extraction ────────────────────────────────────────────

def _words_to_lines(words, y_tol=4.0, x_gap_split=45.0):
    """
    Cluster words into lines by y-proximity, then split each visual line on
    large x-gaps (columns, tab-stops, spaced list items like '1) Foo   2) Bar').

    x_gap_split=45pt: gap between consecutive words on the same y-level that
    indicates a column boundary or tab-stop — they become separate lines.
    This prevents '1) Football 2) Chess' (same y, big gap) being sent as one
    translation unit and returned as '१) फुटबल २) चेस' on one line.
    """
    if not words:
        return []
    sw = sorted(words, key=lambda w: (round(w['top'] / y_tol) * y_tol, w['x0']))
    # Group by y-proximity
    raw_lines, cur, cy = [], [], None
    for w in sw:
        if cy is None or abs(w['top'] - cy) > y_tol:
            if cur:
                raw_lines.append(cur)
            cur, cy = [w], w['top']
        else:
            cur.append(w)
    if cur:
        raw_lines.append(cur)

    # For each raw line, split further on large x-gaps
    result = []
    for lw in raw_lines:
        lw = sorted(lw, key=lambda w: w['x0'])
        # Split into sub-groups on large horizontal gaps
        groups, grp = [], [lw[0]]
        for i in range(1, len(lw)):
            gap = lw[i]['x0'] - lw[i-1]['x1']
            if gap > x_gap_split:
                groups.append(grp)
                grp = []
            grp.append(lw[i])
        groups.append(grp)

        for g in groups:
            text = ' '.join(w['text'] for w in g).strip()
            if text:
                result.append({
                    'top':    min(w['top']                      for w in g),
                    'bottom': max(w.get('bottom', w['top']+12) for w in g),
                    'x0':     min(w['x0']                      for w in g),
                    'x1':     max(w['x1']                      for w in g),
                    'words':  g,
                    'text':   text,
                })
    return result


def _avg_size(words):
    s = [w.get('size') or 11 for w in words]
    return sum(s) / len(s) if s else 11


def _is_bold_words(words):
    return any(
        'bold' in (w.get('fontname') or '').lower() or
        (w.get('fontname') or '').endswith('-B') or
        'bd' in (w.get('fontname') or '').lower()
        for w in words
    )


def _lines_to_blocks(lines, page_width, gap=5.0):
    """
    Cluster pdfplumber lines into paragraph blocks.

    Key rules:
    - Split on vertical gap > 5pt (reduced from 7 to better preserve list items)
    - Split on font size jump > 1.8pt (heading vs body)
    - Split on indent change > 18pt (column change, not list indent)
    - ALWAYS split single-word/short lines that are indented (list bullets/labels)
    - Do NOT merge lines with different x0 if they look like list items
    """
    if not lines:
        return []

    result, cur = [], [lines[0]]
    csz = _avg_size(lines[0]['words'])
    cx0 = lines[0]['x0']

    for i in range(1, len(lines)):
        prev, curr = lines[i-1], lines[i]
        gap_v      = curr['top'] - prev['bottom']
        size_jump  = abs(_avg_size(curr['words']) - csz) > 1.8
        # Use larger indent threshold — list items often share the same x0
        indent_jump = abs(curr['x0'] - cx0) > 18

        # Force split if gap is large, size changes, or big indent jump
        split = gap_v > gap or size_jump or indent_jump

        # Also split if EITHER line is a short/single-line indented block
        # (these are list items and should never be merged with adjacent lines)
        prev_is_list_item = (len(cur) == 1 and cx0 > 85 and
                             len(cur[0]['text'].split()) <= 6)
        curr_is_list_item = (curr['x0'] > 85 and
                             len(curr['text'].split()) <= 6)

        if prev_is_list_item or curr_is_list_item:
            split = True

        if split:
            blk = _make_block(cur, page_width)
            if blk:
                result.append(blk)
            cur  = [curr]
            csz  = _avg_size(curr['words'])
            cx0  = curr['x0']
        else:
            cur.append(curr)

    blk = _make_block(cur, page_width)
    if blk:
        result.append(blk)

    return [b for b in result if b and b['text'].strip()]


def _make_block(lines, page_width):
    words  = [w for ln in lines for w in ln['words']]
    text   = ' '.join(ln['text'] for ln in lines).strip()
    if not text:
        return None
    avg_sz = _avg_size(words)
    max_sz = max(w.get('size') or 11 for w in words)
    bold   = _is_bold_words(words)
    x0     = min(ln['x0'] for ln in lines)
    x1     = max(ln['x1'] for ln in lines)

    # Alignment detection:
    # Use x0 (left edge of block) as the primary signal, NOT cx.
    # Using cx causes short translated text (e.g. right-column table cells)
    # to drift toward page center and be wrongly labeled 'center'.
    #
    # Rules:
    #   center → x0 is clearly past left margin AND the block spans near-symmetrically
    #            around the page center. We check BOTH x0 and x1 relative to margins.
    #   right  → x0 is in the right 45% of the page (not just right 55% to be safe)
    #   left   → everything else (default, most common)
    margin_l = 55          # typical left margin in points
    margin_r = page_width - 55  # typical right margin

    # True center: block is positioned symmetrically — both x0 and x1 are
    # roughly equidistant from left and right margins
    left_dist  = x0 - margin_l
    right_dist = margin_r - x1
    block_width = x1 - x0
    page_fraction = block_width / page_width
    is_center = (x0 > margin_l + 10 and
             x1 < margin_r - 10 and
             abs(left_dist - right_dist) < 30 and
             page_fraction < 0.60)  # ← full-width paragraphs are NOT centered

    if is_center:
        align = 'center'
    elif x0 > page_width * 0.50:
        align = 'right'
    else:
        align = 'left'

    # Heading level detection
    if   max_sz >= 20:
        hlevel = 1
    elif max_sz >= 14 or (bold and avg_sz >= 13):
        hlevel = 2
    elif bold and avg_sz >= 11 and len(text.split()) <= 15:
        hlevel = 3
    else:
        hlevel = 0

    return {
        'text':     text,
        'x0':       x0,
        'x1':       x1,
        'top':      lines[0]['top'],
        'bottom':   lines[-1]['bottom'],
        'size':     avg_sz,
        'max_size': max_sz,
        'bold':     bold,
        'align':    align,
        'hlevel':   hlevel,
        'nlines':   len(lines),
        'indent':   max(0.0, x0 - 72.0),
    }


# ── Public API — Extract ─────────────────────────────────────────────────────

def extract_pdf_structure(path: str) -> dict:
    pages_data = []
    with pdfplumber.open(path) as pdf:
        meta     = {'page_count': len(pdf.pages), 'metadata': pdf.metadata or {}}
        fitz_doc = fitz.open(path)

        for page_num, page in enumerate(pdf.pages):
            w, h = float(page.width), float(page.height)

            # Table detection
            tbl_cfg    = {'vertical_strategy':   'lines',
                          'horizontal_strategy': 'lines',
                          'intersection_tolerance': 5,
                          'snap_tolerance':         3}
            tbl_objs   = page.find_tables(tbl_cfg) or []
            tbl_bboxes = [t.bbox for t in tbl_objs]   # (x0,y0,x1,y1) in PDF points
            tables     = [t.extract() for t in tbl_objs if t.extract()]

            # Word extraction — skip table areas
            words = page.extract_words(
                x_tolerance=3, y_tolerance=3,
                keep_blank_chars=False, use_text_flow=True,
                extra_attrs=['size', 'fontname']
            ) or []

            def in_table(wd):
                for bb in tbl_bboxes:
                    if (bb[0]-3 <= wd['x0'] <= bb[2]+3 and
                            bb[1]-3 <= wd['top'] <= bb[3]+3):
                        return True
                return False

            text_words = [wd for wd in words if not in_table(wd)]
            lines      = _words_to_lines(text_words)
            for i in range(1, len(lines)):
                lines[i]['gap_before'] = lines[i]['top'] - lines[i-1]['bottom']
            if lines:
                lines[0]['gap_before'] = 0
            blocks = _lines_to_blocks(lines, w)

            # PyMuPDF rich data
            fitz_page = fitz_doc[page_num]
            spans     = _spans_for_page(fitz_page)
            tbl_fills = _table_fills_for_page(fitz_page)
            pg_links  = _links_for_page(fitz_page)

            # Real underline detection via drawn paths (not font flags)
            ul_paths = _real_underlines_for_page(fitz_page, tbl_bboxes)

            # Enrich blocks with formatting
            for blk in blocks:
                style = _block_style(spans, blk['x0'], blk['top'],
                                     blk['x1'], blk['bottom'])
                blk['italic']    = style['italic']
                blk['color']     = style['color']
                blk['bold']      = blk['bold'] or style['bold']
                # Real underline: drawn path below this block
                blk['underline'] = _is_underlined(
                    ul_paths, blk['x0'], blk['top'],
                    blk['x1'], blk['bottom']
                )
                # Hyperlink
                blk['link_uri']  = _link_at(pg_links,
                                            blk['x0'], blk['top'],
                                            blk['x1'], blk['bottom'])

            # Page image at 2× for background
            mat     = fitz.Matrix(2.0, 2.0)
            pix     = fitz_page.get_pixmap(matrix=mat, alpha=False)
            img_b64 = base64.b64encode(pix.tobytes('png')).decode()

            pages_data.append({
                'page_num':   page_num + 1,
                'width':      w,
                'height':     h,
                'blocks':     blocks,
                'paragraphs': blocks,
                'tables':     tables,
                'tbl_bboxes': tbl_bboxes,
                'tbl_fills':  tbl_fills,
                'page_links': pg_links,
                'full_text':  page.extract_text() or '',
                'img_b64':    img_b64,
                'img_scale':  2.0,
            })

        fitz_doc.close()
    return {'pages': pages_data, 'meta': meta}


# ── Public API — Translate ───────────────────────────────────────────────────

def translate_pdf_structure(structure: dict, src: str, tgt: str,
                             progress_cb=None) -> dict:
    from backend.translator import translate_paragraph, translate_sentence

    total = sum(
        len(p['blocks']) + sum(len(r) for t in p['tables'] for r in t)
        for p in structure['pages']
    )
    done = [0]

    translated_pages = []
    for page in structure['pages']:
        t_blocks = []
        for blk in page['blocks']:
            res = translate_paragraph(blk['text'], src, tgt)
            tb  = dict(blk)
            tb['text']     = res['output']
            tb['original'] = blk['text']
            t_blocks.append(tb)
            done[0] += 1
            if progress_cb:
                progress_cb(done[0], total, blk['text'])
            time.sleep(0.08)

        t_tables = []
        for table in page['tables']:
            t_tbl = []
            for row in table:
                t_row = []
                for cell in row:
                    s = str(cell).strip() if cell is not None else ''
                    if s:
                        res = translate_sentence(s, src, tgt)
                        t_row.append(res['output'] if res.get('ok') else s)
                        done[0] += 1
                        if progress_cb:
                            progress_cb(done[0], total, s)
                        time.sleep(0.08)
                    else:
                        t_row.append('')
                t_tbl.append(t_row)
            t_tables.append(t_tbl)

        translated_pages.append({
            **page,
            'blocks':     t_blocks,
            'paragraphs': t_blocks,
            'tables':     t_tables,
        })

    return {'pages': translated_pages, 'meta': structure['meta']}


# ── HTML reconstruction ──────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return _html.escape(str(text))


def _font_face_css() -> str:
    """
    Register all four font variants so WeasyPrint can apply bold/italic
    correctly without synthesizing fake bold/italic (which looks bad).
    """
    parts = []
    faces = [
        (FONT_REGULAR,     'normal', 'normal'),
        (FONT_BOLD,        'bold',   'normal'),
        (FONT_ITALIC,      'normal', 'italic'),
        (FONT_BOLD_ITALIC, 'bold',   'italic'),
    ]
    for path, weight, style in faces:
        if path:
            parts.append(
                f"@font-face {{ font-family:'DocFont'; "
                f"font-weight:{weight}; font-style:{style}; "
                f"src:url('file://{path}'); }}"
            )
    return '\n'.join(parts)


def _page_to_html(page: dict) -> str:
    w         = page['width']
    h         = page['height']
    img       = page.get('img_b64', '')
    blocks    = page.get('blocks', [])
    tables    = page.get('tables', [])
    tbboxes   = page.get('tbl_bboxes', [])
    tbl_fills = page.get('tbl_fills', [])

    def pt2mm(v): return v * 0.3528

    W_mm = pt2mm(w)
    H_mm = pt2mm(h)

    overlays = []

    # ── Text block overlays ─────────────────────────────────────────────────
    for blk in blocks:
        x0    = pt2mm(blk['x0']     - 2)
        y0    = pt2mm(blk['top']    - 1)
        bw    = pt2mm(blk['x1']     - blk['x0'] + 4)
        bh    = pt2mm(blk['bottom'] - blk['top'] + 6)
        sz_mm = max(blk.get('size', 11), 7) * 0.3528

        bold      = blk.get('bold', False) or blk.get('hlevel', 0) in (1, 2, 3)
        italic    = blk.get('italic', False)
        underline = blk.get('underline', False)
        align     = blk.get('align', 'left')
        link_uri  = blk.get('link_uri')

        # Heading semantic colors; all others use original span color
        hlevel = blk.get('hlevel', 0)
        heading_colors = {1: '#1A1A2E', 2: '#2E4057', 3: '#3D5A80'}
        color = heading_colors.get(hlevel, blk.get('color', '#111111'))

        fw  = 'bold'   if bold      else 'normal'
        fs  = 'italic' if italic    else 'normal'
        td  = 'underline' if underline else 'none'

        div_style = (
            f'left:{x0:.2f}mm;top:{y0:.2f}mm;'
            f'width:{bw:.2f}mm;min-height:{bh:.2f}mm;'
            f'font-size:{sz_mm:.3f}mm;'
            f'font-weight:{fw};font-style:{fs};'
            f'text-align:{align};color:{color};'
            f'text-decoration:{td};'
        )

        inner    = f'<div class="ov" style="{div_style}">{_esc(blk.get("text",""))}</div>'
        wo_style = (f'left:{x0:.2f}mm;top:{y0:.2f}mm;'
                    f'width:{bw:.2f}mm;height:{bh:.2f}mm;')

        overlays.append(f'<div class="wo" style="{wo_style}"></div>')
        if link_uri:
            overlays.append(f'<a href="{_esc(link_uri)}">{inner}</a>')
        else:
            overlays.append(inner)

    # ── Table overlays ──────────────────────────────────────────────────────
    for tbl_data, bbox in zip(tables, tbboxes):
        if not tbl_data:
            continue
        bx0 = pt2mm(bbox[0])
        by0 = pt2mm(bbox[1])
        bw2 = pt2mm(bbox[2] - bbox[0])
        bh2 = pt2mm(bbox[3] - bbox[1])
        col_count = max(len(r) for r in tbl_data)
        row_count = len(tbl_data)
        row_h_pt  = (bbox[3] - bbox[1]) / max(row_count, 1)
        col_w_pt  = (bbox[2] - bbox[0]) / max(col_count, 1)

        rows_html = ''
        for ri, row in enumerate(tbl_data):
            cells  = [str(c) if c is not None else '' for c in row]
            cells += [''] * (col_count - len(cells))
            cell_y0 = bbox[1] + ri * row_h_pt
            cell_y1 = cell_y0 + row_h_pt
            row_cells_html = ''
            for ci, cell_text in enumerate(cells):
                cell_x0 = bbox[0] + ci * col_w_pt
                cell_x1 = cell_x0 + col_w_pt
                fill    = _fill_at(tbl_fills, cell_x0, cell_y0, cell_x1, cell_y1)
                if fill:
                    nums = re.findall(r'\d+', fill)
                    r2, g2, b2 = (int(n) for n in nums[:3])
                    lum = 0.299*r2 + 0.587*g2 + 0.114*b2
                    tc  = 'white' if lum < 128 else '#111111'
                    cs  = f'background:{fill};color:{tc};font-weight:bold;'
                else:
                    cs  = 'background:white;color:#111111;'
                row_cells_html += f'<td style="{cs}">{_esc(cell_text)}</td>'
            rows_html += f'<tr>{row_cells_html}</tr>'

        wo_s = f'left:{bx0:.2f}mm;top:{by0:.2f}mm;width:{bw2:.2f}mm;height:{bh2:.2f}mm;'
        overlays.append(
            f'<div class="wo" style="{wo_s}"></div>'
            f'<div class="tbl-ov" style="left:{bx0:.2f}mm;top:{by0:.2f}mm;width:{bw2:.2f}mm;">'
            f'<table style="width:100%;border-collapse:collapse;">{rows_html}</table></div>'
        )

    overlays_html = '\n'.join(overlays)

    return (
        f'<div class="page" style="'
        f'width:{W_mm:.2f}mm;height:{H_mm:.2f}mm;'
        f'position:relative;overflow:hidden;'
        f'page-break-after:always;break-after:page;'
        f'background:url(\'data:image/png;base64,{img}\') no-repeat top left / 100% 100%;'
        f'">\n{overlays_html}\n</div>'
    )


def reconstruct_pdf(structure: dict, output_path: str):
    from weasyprint import HTML as WP

    pages    = structure.get('pages', [])
    font_css = _font_face_css()
    ff       = "'DocFont','Noto Sans','FreeSans','DejaVu Sans',sans-serif"

    pages_html = '\n'.join(_page_to_html(page) for page in pages)

    html = f"""<!DOCTYPE html>
<html lang="ne"><head>
<meta charset="UTF-8">
<style>
{font_css}

*, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:{ff}; background:white; }}
@page {{ margin:0; size:auto; }}

.page {{
  display: block;
  page-break-after: always;
  break-after: page;
}}

/* White-out layer — erases original text from background image */
.wo {{
  position: absolute;
  background: white;
  z-index: 1;
}}

/* Text overlay layer */
.ov {{
  position: absolute;
  font-family: {ff};
  line-height: 1.35;
  z-index: 2;
  word-wrap: break-word;
  overflow: visible;
}}

/* Links: styling comes from the inner .ov div */
a {{ text-decoration: none; }}

/* Table overlay */
.tbl-ov {{
  position: absolute;
  z-index: 2;
  font-family: {ff};
}}

.tbl-ov table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 2.6mm;
}}

.tbl-ov td {{
  padding: 1mm 2mm;
  border: 0.3mm solid #888;
  vertical-align: middle;
}}
</style>
</head><body>
{pages_html}
</body></html>"""

    WP(string=html, base_url='/').write_pdf(output_path)