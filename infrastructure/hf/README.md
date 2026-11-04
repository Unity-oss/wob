# Hugging Face Space deployment

Single-container alternative to `infrastructure/deployment/` (the droplet's six-service compose
stack), for running on a Hugging Face Space. Background and tradeoffs below; this file isn't
itself part of the Space -- `SPACE_README.md`, `Dockerfile` and the other files in this directory
are (see "Setting up the Space").

## Why one container

A Docker-SDK Space runs exactly one container and exposes one port, and its disk is wiped on
every rebuild unless you pay for Persistent Storage. Rather than pay for that, this setup keeps
the two things that need to survive restarts outside the Space entirely:

- **Files** (originals, reports) go to Cloudflare R2 instead of the local disk the droplet uses.
  See `backend/app/services/storage.py`'s `R2Storage`. Existing files were copied over once as a
  one-off migration (raw bytes, already encrypted -- not re-encrypted).
- **Postgres** stays on the droplet (`infrastructure/deployment/`), reached over an SSH tunnel
  through a restricted `dbtunnel` user (see `db-tunnel.sh`) rather than exposed publicly. This
  means **the droplet needs to keep running** for the Space to have a database -- this isn't a
  full migration off DigitalOcean, just off needing it to serve traffic directly.
- **ClamAV** has no persistent volume for its signature database: `freshclam` re-downloads it on
  every container start (`supervisord.conf`'s `clamav` program). Adds roughly a minute to cold
  start; no stale definitions ever ship.

Caddy is dropped: the Space's own edge terminates TLS, so there's nothing for it to do here.

## Required secrets (Space settings -> Variables and secrets)

| Name | Value |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://wob:<POSTGRES_PASSWORD>@127.0.0.1:5432/wob` (the tunnel makes the droplet's DB look local) |
| `DB_TUNNEL_HOST` | the droplet's IP |
| `DB_TUNNEL_SSH_KEY` | the `dbtunnel` user's private key (whole file, including `-----BEGIN...` lines) |
| `SECRET_KEY`, `FILE_ENCRYPTION_KEYS` | same values as the droplet's `/etc/wob/env` |
| `STORAGE_BACKEND` | `r2` |
| `R2_BUCKET`, `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | from the Cloudflare R2 API token |
| `PUBLIC_BASE_URL` | the Space's URL |
| `ANTHROPIC_API_KEY` / `LLM_PROVIDER` etc. | same as the droplet, if AI features are turned on here |

Everything else `app/config.py` reads has a workable default.

## Setting up the Space

This directory is staged inside the main repo for review; the Space itself is a separate git
repo (`huggingface.co/spaces/<org>/<name>`) that needs only this directory's contents, flattened
to its root:

```
git clone https://huggingface.co/spaces/<org>/<name> wob-space
cp infrastructure/hf/{Dockerfile,clamd.conf,freshclam.conf,supervisord.conf,db-tunnel.sh,wait-for.sh} wob-space/
cp infrastructure/hf/SPACE_README.md wob-space/README.md
cd wob-space && git add -A && git commit -m "Deploy" && git push
```

The `Dockerfile` pulls the actual application source (`backend/`, `frontend/`) from
`SOURCE_REPO`/`SOURCE_REF` at build time rather than duplicating it here, so the Space always
builds from a real commit of the app instead of a second, driftable copy. It currently points at
this branch on a fork (`deploy/hf-migration-r2-storage`, see the open PR) -- once merged, update
the `ARG SOURCE_REPO`/`ARG SOURCE_REF` defaults at the top of `Dockerfile` to
`samuelmugisha/wob`/`main`, or override them as Space build args.

## Known gaps / not yet done here

- `R2Storage` (the code) has unit tests against a fake S3 client but hasn't been exercised
  against a real R2 bucket inside this app end-to-end -- only the one-off raw-byte migration of
  existing files has been verified against the real bucket.
- No smoke test of the full combined container has been run yet (build + boot + login + upload).
- If the droplet is ever decommissioned, the database needs to move somewhere durable first (a
  managed Postgres with pgvector) -- this setup assumes it keeps running.
