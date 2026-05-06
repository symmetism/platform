"""OpenAI-backed narrative generation (Phase E3).

Per Stack SoT S3 (operator R14 amendment 2026-05-06):
  Provider:  OpenAI
  Model:     gpt-4o-mini (configurable via env)
  Max tokens: 200
  Temperature: 0.2
  Trigger discipline: only on meaningful events
                      (drift, deploy, lockdown, --explain, daily summary)

Failure mode: API down or budget exceeded → return None and let the
caller persist a `narrative_unavailable` row. Structured fields
(brackets, snapshots, fingerprints) are unaffected.

Secret hygiene (Rule R3): we ALWAYS strip recognized secret patterns
from the snapshot before sending it to the API. Even though the
brackets are derived data and shouldn't contain credentials, defense
in depth.
"""

from __future__ import annotations

import os
import re
from typing import Any

from symverify import config
from symverify.stabilizer import SECRET_PATTERNS

OPENAI_MODEL = os.environ.get("SYMVERIFY_NARRATIVE_MODEL", "gpt-4o-mini")
OPENAI_MAX_TOKENS = 200
OPENAI_TEMPERATURE = 0.2

SYSTEM_PROMPT = (
    "You write short, factual status summaries for an infrastructure "
    "verifier called SymVerify. Audience: a solo operator. "
    "Style: 2-4 sentences, plain English, no marketing voice, no emoji. "
    "State what changed since the last snapshot, what the {Q_A, H_S} "
    "brackets say, and whether human action is needed. "
    "If everything is conserved, say so plainly. If there is drift, "
    "name the affected charge(s) and the descriptor pattern. "
    "Don't restate the data verbatim — interpret it."
)


class NarrativeError(RuntimeError):
    """Raised when narrative generation fails irrecoverably (caller decides)."""


def _strip_secrets(s: str) -> str:
    """Replace any recognized secret pattern with `<REDACTED>` before sending."""
    out = s
    for pat in SECRET_PATTERNS:
        try:
            out = pat.sub(b"<REDACTED>", out.encode("utf-8")).decode("utf-8", "replace")
        except re.error:
            pass
    return out


def _read_api_key() -> str | None:
    """Read OpenAI API key from `~/.symmetism/secrets/openai.key` or env."""
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key.strip()
    p = config.secrets_dir() / "openai.key"
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return None


def _build_user_prompt(snapshot: dict[str, Any], trigger: str) -> str:
    """Compose the user prompt from a snapshot dict (post-strip)."""
    parts: list[str] = []
    parts.append(f"Trigger: {trigger}")
    parts.append(f"Status: {snapshot.get('status')}")
    if snapshot.get("trinity_r"):
        parts.append(f"Trinity (Reflexivity): {snapshot['trinity_r']}")
    if snapshot.get("trinity_p"):
        parts.append(f"Trinity (Platform):    {snapshot['trinity_p']}")
    if snapshot.get("system_fold"):
        parts.append(f"System fold: {snapshot['system_fold']}")
    parts.append("")
    parts.append("Brackets:")
    for cid, b in (snapshot.get("brackets") or {}).items():
        st = b.get("status", "?")
        desc = b.get("descriptor") or b.get("value") or ""
        parts.append(f"  {cid}  {st}  {desc}")
    return _strip_secrets("\n".join(parts))


def narrate(
    snapshot: dict[str, Any],
    trigger: str,
    *,
    api_key: str | None = None,
    client: Any = None,
) -> str | None:
    """Generate a 2-4 sentence summary. Returns None on any error.

    `client` is overridable for tests (must implement
    `chat.completions.create(...)` from the openai SDK).
    """
    if client is None:
        key = api_key or _read_api_key()
        if not key:
            return None
        try:
            from openai import OpenAI

            client = OpenAI(api_key=key)
        except Exception:
            return None

    user_prompt = _build_user_prompt(snapshot, trigger)
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=OPENAI_MAX_TOKENS,
            temperature=OPENAI_TEMPERATURE,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = resp.choices[0].message.content
        if not text:
            return None
        return text.strip()
    except Exception as e:  # pragma: no cover — requires live API
        # Caller persists this as text="<narrative unavailable: ...>"
        return None


def unavailable_text(reason: str) -> str:
    return f"<narrative unavailable: {reason}>"
