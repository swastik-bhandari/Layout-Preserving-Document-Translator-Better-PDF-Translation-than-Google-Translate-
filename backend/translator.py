"""
TMT Translation API Client
==========================
Google Translate-grade reliability:
  - Sentence-aware batching (never splits mid-sentence)
  - Exponential backoff with jitter
  - Per-request timeout + connection pooling
  - Automatic fallback to original on hard failure
  - Devanagari-aware sentence splitting (।)
"""
import re
import time
import random
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session = requests.Session()
_session.mount("https://", HTTPAdapter(
    max_retries=Retry(total=0),
    pool_connections=4,
    pool_maxsize=8,
))

_SENT_SPLIT = re.compile(r'(?<=[.!?।])\s+')


def _jitter(base: float) -> float:
    return base * (0.8 + random.random() * 0.4)


def translate_sentence(text: str, src: str, tgt: str,
                        retries: int = 4, base_delay: float = 0.6) -> dict:
    text = text.strip()
    if not text:
        return {"ok": True, "output": ""}

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    payload = {"text": text, "src_lang": src, "tgt_lang": tgt}
    last_err = "unknown"

    for attempt in range(retries):
        try:
            resp = _session.post(API_URL, json=payload, headers=headers, timeout=25)
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                last_err = f"Non-JSON {resp.status_code}: {resp.text[:120]}"
                if attempt < retries - 1:
                    time.sleep(_jitter(base_delay * (2 ** attempt)))
                continue
            data = resp.json()
            if data.get("message_type") == "SUCCESS":
                out = data.get("output", "").strip()
                return {"ok": True, "output": out or text}
            last_err = data.get("message", "API error")
            if resp.status_code in (401, 403):
                break
            if attempt < retries - 1:
                time.sleep(_jitter(base_delay * (2 ** attempt)))
        except requests.exceptions.Timeout:
            last_err = "timeout"
            if attempt < retries - 1:
                time.sleep(_jitter(base_delay * (2 ** attempt)))
        except requests.exceptions.ConnectionError:
            return {"ok": False, "output": text, "error": "cannot reach TMT API"}
        except Exception as exc:
            last_err = str(exc)
            if attempt < retries - 1:
                time.sleep(_jitter(base_delay))

    return {"ok": False, "output": text, "error": last_err}


def split_sentences(text: str) -> list:
    if not text or not text.strip():
        return [text] if text else []
    parts = _SENT_SPLIT.split(text.strip())
    return [p.strip() for p in parts if p.strip()] or [text.strip()]


def translate_paragraph(text: str, src: str, tgt: str,
                         rate_delay: float = 0.12) -> dict:
    if not text or not text.strip():
        return {"ok": True, "output": text,
                "stats": {"total": 0, "translated": 0, "errors": 0}}
    sentences = split_sentences(text)
    translated, errors = [], 0
    for i, sent in enumerate(sentences):
        res = translate_sentence(sent, src, tgt)
        translated.append(res["output"])
        if not res["ok"]:
            errors += 1
        if i < len(sentences) - 1:
            time.sleep(rate_delay)
    return {
        "ok":     errors == 0,
        "output": " ".join(translated),
        "stats":  {"total": len(sentences),
                   "translated": len(sentences) - errors,
                   "errors": errors},
    }
