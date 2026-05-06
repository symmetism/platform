"""Gist-publishing helper.

Append a new attestation to the Gist's JSON array (newest-first), via
GitHub's `PATCH /gists/{id}` API. Auth: the gist-scoped PAT in
`ATTESTATION_GIST_TOKEN`.

The gist content is a single file (`symmetism-attestations.json`)
containing a JSON array. Each attestation is a self-contained object
the verify page can re-derive without trusting the service.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

GIST_API = "https://api.github.com/gists"


class PublisherError(RuntimeError):
    """Raised when the Gist API call fails."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_attestations(
    gist_id: str, gist_token: str, filename: str, *, timeout: float = 10.0
) -> list[dict]:
    """Read the current attestation list from the Gist."""
    headers = {
        "Authorization": f"token {gist_token}",
        "Accept": "application/vnd.github+json",
    }
    resp = httpx.get(f"{GIST_API}/{gist_id}", headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise PublisherError(f"GET gist failed: {resp.status_code} {resp.text[:200]}")
    # Force UTF-8 decode of the response body. JSON over HTTP must be UTF-8
    # per RFC 8259 §8.1, but httpx falls back to ISO-8859-1 when the
    # Content-Type header has no explicit charset (which GitHub's gist API
    # sometimes does). Decoding raw bytes via json.loads is unambiguous.
    payload = json.loads(resp.content)
    files = payload.get("files", {})
    if filename not in files:
        return []
    raw = files[filename].get("content", "[]") or "[]"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PublisherError(f"gist content not valid JSON: {e}") from e
    return data if isinstance(data, list) else []


def publish(
    gist_id: str,
    gist_token: str,
    filename: str,
    new_attestation: dict[str, Any],
    *,
    max_keep: int = 1000,
    timeout: float = 15.0,
) -> dict:
    """Prepend `new_attestation` to the gist's JSON array, PATCH it.

    Returns the updated attestation dict (with `received_at` set if
    not already present). Caps total length at `max_keep` (oldest
    truncated) so the gist doesn't grow unbounded.
    """
    if "received_at" not in new_attestation:
        new_attestation = {**new_attestation, "received_at": _utc_now_iso()}

    current = fetch_attestations(gist_id, gist_token, filename, timeout=timeout)
    updated = [new_attestation] + current
    if len(updated) > max_keep:
        updated = updated[:max_keep]

    body = {
        "files": {
            filename: {
                "content": json.dumps(updated, indent=2, ensure_ascii=False),
            }
        }
    }
    headers = {
        "Authorization": f"token {gist_token}",
        "Accept": "application/vnd.github+json",
    }
    resp = httpx.patch(
        f"{GIST_API}/{gist_id}", headers=headers, json=body, timeout=timeout
    )
    if resp.status_code != 200:
        raise PublisherError(
            f"PATCH gist failed: {resp.status_code} {resp.text[:200]}"
        )
    return new_attestation


def latest(
    gist_id: str, gist_token: str, filename: str, *, timeout: float = 10.0
) -> dict | None:
    """Return the most recent attestation, or None if the gist is empty."""
    arr = fetch_attestations(gist_id, gist_token, filename, timeout=timeout)
    return arr[0] if arr else None
