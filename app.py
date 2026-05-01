"""
TMT File Translator — Flask Backend
Google Translate-grade pipeline
Routes: POST /translate  GET /status/<id>  GET /download/<id>  GET /
"""
import os
import sys
import uuid
import threading
import time
import argparse
import tempfile
from pathlib import Path
from flask import Flask, request, jsonify, send_file

sys.path.insert(0, str(Path(__file__).parent))

# Load environment from .env when available (optional dependency)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

_TMP       = Path(tempfile.gettempdir())
UPLOAD_DIR = _TMP / "tmt_uploads"
OUTPUT_DIR = _TMP / "tmt_outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

jobs = {}


def run_translation(job_id, input_path, output_path, ext, src, tgt,
                    orig_name, opts):
    job = jobs[job_id]
    job["status"] = "running"

    def progress_cb(done, total, text=""):
        job["progress"] = done
        job["total"]    = max(job.get("total", 1), total)
        snippet = str(text)[:60] + ("…" if len(str(text)) > 60 else "")
        job["log"].append({"type": "ok", "msg": f"[{done}/{total}] {snippet}"})

    try:
        if ext == "pdf":
            from backend.pdf_processor import (
                extract_pdf_structure, translate_pdf_structure, reconstruct_pdf
            )
            job["log"].append({"type": "info", "msg": "📄 Extracting PDF layout…"})
            structure = extract_pdf_structure(input_path)
            npages  = len(structure["pages"])
            nblocks = sum(len(p["blocks"]) for p in structure["pages"])
            job["total"] = nblocks
            job["log"].append({"type": "info",
                "msg": f"Found {nblocks} text blocks across {npages} page(s)"})
            job["log"].append({"type": "info", "msg": "🌐 Translating blocks…"})
            translated = translate_pdf_structure(structure, src, tgt, progress_cb)
            job["log"].append({"type": "info", "msg": "🖨 Reconstructing PDF…"})
            reconstruct_pdf(translated, output_path)

        elif ext == "docx":
            from backend.docx_processor import translate_docx
            job["log"].append({"type": "info", "msg": "📝 Processing DOCX…"})
            result = translate_docx(input_path, output_path, src, tgt, progress_cb)
            job["log"].append({"type": "info",
                "msg": f"Translated {result['done']} segments"})

        elif ext in ("csv", "tsv"):
            from backend.csv_processor import translate_csv
            job["log"].append({"type": "info",
                "msg": f"📊 Processing {ext.upper()}…"})
            result = translate_csv(
                input_path, output_path, src, tgt,
                has_header        = opts.get("has_header", True),
                translate_header  = opts.get("translate_header", True),
                skip_numeric      = opts.get("skip_numeric", True),
                progress_cb       = progress_cb,
                rate_delay        = opts.get("rate_delay", 0.10),
            )
            job["log"].append({"type": "info",
                "msg": f"Translated {result['translated']} cells "
                       f"({result['errors']} errors)"})

        job["status"]      = "done"
        job["output_path"] = output_path
        job["output_name"] = (
            orig_name.rsplit(".", 1)[0] + f"_translated_{tgt}." + ext
        )
        job["log"].append({"type": "ok", "msg": "✅ Translation complete!"})

    except Exception as e:
        import traceback
        job["status"] = "error"
        job["error"]  = str(e)
        job["log"].append({"type": "err", "msg": f"Error: {e}"})
        traceback.print_exc()


@app.route("/")
def index():
    html_path = Path(__file__).parent / "frontend" / "index.html"
    with open(html_path, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/translate", methods=["POST"])
def translate_file():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    src  = request.form.get("src_lang", "en")
    tgt  = request.form.get("tgt_lang", "ne")

    if src == tgt:
        return jsonify({"error": "Source and target languages must differ"}), 400

    filename = file.filename or "document"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("pdf", "docx", "csv", "tsv"):
        return jsonify({"error": f"Unsupported file type: .{ext}"}), 400

    job_id      = str(uuid.uuid4())[:8]
    input_path  = str(UPLOAD_DIR / f"{job_id}_input.{ext}")
    output_path = str(OUTPUT_DIR / f"{job_id}_output.{ext}")
    file.save(input_path)

    opts = {
        "has_header":       request.form.get("has_header", "1") == "1",
        "translate_header": request.form.get("translate_header", "1") == "1",
        "skip_numeric":     request.form.get("skip_numeric", "1") == "1",
        "rate_delay":       int(request.form.get("delay", "120")) / 1000.0,
    }

    jobs[job_id] = {
        "status":      "queued",
        "progress":    0,
        "total":       1,
        "log":         [],
        "output_path": None,
        "output_name": None,
        "error":       None,
        "ext":         ext,
        "started_at":  time.time(),
    }

    threading.Thread(
        target=run_translation,
        args=(job_id, input_path, output_path, ext, src, tgt, filename, opts),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/status/<job_id>")
def get_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "job_id":      job_id,
        "status":      job["status"],
        "progress":    job["progress"],
        "total":       job["total"],
        "log":         job["log"][-40:],
        "error":       job["error"],
        "output_name": job.get("output_name"),
    })


@app.route("/download/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Job not ready"}), 404
    op = job["output_path"]
    if not op or not os.path.exists(op):
        return jsonify({"error": "Output file missing"}), 500
    mime = {
        "pdf":  "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "csv":  "text/csv",
        "tsv":  "text/tab-separated-values",
    }.get(job["ext"], "application/octet-stream")
    return send_file(op, mimetype=mime, as_attachment=True,
                     download_name=job["output_name"])


@app.route("/health")
def health():
    return jsonify({"ok": True, "version": "2.0.0-gt",
                    "upload_dir": str(UPLOAD_DIR)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TMT File Translator")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    print("=" * 55)
    print("  TMT File Translator v2 — Google Translate Pipeline")
    print("  LowResource Labs · Google TMT Hackathon 2026")
    print("=" * 55)
    print(f"  URL : http://localhost:{args.port}")
    print(f"  Dirs: {UPLOAD_DIR}")
    print("=" * 55)

    app.run(host=args.host, port=args.port, debug=False)
