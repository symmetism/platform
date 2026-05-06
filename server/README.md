# Platform / server

Server-side compose layout for the Symmetism deployment (battle plan
F6). Lives in this repo so changes are version-controlled, but is
deployed to `/srv/symmetism/` on the VPS by F9.

```
server/
├── compose.yaml                       # caddy + watchtower (top-level)
├── Caddyfile                          # reverse-proxy config
├── apps/
│   └── reflexivity-webapp.compose.yaml  # one file per deployed app
└── README.md                          # this file
```

## Bring-up on a fresh VPS

After Phase F9 provisioning (Docker installed, /srv/symmetism/
populated):

```bash
cd /srv/symmetism
docker compose pull
docker compose up -d
```

## Host-side env files

Each app reads `/etc/symmetism/<name>.env` for runtime secrets that
shouldn't end up in the repo. For reflexivity-webapp:

```
SYMVERIFY_TOKEN=<32-byte hex; matches ~/.symmetism/secrets/symverify.reflexivity-webapp.token on the operator side>
FRAMEWORK_VERSION=v0.42-canonical
```

ACL: `chmod 0600 /etc/symmetism/*.env` and chown to root.

## Adding a new app

1. Add a per-app compose at `apps/<name>.compose.yaml`.
2. Append the path to the `include:` block in `compose.yaml`.
3. Add a routing block to `Caddyfile`.
4. Place `/etc/symmetism/<name>.env` on the VPS.
5. `docker compose up -d` on the VPS picks it up.

The build-and-publish workflow at `Reflexivity/.github/workflows/build-and-publish.yml`
auto-builds any container whose source changes under `apps/<name>/`.
