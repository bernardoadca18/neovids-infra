# NeoVids local operations

The authoritative operational sources live in the dedicated `neovids-infra`
Git repository. Workspace-level `infra` and `scripts` paths are relative
symlinks retained only for operator convenience. The CLI resolves the workspace
from the repository location, so the repository can be moved together with its
five sibling service repositories without embedding an absolute host path.

This directory coordinates the five independent NeoVids Compose projects. It
does not replace their service-owned Compose files, move business logic, or own
LiteLLM/Ollama. The operational layer adds a shared API backplane and one safe
development lifecycle.

## Normal workflow

From `/home/bernardo/workspaces/neovids`:

```bash
./scripts/neovids doctor
./scripts/neovids up
./scripts/neovids status
./scripts/neovids health
./scripts/neovids logs media-indexer
./scripts/neovids down
```

`up` works from a cold image/cache state. It validates installation mode and
bind-mount identities, creates the external `neovids` network when absent,
starts only the three application databases and Orchestrator Redis, verifies
representative existing table contents, builds missing images, inspects Alembic
state, and starts APIs in dependency order. When at least one database has a
pending migration, it writes logical PostgreSQL backups outside Git/volumes
before upgrading. A normal current startup does not create another full dump.
Readiness polling is bounded; fixed sleeps are not used as the primary gate.

Use `./scripts/neovids up --build` after source or dependency changes that must
be copied into images. Plain `up` builds only missing images and intentionally
does not guess whether an existing image is stale. `./scripts/neovids build`
rebuilds all application images without starting the stack.

`down` executes project-scoped `docker compose down --remove-orphans` in reverse
dependency order. It never passes `-v`, never prunes, leaves the external
network in place, and never manages LiteLLM, Ollama, or unrelated containers.
Removing the application containers is deliberate: starting Docker or opening
WSL after `down` cannot resurrect Qwen, Whisper workers, rendering, or workflow
workers through a restart policy.

## Ownership and dependency graph

```text
LiteLLM / Ollama (external, detected only)

Ideas Hub ────────────────> Media Indexer
                                  ^
                                  |
Orchestrator ─> Ideas Hub         |
             ├> Qwen Voice        |
             ├> Media Indexer ----+
             └> Video Composer ---+
                         |
                         └> Orchestrator
```

Databases remain attached only to their service-local default networks. The
external `neovids` bridge attaches application APIs that communicate across
repositories. Stable aliases are:

| Alias | Container port | Owner |
| --- | ---: | --- |
| `ideas-hub` | 8000 | Ideas Hub backend |
| `media-indexer` | 8000 | Media Indexer API/worker |
| `qwen-voice` | 8889 | Qwen Voice Service |
| `video-composer` | 8013 | Video Composer |
| `orchestrator-api` | 8000 | Orchestrator API |

The Orchestrator/Composer bidirectional runtime relationship is not represented
as a circular Compose `depends_on`. Each side starts independently and reports
eventual readiness.

## Host port inventory

| Service | Host port | Readiness |
| --- | ---: | --- |
| Ideas Hub API | 8000 | `/health/ready` |
| Ideas Hub frontend | 5173 | HTTP root |
| Ideas Hub PostgreSQL | 5432 | `pg_isready` |
| Orchestrator API | 8011 | `/health/ready` |
| Orchestrator frontend | 5174 | HTTP root |
| Orchestrator PostgreSQL | 5434 | `pg_isready` |
| Orchestrator Redis | 6380 | `redis-cli ping` |
| Media Indexer API | 8012 | `/health/ready` |
| Media Indexer PostgreSQL | 5435 | `pg_isready` |
| Qwen Voice | 8889 | `/health/ready` |
| Video Composer | 8013 | `/health/ready` |
| LiteLLM (external) | 4000 | `/health/readiness` |
| Ollama (external, optional) | 11434 | `/api/tags` |

Liveness endpoints do not invoke models, embeddings, FFmpeg, Chromium, or jobs.
Orchestrator readiness checks its durable stores and the four required service
APIs. LiteLLM is checked separately because NeoVids does not own its lifecycle.

## Durable storage contract

The CLI refuses to start when an established named volume is absent:

| Store | Effective storage |
| --- | --- |
| Ideas Hub PostgreSQL | `ideas-hub-db-data` |
| Media Indexer PostgreSQL | `media-indexer_media-indexer-db-data` |
| Media Indexer model cache | `media-indexer_media-indexer-model-cache` |
| Orchestrator PostgreSQL | `orchestrator-db-data` |
| Video Composer data/SQLite/outputs | `video-composer_video-composer-data` |
| Media Indexer managed media | `media-indexer/storage` bind mount |
| Orchestrator narration | `orchestrator/data/audio` bind mount |
| Qwen model cache | `qwen3-tts-workshop/cache/huggingface` bind mount |
| Qwen references/jobs/outputs | `qwen3-tts-workshop/data/*` bind mounts |

