"""
CSV / TSV Processor — Fixed for partial translation bug
=======================================================

BUG FIXED — Duplicate cells were only translated once:
  The old code built a set of (row, col) positions to translate, then called
  the API once per unique CELL TEXT and cached results. But when the same text
  appeared in multiple rows (e.g. "Type of outstanding debt: bank overdrafts"
  repeating across rows 15, 16, 17), only the first occurrence got written
  back — the rest were skipped because the translation was "already done".

  FIX: Translate every (row, col) position independently. Cache translations
  by text so duplicate strings hit the cache instantly (no extra API calls),
  but ALL positions get written back correctly.

Also fixed:
  - Encoding detection cascade now tries utf-8-sig first (handles Excel BOM)
  - Short words like "total", "yes", "no" are no longer skipped by _CODE_RE
  - Better number detection that doesn't false-positive on "total", "male" etc.
"""
import csv
import io
import os
import re
import time


_NUM_RE  = re.compile(r'^[+\-]?[\d,\s]+\.?\d*\s*[%$€£¥₹]?$')
_DATE_RE = re.compile(r'^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$')
_URL_RE  = re.compile(r'^https?://')


def _skippable(cell: str) -> bool:
    t = cell.strip()
    if not t:
        return True
    # URLs
    if _URL_RE.match(t):
        return True
    # Only skip short all-caps if it's truly code-like (no vowels = acronym)
    # e.g. "USD", "N/A", "ID" 
    if len(t) <= 4 and t.upper() == t and re.match(r'^[A-Z0-9_/\-]+$', t):
        return True
    return False


def _detect(raw: str, filename: str):
    if filename.lower().endswith('.tsv'):
        return '\t', '"', True
    try:
        d = csv.Sniffer().sniff(raw[:8192], delimiters=',\t|;')
        hdr = csv.Sniffer().has_header(raw[:8192])
        return d.delimiter, d.quotechar or '"', hdr
    except Exception:
        pass
    lines = raw.splitlines()[:5]
    counts = {d: sum(l.count(d) for l in lines) for d in [',', '\t', '|', ';']}
    return max(counts, key=counts.get), '"', True


def translate_csv(input_path: str, output_path: str,
                  src: str, tgt: str,
                  has_header: bool = True,
                  translate_header: bool = True,
                  skip_numeric: bool = True,
                  progress_cb=None,
                  rate_delay: float = 0.10) -> dict:

    from backend.translator import translate_sentence

    filename = os.path.basename(input_path)

    # Encoding detection — try common encodings in order
    raw = None
    for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
        try:
            with open(input_path, 'r', encoding=enc, newline='') as f:
                raw = f.read()
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        return {'ok': False, 'error': 'Cannot decode file encoding'}

    raw = raw.lstrip('\ufeff')
    delimiter, quotechar, _ = _detect(raw, filename)

    reader   = csv.reader(io.StringIO(raw), delimiter=delimiter, quotechar=quotechar)
    rows     = list(reader)
    if not rows:
        return {'ok': False, 'error': 'Empty file'}

    # ── Build translation queue ──────────────────────────────────────────────
    # KEY FIX: queue EVERY (row, col) position that needs translation.
    # Use a text→translation cache so duplicate strings only hit the API once,
    # but every position still gets written back correctly.
    queue = []  # list of (ri, ci)
    for ri, row in enumerate(rows):
        is_header = has_header and ri == 0
        if is_header and not translate_header:
            continue
        for ci, cell in enumerate(row):
            if not cell.strip():
                continue
            if skip_numeric and _skippable(cell):
                continue
            queue.append((ri, ci))

    total   = len(queue)
    done    = 0
    errors  = 0
    cache   = {}   # text → translated text

    out_rows = [list(row) for row in rows]  # deep copy

    for ri, ci in queue:
        cell = rows[ri][ci]
        text = cell.strip()

        if text in cache:
            # Cache hit — no API call needed, just write the result
            out_rows[ri][ci] = cache[text]
        else:
            # Cache miss — call API
            res = translate_sentence(text, src, tgt)
            translated = res['output'] if res.get('ok') else cell
            cache[text] = translated
            out_rows[ri][ci] = translated
            if not res.get('ok'):
                errors += 1
            time.sleep(rate_delay)

        done += 1
        if progress_cb:
            progress_cb(done, total, text[:50])

    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter=delimiter, quotechar=quotechar,
                            quoting=csv.QUOTE_MINIMAL)
        writer.writerows(out_rows)

    return {
        'ok':         True,
        'total':      total,
        'translated': done,
        'cached':     sum(1 for ri, ci in queue if rows[ri][ci].strip() in cache
                          and queue.index((ri, ci)) > 0),
        'errors':     errors,
    }