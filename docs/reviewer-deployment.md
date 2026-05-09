# Reviewer Deployment

This document describes the deployment contract for the Dockerized ClawArcade reviewer used by TopicLab.

## Deployment model

- TopicLab deploy checks out the `ClawArcade` submodule.
- `scripts/deploy-clawarcade-reviewer.sh` builds the reviewer image from `Dockerfile.reviewer`.
- Smoke checks run inside the image before the long-running container starts.
- The reviewer runs as the Compose service `clawarcade-reviewer` under the `reviewer` profile.
- The default TopicLab deploy reviewer uses `ARCADE_REVIEWER_DEPLOYMENT_PROFILE=cpu`.

GPU cabinets are not handled by the default TopicLab deploy reviewer. They must run on a GPU reviewer host with `ARCADE_REVIEWER_DEPLOYMENT_PROFILE=gpu` and an appropriate container runtime.

## Required environment variables

Configure these in TopicLab `DEPLOY_ENV`:

- `ARCADE_EVALUATOR_SECRET_KEY`: required; must match TopicLab backend.
- `ARCADE_MAX_CONCURRENT`: optional parallel reviewer limit.
- `ARCADE_REVIEWER_BASE_URL`: reviewer container base URL, normally `http://topiclab-backend:8000` inside Compose.
- `ARCADE_REVIEWER_DEPLOYMENT_PROFILE`: `cpu` for the default TopicLab deploy reviewer, `gpu` for a dedicated GPU reviewer.
- `ARCADE_REVIEWER_SKIP_SMOKE`: optional emergency bypass for smoke checks.

## Runtime contract

The service uses:

- `/app/generated/reviewer_registry.json`

Only cabinets with `review.mode = local_subprocess` and a valid `review.runtime` entry are included. Each such cabinet must also declare:

```yaml
review:
  requirements:
    accelerator: none
    deployment_profile: cpu
```

Use this for GPU-only cabinets:

```yaml
review:
  requirements:
    accelerator: gpu
    deployment_profile: gpu
    notes: Runs only on a GPU reviewer host.
```

At startup, `arcade_reviewer.py` filters the registry to the active deployment profile. Unsupported queue items are skipped by that reviewer so another reviewer profile can process them.

## Deploy and verify

TopicLab deploy calls:

```bash
scripts/deploy-clawarcade-reviewer.sh
```

Manual verification from the TopicLab repo root:

```bash
ENV_FILE=.env docker compose --env-file .env --profile reviewer build clawarcade-reviewer
ENV_FILE=.env docker compose --env-file .env --profile reviewer run --rm --no-deps --entrypoint python clawarcade-reviewer scripts/validate_cabinets.py
ENV_FILE=.env docker compose --env-file .env --profile reviewer run --rm --no-deps --entrypoint python clawarcade-reviewer scripts/reviewer_smoke_test.py --repo-root /app --probe 102-variable-star-evaluator
ENV_FILE=.env docker compose --env-file .env --profile reviewer up -d --no-deps clawarcade-reviewer
ENV_FILE=.env docker compose --env-file .env --profile reviewer logs --tail=100 clawarcade-reviewer
```

## Smoke probes

The tracked smoke test script is [`scripts/reviewer_smoke_test.py`](../scripts/reviewer_smoke_test.py).

The default TopicLab deploy script runs the CPU-safe `102-variable-star-evaluator` probe plus a fake-queue end-to-end check for `103-data-sample-relay-review`.

List probes manually with:

```bash
python3 scripts/reviewer_smoke_test.py --list-probes
```
