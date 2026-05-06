# attestation-service

Symmetism's public attestation surface (battle plan Phase H).

Three jobs:

1. **Publish** — `sym push` POSTs a new attestation here on every
   successful deploy; the service prepends it to a public Gist
   (configured via `~/.symmetism/config/attestation.toml`).
2. **Verify** — serves `symmetism.com/verify` (HTML + JS that
   recomputes the proof in the visitor's browser via Web Crypto API).
3. **Badges** — emits the SVG badges the repo READMEs embed.

Endpoints:

| Path | Description | Auth |
|---|---|---|
| `GET /healthz` | container liveness | none |
| `GET /__manifest` | trinity third leg (per F4 helper) | `X-Symverify-Token` |
| `GET /api/fingerprint/latest.json` | latest attestation | none, 5-min cache |
| `GET /api/fingerprint/badge.svg` | system-fold badge SVG | none, 5-min cache |
| `GET /api/fingerprint/timestamp.svg` | last-verified badge SVG | none, 5-min cache |
| `GET /api/timeline.json` | last 30 days of fingerprints | none, 1-h cache |
| `POST /api/publish` | append a new attestation | `X-Attestation-Token` |
| `GET /verify` | static `verify.html` | none |
| `GET /static/*` | css / js / fonts | none |

Stack: FastAPI + Uvicorn; httpx for Gist API; pydantic models. No DB.
