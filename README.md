# Layout-Preserving Document Translator
### Better PDF Translation than Google Translate

> A structure-preserving multilingual document translator for PDF, DOCX, and CSV/TSV.
> Unlike Google Translate, which breaks formatting in Nepali PDFs, this system preserves layout, tables, bold/italic styling, colors, and hyperlinks, delivering high-fidelity document translation instead of plain text output.
> Supports PDF, DOCX, CSV/TSV with full structure, table, and formatting preservation, especially strong on PDFs where most tools fail.

**Powered by** [TMT API](https://tmt.ilprl.ku.edu.np) · LowResource Labs · Google TMT Hackathon 2026

---

## The Problem Google Translate Doesn't Solve

When you upload a PDF to Google Translate for English↔Nepali translation, the translated output **loses most formatting**. Bold headings become plain text. Italicised passages look identical to body text. Suffers from syllabic splitting.

This isn't a minor cosmetic issue. In medical reports, legal documents, academic papers, and government forms — formatting *is* meaning. A bold warning, an italic definition, an underlined clause — stripping these changes how the document reads.

**This project fixes that.**

---

## What We Preserve That Google Translate Does Not

| Formatting Feature | Google Translate (EN↔NE PDF) | This Project |
|---|---|---|
| **Bold text** | ✗ Lost | ✓ Preserved |
| **Italic text** | ✗ Lost | ✓ Preserved |
|**Syllabic Splitting issue** | ✗ Lost | ✓ Preserved |
| **Underline** | ✓ Preserved | ✓ Preserved |
| **Text color** | ✓ Preserved | ✓ Preserved |
| **Hyperlink** | ✓ Preserved | ✓ Preserved |
| **Images** | ✓ Preserved | ✓ Preserved |
| **Devanagari shaping (no boxes)** | ✓ | ✓ |
| **DOCX bold/italic/color/font** | ✓ | ✓ |
| **DOCX table structure** | ✓ | ✓ |
---
Our application accepts CSV/TSV and translates accurately, preserving structure. Google Translate doesn't accept CSV/TSV.

## Quick Start

```bash
# System dependencies — WeasyPrint needs Pango for Devanagari shaping
sudo apt update
sudo apt install -y \
  libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
  libcairo2 libgdk-pixbuf2.0-0 \
  fonts-freefont-ttf fonts-noto fonts-noto-core \
  shared-mime-info

# Python packages
pip install flask pdfplumber pymupdf weasyprint python-docx \
            requests uharfbuzz fonttools pillow
```

### Run

```bash
git clone git@github.com:swastik-bhandari/Layout-Preserving-Document-Translator-Better-PDF-Translation-than-Google-Translate-.git
python app.py --port 5050
```

Open **http://localhost:5050** in your Windows browser (WSL auto-forwards ports).

> **Windows note:** Run inside WSL Ubuntu. WeasyPrint requires GTK3/Pango which installs natively on Linux but requires complex setup on bare Windows.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser UI                               │
│          Drag & drop · Language selector · Live progress        │
└──────────────────────────┬──────────────────────────────────────┘
                           │ POST /translate  (multipart/form-data)
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      app.py  (Flask)                            │
│  • Job queue (threading)    • GET /status/<id>  (live log)      │
│  • 50 MB upload limit       • GET /download/<id>                │
└──────┬──────────────┬──────────────┬───────────────────────────┘
       │ .pdf         │ .docx        │ .csv / .tsv
       ▼              ▼              ▼
┌────────────┐  ┌────────────┐  ┌────────────┐
│pdf_        │  │docx_       │  │csv_        │
│processor   │  │processor   │  │processor   │
│.py         │  │.py         │  │.py         │
└─────┬──────┘  └─────┬──────┘  └─────┬──────┘
      │               │               │
      └───────────────┴───────────────┘
                      │
                      ▼
          ┌───────────────────────┐
          │   translator.py       │
          │  TMT API client       │
          │  Retry · Backoff      │
          │  Sentence splitting   │
          └───────────────────────┘
                      │
                      ▼
          https://tmt.ilprl.ku.edu.np
              /lang-translate
```

---

## PDF Pipeline — How It Works

This is the most technically significant part of the project. The pipeline has three distinct phases:

### Phase 1 — Extraction (Dual-Library Strategy)

We use **two libraries simultaneously** because each does something the other cannot:

```
Original PDF
    │
    ├─► pdfplumber.extract_words()
    │     Word-level bounding boxes (x0, y0, x1, y1)
    │     Used for: layout clustering, heading detection, table detection
    │     Cannot do: underline, italic, color, link flags
    │
    └─► PyMuPDF  get_text('dict')
          Per-span OpenType flags:
            bit 4 → bold
            bit 1 → italic
            bit 2 → underline
            int   → color as 0xRRGGBB
          page.get_links()  → URI + rect per annotation
          get_drawings()    → fill rectangles (table cell backgrounds)
          get_pixmap()      → full page PNG at 2× resolution
```

**pdfplumber** gives us the layout structure (where things are, how big, how indented). **PyMuPDF** gives us the rich formatting metadata (how things look). They are merged by bounding-box overlap: for each text block found by pdfplumber, we look up all PyMuPDF spans that fall inside that block's rectangle and union their formatting flags.

```python
# From pdf_processor.py — _block_style()
def _block_style(spans, x0, y0, x1, y1, tol=4) -> dict:
    matching = []
    for sp in spans:
        if (sp['x0'] - tol < x1 and sp['x1'] + tol > x0 and
                sp['y0'] - tol < y1 and sp['y1'] + tol > y0):
            matching.append(sp)

    underline = any(s['underline'] for s in matching)
    italic    = any(s['italic']    for s in matching)
    bold      = any(s['bold']      for s in matching)
    # Prefer the first span with a distinctive (non-black) color
    # e.g. hyperlink teal #467886 — black is valid, not a fallback
    color = matching[0]['color']
    for s in matching:
        if s['color'] not in ('#000000', '#111111'):
            color = s['color']
            break
    return {'underline': underline, 'italic': italic, 'bold': bold, 'color': color}
```

### Phase 2 — Translation

Each extracted text block is sent to the TMT API individually. The translator uses:

- **Sentence-aware splitting** on `.!?` and Devanagari danda `।` so long paragraphs are split into meaningful units before API calls
- **Exponential backoff with ±20% jitter** on failures (avoids thundering-herd on retry)
- **Connection pooling** via `requests.Session` + `HTTPAdapter` (4 persistent connections)
- **Graceful fallback** — if a block fails after 4 retries, the original text is kept so the document is never partially broken

### Phase 3 — Reconstruction (The Key Innovation)

**This is where we beat Google Translate.**

We use the same high-level approach Google uses (original page as background image + translated text overlaid), but we add the formatting layer Google omits for Nepali:

```
For each page:
  1. PyMuPDF renders page → PNG at 2× resolution (crisp, captures all graphics)
  2. White <div>s are placed over original text areas (erase original)
  3. Translated text <div>s are placed at original coordinates with:
       font-weight: bold/normal          ← from PyMuPDF span flag bit 4
       font-style:  italic/normal        ← from PyMuPDF span flag bit 1
       text-decoration: underline/none   ← from PyMuPDF span flag bit 2
       color: #rrggbb                    ← from PyMuPDF span color integer
  4. If a block has a link annotation → wrapped in <a href="...">
  5. Table cells → background color from actual page drawings (not hardcoded)
  6. WeasyPrint renders the HTML → PDF
       └─► Pango → HarfBuzz → OpenType shaping → correct Devanagari glyphs
```

```python
# From pdf_processor.py — per-block style application
div_style = (
    f'left:{x0:.2f}mm;top:{y0:.2f}mm;'
    f'width:{bw:.2f}mm;min-height:{bh:.2f}mm;'
    f'font-size:{sz_mm:.3f}mm;'
    f'font-weight:{css_font_weight};'    # bold or normal
    f'font-style:{css_font_style};'      # italic or normal
    f'text-align:{align};'
    f'color:{color};'                    # exact hex from original span
    f'text-decoration:{css_decoration};' # underline or none
)
# Hyperlinks get an actual <a href> wrapper
if link_uri:
    overlays.append(f'<a href="{_esc(link_uri)}">{inner}</a>')
```


---

## DOCX Pipeline

DOCX files are XML archives. The translator walks the document tree at the XML level, which gives complete control over formatting:

### The Three Bugs We Solved

**1. Phantom merged cells (python-docx bug)**

`python-docx`'s `row.cells` uses `vMerge` to repeat vertically-merged cells across rows. This caused the same `<w:tc>` element to appear multiple times under different row/column indices. Our `seen` set (designed to skip true merged cells) was then incorrectly skipping unmerged cells that happened to share an element ID.

Fix: iterate raw `<w:tc>` XML elements directly per row:

```python
# From docx_processor.py — _iter_real_cells()
def _iter_real_cells(table):
    for tr in table._tbl.iterchildren(W('tr')):
        for tc in tr.iterchildren(W('tc')):
            yield tc   # always unique per physical row — no phantoms
```

**2. Hyperlink paragraphs have zero runs**

`para.runs` only sees `<w:r>` elements that are direct children of `<w:p>`. Text inside `<w:hyperlink>` sits one level deeper. Result: all hyperlink text was silently skipped.

Fix: extract text from ALL `<w:t>` descendants using XML iteration:

```python
# From docx_processor.py — _para_text() and _set_para_text()
def _para_text(para) -> str:
    return ''.join(t.text or '' for t in para._p.iter(W('t')))
    # Captures body runs AND hyperlink runs in one pass

def _set_para_text(para, translated: str):
    all_t = list(para._p.iter(W('t')))
    all_t[0].text = translated   # write into first <w:t>
    for t in all_t[1:]:
        t.text = ''              # clear rest — formatting preserved via rPr XML
```

**3. Full paragraph context for translation**

### What Gets Translated
- Body paragraphs (all styles: Normal, Heading 1–9, Title, Subtitle, List)
- Table cells (all rows, all columns, all nested tables)
- Headers and footers (first page, odd page, even page variants)
- Text boxes and drawing shapes with text frames

### What Is Preserved
- Bold, italic, underline, strikethrough — via `<w:rPr>` XML clone
- Font name, font size, font color, highlight color
- All paragraph styles (headings, lists, alignment, indent)
- Table structure: cell widths, borders, shading, merge state
- Embedded images (untouched — they are not text)

---

## CSV/TSV Pipeline

### Encoding & Delimiter Auto-Detection

```python
# From csv_processor.py
for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
    try:
        with open(input_path, 'r', encoding=enc, newline='') as f:
            raw = f.read()
        break
    except UnicodeDecodeError:
        continue
```

`utf-8-sig` is tried first because Excel exports always write a UTF-8 BOM. Delimiter is detected by `csv.Sniffer` with fallback to counting occurrences of `,`, `\t`, `|`, `;`.

```

### Smart Skip Logic

Cells are skipped (not sent to API) if they are: pure numbers, dates (`DD/MM/YYYY`), URLs (`https://...`), or short all-uppercase acronyms (`USD`, `N/A`, `ID`).

---

## Translation API Client

```python
# From translator.py — retry with exponential backoff + jitter
for attempt in range(retries):
    try:
        resp = _session.post(API_URL, json=payload, headers=headers, timeout=25)
        data = resp.json()
        if data.get('message_type') == 'SUCCESS':
            return {'ok': True, 'output': data['output'].strip()}
        if resp.status_code in (401, 403):
            break   # auth error — don't retry
        time.sleep(_jitter(base_delay * (2 ** attempt)))
    except requests.exceptions.Timeout:
        time.sleep(_jitter(base_delay * (2 ** attempt)))
    except requests.exceptions.ConnectionError:
        return {'ok': False, 'output': text, 'error': 'cannot reach TMT API'}

def _jitter(base: float) -> float:
    return base * (0.8 + random.random() * 0.4)   # ±20% randomisation
```

The shared `requests.Session` with `HTTPAdapter(pool_connections=4, pool_maxsize=8)` means TCP connections are reused across all translation calls — no handshake overhead per request, significantly faster for large documents.

---

## Project Structure

```
tmt-translator/
│
├── app.py                      # Flask server — job queue, routes, file handling
│   ├── POST /translate         # Upload file, start background translation job
│   ├── GET  /status/<id>       # Poll progress: done count, total, log tail
│   ├── GET  /download/<id>     # Download completed translated file
│   └── GET  /health            # Service liveness check
│
├── frontend/
│   └── index.html              # Single-file UI — no build step, no dependencies
│                               # Drag-drop · language swap · live log · ETA
│
├── backend/
│   ├── translator.py           # TMT API client (107 lines)
│   │   ├── translate_sentence  # Single segment with retry/backoff
│   │   ├── translate_paragraph # Sentence-split then join
│   │   └── split_sentences     # Regex on .!?। boundaries
│   │
│   ├── pdf_processor.py        # PDF pipeline (644 lines)
│   │   ├── _spans_for_page     # PyMuPDF → per-span bold/italic/underline/color
│   │   ├── _links_for_page     # PyMuPDF → hyperlink URI + rect
│   │   ├── _table_fills_for_page # PyMuPDF drawings → cell background colors
│   │   ├── _block_style        # Merge span flags onto pdfplumber blocks
│   │   ├── extract_pdf_structure  # Full extraction: layout + formatting + image
│   │   ├── translate_pdf_structure # Block-by-block + table cell translation
│   │   ├── _page_to_html       # Build HTML overlay per page
│   │   └── reconstruct_pdf     # WeasyPrint render → output PDF
│   │
│   ├── docx_processor.py       # DOCX pipeline (218 lines)
│   │   ├── _iter_real_cells    # Raw <w:tc> XML iteration (no phantom merges)
│   │   ├── _para_text          # All <w:t> descendants (includes hyperlinks)
│   │   ├── _set_para_text      # Write-back via XML <w:t> nodes
│   │   └── translate_docx      # Body + tables + headers/footers + textboxes
│   │
│   └── csv_processor.py        # CSV/TSV pipeline (155 lines)
│       ├── _detect             # Encoding + delimiter auto-detection
│       ├── _skippable          # Filter numbers, dates, URLs, acronyms
│       └── translate_csv       # Position-keyed queue + text cache
│
├── sample_docs/                # Test documents for all formats and scenarios
├── tests/                      # Layout verification and report generation
├── requirements.txt
└── run.sh
```

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `flask` | ≥3.0 | HTTP server, job routing |
| `pymupdf` (fitz) | ≥1.23 | PDF page render, span flags, link annotations, drawings |
| `pdfplumber` | ≥0.10 | Word-level bbox extraction, table detection |
| `weasyprint` | ≥60 | HTML→PDF with Pango+HarfBuzz (Devanagari shaping) |
| `python-docx` | ≥1.1 | DOCX read/write |
| `requests` | ≥2.31 | TMT API calls with connection pooling |
| `uharfbuzz` | any | HarfBuzz bindings (used by WeasyPrint) |
| `pillow` | ≥10 | Image handling |
| `fonttools` | any | Font introspection |

---

## Supported Languages

The TMT API currently supports:

| Code | Language |
|---|---|
| `en` | English |
| `ne` | Nepali (नेपाली) |
| `tmg` | Tamang |

All three can be used as source or target in the UI.

---


---

*Built for the Google TMT Hackathon 2026 · LowResource Labs*
