"""FastAPI app — endpoints listed in the README.

In-memory caches with TTLs avoid hammering the Gist API. The verify
page is served as a static file and never trusts this service for
the cryptographic decision; it recomputes the proof against raw
GitHub data via Web Crypto.
"""

from __future__ import annotations

import hmac
import json
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from attestation import __version__, config, publisher, badges

settings = config.load()

# Static assets (verify.html, css, js, fonts).
HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE.parent.parent / "static"

app = FastAPI(
    title="Symmetism Attestation Service",
    version=__version__,
    docs_url=None,                # public service: keep /docs off
    redoc_url=None,
    openapi_url=None,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Caches (very small, in-process)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: int, compute):
    now = time.monotonic()
    item = _cache.get(key)
    if item and (now - item[0]) < ttl:
        return item[1]
    value = compute()
    _cache[key] = (now, value)
    return value


# ---------------------------------------------------------------------------
# Health + manifest
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.get("/__manifest")
def manifest_endpoint(request: Request):
    """Server-side trinity leg. Auth via X-Symverify-Token."""
    if not settings.symverify_token:
        raise HTTPException(503, "manifest endpoint disabled (SYMVERIFY_TOKEN unset)")
    provided = request.headers.get("X-Symverify-Token", "")
    if not hmac.compare_digest(provided, settings.symverify_token):
        raise HTTPException(401, "unauthorized")
    # Compute deterministic manifest of the deployed app at startup.
    if "manifest" not in _cache:
        _cache["manifest"] = (time.monotonic(), _compute_self_manifest())
    return _cache["manifest"][1]


def _compute_self_manifest() -> dict:
    import hashlib
    import os
    import unicodedata
    from datetime import datetime, timezone

    root = HERE.parent.parent  # /app at runtime
    entries = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = unicodedata.normalize("NFC", str(p.relative_to(root)).replace("\\", "/"))
        if any(part in {"__pycache__", ".pytest_cache"} or part.startswith(".git")
               for part in rel.split("/")):
            continue
        data = p.read_bytes()
        entries.append({
            "path": rel,
            "mode": 0o100755 if p.stat().st_mode & 0o111 else 0o100644,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    entries.sort(key=lambda e: e["path"].encode("utf-8"))
    canonical = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "spec": "symverify-fingerprint/1",
        "manifest_root_hash": hashlib.sha256(canonical).hexdigest(),
        "file_count": len(entries),
        "commit_sha": os.environ.get("IMAGE_GIT_SHA", "unknown"),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": __version__,
    }


# ---------------------------------------------------------------------------
# Public attestation read endpoints
# ---------------------------------------------------------------------------


def _fetch_latest():
    if not settings.gist_id or not settings.gist_token:
        return None
    try:
        return publisher.latest(
            settings.gist_id, settings.gist_token, settings.gist_filename
        )
    except publisher.PublisherError:
        return None


def _fetch_all(limit: int = 1000):
    if not settings.gist_id or not settings.gist_token:
        return []
    try:
        return publisher.fetch_attestations(
            settings.gist_id, settings.gist_token, settings.gist_filename
        )[:limit]
    except publisher.PublisherError:
        return []


@app.get("/api/fingerprint/latest.json")
def latest_attestation():
    item = _cached("latest", settings.cache_ttl, _fetch_latest)
    if item is None:
        return JSONResponse({"system_fold": None, "verified_at": None}, status_code=204)
    return Response(
        content=json.dumps(item, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Cache-Control": f"public, max-age={settings.cache_ttl}"},
    )


@app.get("/api/timeline.json")
def timeline_json():
    arr = _cached("timeline", settings.timeline_cache_ttl, lambda: _fetch_all(limit=180))
    return Response(
        content=json.dumps(arr, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": f"public, max-age={settings.timeline_cache_ttl}"},
    )


# ---------------------------------------------------------------------------
# SVG badges
# ---------------------------------------------------------------------------


def _badge_response(svg: str) -> Response:
    # Explicit utf-8 encode so Content-Length matches the body byte
    # length exactly (avoids starlette's str-len vs byte-len mismatch).
    body = svg.encode("utf-8")
    return Response(
        content=body,
        media_type="image/svg+xml",
        headers={"Cache-Control": f"public, max-age={settings.cache_ttl}"},
    )


@app.get("/api/fingerprint/badge.svg")
def fingerprint_badge():
    item = _cached("latest", settings.cache_ttl, _fetch_latest)
    fold = (item or {}).get("system_fold") or "SYM-NONE-NONE-NONE-NONE"
    return _badge_response(badges.fingerprint_badge(fold))


@app.get("/api/fingerprint/timestamp.svg")
def timestamp_badge():
    item = _cached("latest", settings.cache_ttl, _fetch_latest)
    ts = (item or {}).get("verified_at") or (item or {}).get("received_at") or ""
    return _badge_response(badges.timestamp_badge(ts))


# ---------------------------------------------------------------------------
# Publish (write)
# ---------------------------------------------------------------------------


@app.post("/api/publish")
async def publish_attestation(
    request: Request,
    x_attestation_token: str | None = Header(default=None, alias="X-Attestation-Token"),
):
    if not settings.publish_token:
        raise HTTPException(503, "publish disabled (ATTESTATION_PUBLISH_TOKEN unset)")
    if not x_attestation_token or not hmac.compare_digest(
        x_attestation_token, settings.publish_token
    ):
        raise HTTPException(401, "unauthorized")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "body must be valid JSON")
    if not isinstance(body, dict) or "system_fold" not in body:
        raise HTTPException(422, "body must be a dict with at least 'system_fold'")
    try:
        published = publisher.publish(
            settings.gist_id,
            settings.gist_token,
            settings.gist_filename,
            body,
        )
    except publisher.PublisherError as e:
        raise HTTPException(502, f"publish failed: {e}") from e
    # Bust caches so subsequent reads see the new attestation.
    for k in ("latest", "timeline"):
        _cache.pop(k, None)
    return {"ok": True, "published": published}


# ---------------------------------------------------------------------------
# Verify page (HTML) — served at both / and /verify so visitors can land
# on the nice index without typing /verify explicitly.
# ---------------------------------------------------------------------------


def _serve_verify_html():
    p = STATIC_DIR / "verify.html"
    if not p.is_file():
        raise HTTPException(503, "verify page not built (static/verify.html missing)")
    return FileResponse(p)


@app.get("/")
def root_page():
    return _serve_verify_html()


@app.get("/verify")
def verify_page():
    return _serve_verify_html()