Compose project names are explicit in the CLI so implicit Media Indexer and
Composer volume names cannot drift. Backups default to
`$HOME/backups/neovids/<UTC timestamp>` with mode-restricted files. Set
`NEOVIDS_BACKUP_ROOT` to another non-repository path if needed.

## Installation modes

The five canonical named volumes form one installation identity:

- **existing**: all five exist; startup verifies representative database
  content and reuses them;
- **fresh**: none exists; `up` creates exactly those canonical volumes and
  initializes schemas without making meaningless empty-database backups;
- **partial/ambiguous**: only some exist; startup fails closed before starting
  databases or creating replacements.

Fresh bootstrap creates only the known writable bind directories. Qwen's
version-controlled reference WAV/text must still be present, and service-local
`.env` configuration remains the operator's responsibility. Never remove real
volumes to test bootstrap behavior; the CLI's shell tests use a fake Docker
boundary.

Backups are never deleted automatically. Review `$NEOVIDS_BACKUP_ROOT`
periodically and move or remove obsolete timestamped sets only after an
operator confirms their retention requirements.

## Source changes and builds

| Service | Development source behavior | Rebuild for a durable image |
| --- | --- | --- |
| Ideas Hub backend | source is copied into the image | `up --build` or `build` |
| Ideas Hub frontend | `src` is bind-mounted in local Compose; dependencies are in the image | rebuild after dependency/Dockerfile changes |
| Media Indexer | source is copied into the image | `up --build` or `build` |
| Qwen Voice | runtime source/config are copied into the image; data/cache are mounted | `up --build` or `build` |
| Video Composer | source is copied into the image | `up --build` or `build` |
| Orchestrator backend/frontend | repository source is bind-mounted in local Compose | rebuild after dependency/Dockerfile changes |

Never use `docker compose down -v`, `docker volume prune`, `docker system
prune`, or broad host-wide cleanup as part of normal NeoVids operation. Safe
cleanup of old caches/outputs remains a deliberate, store-specific operator
action and is not implemented by this CLI.

## Disk and footage safety

No root command scans `W:\`, `/mnt/w`, other Windows drive roots, managed media,
model caches, or generated output trees. Media source selection remains an
explicit operator action. No footage drive is mounted by the orchestration
overrides. Docker build contexts exclude virtual environments, Node modules,
model caches, media storage, outputs, coverage, Git metadata, and local secrets.

## GPU and model lifecycle

Qwen is the canonical voice service. It loads one pinned model through FastAPI
lifespan, caches voice prompts, owns one bounded serial GPU worker, and receives
30 minutes for graceful shutdown. Media Indexer keeps Faster-Whisper lazy and
reuses one loaded model per process. Root orchestration controls when these
services run; it does not change model, revision, precision, sampling, ASR beam,
frame quality, render profile, or any Stage 1-18 production semantics.

On a constrained workstation, avoid running unrelated GPU workloads while Qwen
or Faster-Whisper is active. GPU scheduling and device/precision changes are
intentionally outside this hardening layer.

## Logs and subprocesses

Service Compose files use Docker's `local` driver with bounded rotation. Python
services run with Docker init enabled; Qwen and long-running workers have
explicit grace periods. Video Composer already embeds `tini` and waits for its
active worker during SIGTERM. These settings improve child reaping without
changing FFmpeg/Remotion output.

`./scripts/neovids logs` returns the latest 200 lines rather than following all
services indefinitely. Pass a service group to narrow output.

## Cold recovery and troubleshooting

1. Run `./scripts/neovids doctor`. Missing established volumes/bind mounts are
   fatal; an absent shared network is expected before the first `up`.
2. Check that the Docker daemon and NVIDIA runtime are available.
3. Run `./scripts/neovids up`. A missing required table stops startup before
   application migrations.
4. If readiness fails, inspect `./scripts/neovids status` and
   `./scripts/neovids logs <service>`.
5. LiteLLM/Ollama failures are reported but never repaired or replaced by this
   tooling. Fix them in their owning deployment.

An external container may be marked unhealthy because its own image lacks the
binary used by its Docker healthcheck even while its HTTP readiness endpoint is
healthy. The NeoVids CLI uses the HTTP contract as the external dependency
signal and reports the Docker discrepancy separately during diagnosis.

The service repositories remain independently operable with their original
Compose commands. Repository-local Compose no longer uses automatic restart;
an operator explicitly starts it and `docker compose down` remains non-
destructive to named volumes.

## Host-level settings

WSL systemd and Docker are host responsibilities. This layer does not edit
`/etc/wsl.conf`, `.wslconfig`, Docker Desktop settings, Windows startup,
Defender, Search indexing, or mounted-drive policy. Docker/containerd may start
with WSL, but after `neovids down` there are no NeoVids application containers
for the daemon to restart.

TLS, authentication, and public rate limiting remain deployment concerns. The
documented ports target a trusted local development network.
