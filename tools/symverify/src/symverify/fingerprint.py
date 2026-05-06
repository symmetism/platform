"""Trinity fingerprint and system fold per `_command/08_FINGERPRINT_SPEC.md`.

Spec frozen at `symverify-fingerprint/1`. Implementations in any
language must match byte-for-byte (the verify page recomputes these in
the visitor's browser via Web Crypto API).
"""

from __future__ import annotations

import hashlib
import json

from symverify import SPEC_VERSION

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # 32 chars; no I/L/O/U


def base32_crockford_encode(data: bytes) -> str:
    """Encode bytes to a Crockford-base32 string (uppercase).

    Standard base32 bit-packing (5 bits per char). Bit padding at the
    end if `len(data) * 8` isn't a multiple of 5; we don't emit the
    `=` padding char (none of our outputs need it after truncation).
    """
    bits = "".join(f"{b:08b}" for b in data)
    pad = (5 - len(bits) % 5) % 5
    bits += "0" * pad
    out = []
    for i in range(0, len(bits), 5):
        out.append(CROCKFORD_ALPHABET[int(bits[i : i + 5], 2)])
    return "".join(out)


def trinity_fingerprint(
    local_h: str, git_h: str, server_h: str | None
) -> str:
    """12-char Crockford base32 fingerprint, dash-grouped 4-4-4.

    Input hashes are 64-char lowercase hex root_hash values from the
    three manifest scopes (server may be None pre-deployment).

    Algorithm (spec §7):
      1. Build payload {"spec": SPEC_VERSION, "trinity": [l, g, s]}
         encoded with sorted keys, no whitespace, UTF-8.
      2. SHA-256 -> 32 bytes.
      3. Crockford-base32 encode first 8 bytes (= 12.8 chars worth);
         take first 12.
      4. Format XXXX-XXXX-XXXX.

    Triangle property: identical iff (local_h, git_h, server_h) is the
    exact same tuple.
    """
    payload_obj = {
        "spec": SPEC_VERSION,
        "trinity": [local_h, git_h, server_h],
    }
    payload = json.dumps(
        payload_obj, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    raw = base32_crockford_encode(digest[:8])[:12]
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def system_fold(
    trinities: dict[str, str],
    invariants: dict[str, str],
    symverify_version: str,
) -> str:
    """16-char Crockford fold over both repos' trinities + invariants.

    Format: SYM-XXXX-XXXX-XXXX-XXXX.

    Algorithm (spec §8):
      1. Build payload {"spec", "trinities" (sorted-key dict),
         "invariants" (sorted-key dict), "version"}; sort_keys=True
         in json.dumps handles ordering.
      2. SHA-256 -> 32 bytes.
      3. Crockford-base32 encode first 10 bytes (= 16 chars).
    """
    payload_obj = {
        "spec": SPEC_VERSION,
        "trinities": dict(sorted(trinities.items())),
        "invariants": dict(sorted(invariants.items())),
        "version": symverify_version,
    }
    payload = json.dumps(
        payload_obj, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    raw = base32_crockford_encode(digest[:10])[:16]
    return f"SYM-{raw[0:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"
