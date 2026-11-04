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
- **Postgres** moved to `wob_awards`, a new dedicated database on `AI-Postgre`
  (143.198.183.46), the shared Postgres+Valkey droplet other projects (e.g. `grant_prospector`)
  already use -- not a new database service, and not the droplet's own Postgres container either.
  The droplet's data was dumped (plain-SQL, not custom-format: the droplet runs pg17, AI-Postgre
  runs pg16, and pg17's custom-format dumps aren't readable by pg16's `pg_restore`) and restored
  once; the droplet's own Postgres container is no longer used by this Space and can eventually be
  dropped from `infrastructure/deployment/compose.yaml`.
  **Reached directly over its public IP, not a private/VPC address** (`DB_HOST`/`DB_PORT`
  secrets): Hugging Face's infrastructure isn't on DigitalOcean's network, so the private IP
  `grant_prospector` uses isn't reachable from here. AI-Postgre's own setup notes
  (`docker-compose.db.yml` on that droplet) say port 5432 should be firewalled to the VPC CIDR
  only and never reachable from the public internet; **it currently is reachable from the public
  internet anyway** (confirmed by connecting to it from outside DigitalOcean entirely) --
  pre-existing, affects every project on that droplet already, flagged rather than fixed here.
  Fixing it (a DigitalOcean Cloud Firewall rule) would also break this Space's access to its own
  database unless Hugging Face's egress ranges are allowed too, which isn't a fixed, documented
  list -- worth solving deliberately rather than as a side effect of this migration.
- **ClamAV** has no persistent volume for its signature database: `freshclam` re-downloads it on
  every container start (`supervisord.conf`'s `clamav` program). Adds roughly a minute to cold
  start; no stale definitions ever ship.

Caddy is dropped: the Space's own edge terminates TLS, so there's nothing for it to do here.

## A real gap this surfaced: deployed code isn't all in git

The droplet's deploy flow (`infrastructure/deployment/deploy.sh`) builds images locally and ships
them directly (`docker build` -> `docker save | ssh ... | docker load`); it never pushes source
anywhere. Building this Space from `main` failed alembic migrations (`Can't locate revision
identified by '0021'`) because the repo stopped at migration 0013 while the running database was
on 0021. Pulling the real source out of the running containers (`docker cp`) turned up most of
`backend/app/` and the `judges-pack`/`judges-scores` frontend routes, none of it committed
anywhere -- judging, role permissions, rating scales, form import, AI cost tracking, LangSmith
tracing. That gap is fixed for the backend (this branch now matches the real running source, see
its commit history), but **the frontend can't be fixed the same way**: the production frontend
image only ships the compiled `.next` output, not the `.tsx` source that produced it, and that
source does not exist in this repo. `frontend-dist/.next` in this directory is the real compiled
build, pulled once from the running `wob-web-1` container, used as-is below instead of rebuilding
from the (incomplete) checked-in frontend source -- it is not committed to this repo (a 43MB
binary build artifact doesn't belong in source control; `.next/` is already gitignored) and needs
rebuilding from here whenever the frontend changes, same as `infrastructure/hf/Dockerfile` itself.
The actual current frontend source most likely only exists on whichever machine last ran
`deploy.sh` -- probably worth finding and committing before this gap causes a real problem.

## Required secrets (Space settings -> Variables and secrets)

| Name | Value |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://wob_awards_app:<password>@143.198.183.46:5432/wob_awards` |
| `DB_HOST` | `143.198.183.46` (read by `supervisord.conf` to wait for Postgres before starting api/worker) |
| `DB_PORT` | `5432` |
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
cp infrastructure/hf/{Dockerfile,clamd.conf,freshclam.conf,supervisord.conf,wait-for.sh} wob-space/
cp -r infrastructure/hf/frontend-dist wob-space/
cp infrastructure/hf/SPACE_README.md wob-space/README.md
cd wob-space && git add -A && git commit -m "Deploy" && git push
```

`frontend-dist/` is not part of this repo (see above) -- it has to be regenerated from the live
container whenever the frontend changes:

```
docker exec wob-web-1 tar -czf - -C /app .next > frontend-next.tar.gz   # on the droplet
mkdir -p infrastructure/hf/frontend-dist && tar -xzf frontend-next.tar.gz -C infrastructure/hf/frontend-dist
```

The `Dockerfile` pulls the actual application source (`backend/`, `frontend/`) from
`SOURCE_REPO`/`SOURCE_REF` at build time rather than duplicating it here, so the Space always
builds from a real commit of the app instead of a second, driftable copy. `SOURCE_REF` must be a
commit SHA, not a branch name -- see the comment above it in `Dockerfile` for why (Docker's build
cache silently keeps the first commit it ever saw otherwise, so bumping it is how you make a
rebuild actually pick up new commits). It currently points at a commit on a fork
(`deploy/hf-migration-r2-storage`, see the open PR) -- once merged, update the `ARG SOURCE_REPO`
default to `https://github.com/samuelmugisha/wob.git` and `ARG SOURCE_REF` to the merge commit's
SHA, or override both as Space build args.

## Known gaps / not yet done here

- `R2Storage` (the code) has unit tests against a fake S3 client but hasn't been exercised
  against a real R2 bucket inside this app end-to-end -- only the one-off raw-byte migration of
  existing files has been verified against the real bucket.
- Smoke-tested: the Space builds, boots, `/api/v1/health` returns `{"status":"ok","database":"ok",
  "pgvector":"0.8.6"}`, and `/login` renders. Not yet tested: actually signing in, uploading a
  document, or any AI-processing path end-to-end.
- AI-Postgre's port 5432 being open to the public internet (see above) predates this migration
  and affects other projects on that droplet too -- worth a deliberate fix (Cloud Firewall rule
  scoped to Hugging Face's egress, or moving this specific database behind its own auth proxy),
  not something to half-fix as a side effect of this change.
- The real frontend source (see above) needs to be found and committed; until then,
  `frontend-dist/.next` is a snapshot that silently goes stale the next time anyone changes the
  frontend and deploys it to the droplet without also regenerating this directory.
