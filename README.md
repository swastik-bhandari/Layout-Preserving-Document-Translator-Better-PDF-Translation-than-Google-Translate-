# TMT File Translator v2 — Google Translate Pipeline
**LowResource Labs · Google TMT Hackathon 2026**

> Translate PDF, DOCX, CSV and TSV with full format preservation using the same
> pipeline architecture as Google Translate.

---

## Quick Start

```bash
pip install flask pdfplumber pymupdf weasyprint python-docx requests fonttools uharfbuzz

python app.py
# Open http://localhost:5050
```

---

## How It Works (Google Translate Pipeline)

### PDF — Image Background + Text Overlay
```
Original PDF
    │
    ├─ PyMuPDF renders each page → PNG at 2× resolution
    ├─ pdfplumber extracts text blocks with exact bounding boxes (x,y,size,bold)
    │
    ▼
Translate each block via TMT API
    │
    ▼
WeasyPrint renders output PDF:
    ├─ <div> background = original page PNG (preserves graphics, images, decorations)
    ├─ White-out divs erase original text from background
    └─ Translated text overlaid at exact original coordinates
         └─ Pango → HarfBuzz → OpenType shaping → correct Devanagari glyphs ✓
```

This is **exactly** how Google Translate PDF works — the original page image
acts as a layout-perfect background, and translated text is placed on top.

### DOCX — Run-Level Format Preservation
```
Walk paragraphs → table cells → headers/footers → text boxes
    │
    ▼ For each paragraph:
    ├─ Join all runs → single text string
    ├─ Translate via TMT API (full paragraph = better context)
    └─ Write back: first run gets translated text, rest cleared
         └─ Formatting (bold/italic/size/color/font) preserved via XML clone
```

### CSV/TSV — Cell-by-Cell
```
Auto-detect: encoding (UTF-8/latin-1) + delimiter (,/TAB/;/|)
    │
    ▼ For each cell:
    ├─ Skip: numbers, dates, URLs, empty cells
    ├─ Translate via TMT API
    └─ Write back with same delimiter, UTF-8 output
```

---

## Architecture

```
tmt-translator/
├── app.py                  # Flask server — job queue, upload, download
├── frontend/
│   └── index.html          # Clean Google-style UI (light theme)
└── backend/
    ├── translator.py       # TMT API client — retry, backoff, sentence split
    ├── pdf_processor.py    # PDF: PyMuPDF render + pdfplumber extract + WeasyPrint
    ├── docx_processor.py   # DOCX: python-docx run-level format preservation
    └── csv_processor.py    # CSV/TSV: auto-detect + cell-by-cell translation
```

---

## Key Technical Decisions

| Problem | Solution |
|---------|----------|
| Devanagari rectangles | WeasyPrint → Pango → HarfBuzz (OpenType shaping) |
| PDF layout loss | Original page as PNG background, text overlaid at exact coords |
| DOCX format loss | XML-level rPr clone preserves bold/italic/color/font |
| CSV encoding mess | Try utf-8-sig → utf-8 → latin-1 → cp1252 |
| API rate limits | Exponential backoff with jitter, configurable delay |
| Merged table cells | Track by XML element id (`id(cell._tc)`) |

---

## API

```
POST /translate     — upload + queue job
GET  /status/<id>   — poll progress + logs
GET  /download/<id> — fetch translated file
GET  /health        — service check
```
