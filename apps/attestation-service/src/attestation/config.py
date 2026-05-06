"""Service configuration via env vars (twelve-factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Gist where attestations are published. The raw URL is read by the
    # verify page and by /api/fingerprint/latest.json so visitors can
    # independently compare what we serve vs what GitHub serves.
    gist_id: str
    gist_filename: str
    gist_token: str          # gist-scoped PAT for publish
    publish_token: str       # X-Attestation-Token gate on POST /api/publish
    symverify_token: str     # X-Symverify-Token gate on GET /__manifest
    cache_ttl: int = 300     # 5 min for fingerprint endpoints
    timeline_cache_ttl: int = 3600  # 1 h for timeline endpoint


def load() -> Settings:
    return Settings(
        gist_id=os.environ.get("ATTESTATION_GIST_ID", ""),
        gist_filename=os.environ.get(
            "ATTESTATION_GIST_FILENAME", "symmetism-attestations.json"
        ),
        gist_token=os.environ.get("ATTESTATION_GIST_TOKEN", ""),
        publish_token=os.environ.get("ATTESTATION_PUBLISH_TOKEN", ""),
        symverify_token=os.environ.get("SYMVERIFY_TOKEN", ""),
    )
