# Security

## Reporting a vulnerability

If you've found a security issue in Ouvrage, please email **steve.fritz@gmail.com** rather than opening a public GitHub issue. Include enough detail to reproduce. I'll respond within a few days and coordinate a fix and (if relevant) disclosure timeline before anything goes public.

## Known historical exposures

The following credentials were committed to this repository before it became public on 2026-04-25. They have all been rotated and are no longer valid.

- **Slack bot token** in `switchboard.service` (commit `31214757`, 2026-03-10). Rotated. The systemd unit was later refactored to load secrets from `/etc/ouvrage/env` via `EnvironmentFile=`; the current `ouvrage.service` does not contain credentials.

History was not rewritten — the leaked values remain extractable from old commits, but are non-functional after rotation. If you scan and find one, it's a dead artifact, not a live secret.

## What's in scope

- The Ouvrage server (`ouvrage/` package)
- The dashboard SPA (`dashboard/`)
- The Docker image and entrypoint (`Dockerfile`, `docker-entrypoint.sh`)
- The setup script (`setup.sh`)

## What's not in scope

- Vulnerabilities in upstream dependencies (report those to the relevant project)
- Configuration mistakes in someone else's deployment (e.g. an exposed `/data` volume)
- Issues that require pre-existing privileged access on the host running Ouvrage
