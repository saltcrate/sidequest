"""SaltCrate verification + freshness engine — the core asset behind the Track B oracle.

Four primitives, all self-contained and side-effect-light:
  fetch       — clean structured extraction of a URL (boilerplate stripped)
  check_link  — liveness (status, redirect chain, latency)
  check_change— freshness ("did this change since last check", stateful)
  verify      — does a source support a claim (heuristic term-overlap + excerpt)

Honesty note (non-negotiable): verify() is a *heuristic* — term overlap + best
supporting excerpt. It does NOT do true semantic fact-checking (no LLM, no
cross-source corroboration). The tool description must say so plainly.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from urllib.parse import urlparse

import httpx
import trafilatura

USER_AGENT = "SaltCrateOracle/0.1 (+https://saltcrate.com; verification + freshness oracle)"
TIMEOUT = 20

# Minimal English stopword set (lowercase).
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "of", "at",
    "by", "for", "with", "about", "against", "between", "into", "through",
    "during", "before", "after", "above", "below", "to", "from", "up", "down",
    "in", "out", "on", "off", "over", "under", "again", "further", "once",
    "here", "there", "when", "where", "why", "how", "all", "any", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor",
    "not", "only", "own", "same", "so", "than", "too", "very", "can", "will",
    "just", "should", "now", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "having", "do", "does", "did", "doing",
    "it", "its", "this", "that", "these", "those", "i", "you", "he", "she",
    "we", "they", "them", "his", "her", "their", "our", "your", "my", "me",
    "us", "as", "also", "get", "got", "may", "might", "must", "would",
}


def _client(timeout: int = TIMEOUT) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.9"},
    )


def _clean(text: str) -> str:
    """Collapse whitespace; keep it simple and deterministic."""
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()
    return _clean(soup.get_text(" ", strip=True))


def fetch(url: str, timeout: int = TIMEOUT) -> dict:
    """Fetch a URL and return clean extracted text + metadata."""
    if not urlparse(url).scheme:
        return {"url": url, "status": "error", "error": "URL must include scheme (http/https)"}
    try:
        with _client(timeout) as c:
            resp = c.get(url)
        html = resp.text
        ctype = resp.headers.get("content-type", "")
        text = trafilatura.extract(html, include_comments=False, include_tables=True)
        if not text:
            text = _strip_html(html)
        meta = trafilatura.extract_metadata(html)
        return {
            "url": str(resp.url),
            "status": "ok",
            "http_status": resp.status_code,
            "content_type": ctype,
            "chars": len(text),
            "title": meta.title if meta else None,
            "text": _clean(text),
        }
    except httpx.HTTPError as e:
        return {"url": url, "status": "error", "error": f"{type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001 — return structured error, never crash the tool
        return {"url": url, "status": "error", "error": f"{type(e).__name__}: {e}"}


def check_link(url: str, timeout: int = 15) -> dict:
    """Check whether a URL is alive (status, redirect chain, latency)."""
    start = time.time()
    if not urlparse(url).scheme:
        return {"url": url, "alive": False, "error": "URL must include scheme (http/https)"}
    try:
        with _client(timeout) as c:
            resp = c.head(url)
            if resp.status_code in (405, 501, 403):
                resp = c.get(url)
        status = resp.status_code
        final_url = str(resp.url)
        history = [str(h.url) for h in resp.history]
        error = None
    except httpx.HTTPError as e:
        status, final_url, history, error = None, None, [], f"{type(e).__name__}: {e}"
    return {
        "url": url,
        "alive": status is not None and 200 <= status < 400,
        "status": status,
        "final_url": final_url,
        "redirects": history,
        "latency_ms": round((time.time() - start) * 1000),
        "error": error,
    }


def check_change(url: str, timeout: int = TIMEOUT, db_path: str = "state.db") -> dict:
    """Detect whether a URL's content changed since the last check (stateful)."""
    data = fetch(url, timeout)
    if data["status"] != "ok":
        return {"url": url, "status": "error", "error": data.get("error")}
    normalized = _clean(data["text"].lower())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    now = time.time()
    try:
        with sqlite3.connect(db_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pages ("
                "url TEXT PRIMARY KEY, hash TEXT, chars INTEGER, "
                "first_seen REAL, last_seen REAL)"
            )
            row = db.execute(
                "SELECT hash, chars, first_seen, last_seen FROM pages WHERE url=?", (url,)
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO pages(url, hash, chars, first_seen, last_seen) VALUES(?,?,?,?,?)",
                    (url, digest, len(normalized), now, now),
                )
                return {"url": url, "status": "new", "hash": digest, "chars": len(normalized)}
            old_hash, old_chars, first_seen, _ = row
            db.execute(
                "UPDATE pages SET hash=?, chars=?, last_seen=? WHERE url=?",
                (digest, len(normalized), now, url),
            )
    except sqlite3.Error as e:
        return {"url": url, "status": "error", "error": f"sqlite: {e}"}
    return {
        "url": url,
        "status": "changed" if old_hash != digest else "unchanged",
        "hash": digest,
        "previous_hash": old_hash,
        "chars": len(normalized),
        "previous_chars": old_chars,
        "first_seen_epoch": first_seen,
        "last_checked_epoch": now,
    }


def _claim_terms(claim: str) -> list[str]:
    """Extract significant (non-stopword, length>=3) terms from a claim."""
    words = re.findall(r"[a-z0-9]{3,}", claim.lower())
    return [w for w in words if w not in _STOPWORDS]


def _best_excerpt(source_text: str, terms: list[str], width: int = 280) -> str | None:
    """Return the sentence with the highest term overlap."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", source_text) if s.strip()]
    best, best_score = None, -1
    low = source_text.lower()
    for s in sentences:
        sl = s.lower()
        score = sum(1 for t in terms if t in sl)
        if score > best_score:
            best, best_score = s, score
    if best is None or best_score == 0:
        return None
    return best[:width] + ("…" if len(best) > width else "")


def verify(claim: str, source_url: str, timeout: int = TIMEOUT) -> dict:
    """Heuristic: does source_url support the given claim? (term overlap + excerpt)."""
    data = fetch(source_url, timeout)
    if data["status"] != "ok":
        return {"claim": claim, "source_url": source_url, "status": "error", "error": data.get("error")}
    text = data["text"]
    low = text.lower()
    terms = _claim_terms(claim)
    if not terms:
        return {"claim": claim, "source_url": source_url, "status": "error",
                "error": "no meaningful terms extracted from claim"}
    matched = [t for t in terms if t in low]
    ratio = len(matched) / len(terms)
    if ratio >= 0.5:
        verdict = "supported"
    elif ratio >= 0.25:
        verdict = "partial"
    else:
        verdict = "unsupported"
    return {
        "claim": claim,
        "source_url": source_url,
        "status": "ok",
        "verdict": verdict,
        "confidence": round(ratio, 3),
        "matched_terms": matched,
        "total_terms": len(terms),
        "best_supporting_excerpt": _best_excerpt(text, terms),
        "note": "Heuristic term-overlap only — not semantic fact-checking.",
    }
