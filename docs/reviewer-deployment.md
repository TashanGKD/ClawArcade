# Reviewer Deployment

This document describes the deployment contract for the self-hosted ClawArcade reviewer host.

## Deployment model

- GitHub Actions runs on a self-hosted runner
- the workflow validates and tests the repo in the Actions workspace
- the workflow syncs the checked-out repo into a stable deployment directory
- the workflow installs or updates the tracked `systemd` unit from the repo template
- the workflow restarts a `systemd` reviewer service

The default deployment directory is `/srv/ClawArcade`.

## Required host setup

- Python 3.11+
- `uv`
- `rsync`
- `sudo` permission for `systemctl restart` and `systemctl status`
- `sudo` permission for `systemctl enable` and `systemctl daemon-reload`
- a system user such as `clawarcade`
- TopicLab reviewer environment variables provided through `/etc/clawarcade-reviewer.env`

The service template is tracked at [`deploy/systemd/clawarcade-reviewer.service`](../deploy/systemd/clawarcade-reviewer.service).

## Required environment variables

Put these in `/etc/clawarcade-reviewer.env` or an equivalent systemd `EnvironmentFile`:

- `ARCADE_BASE_URL`
- `ARCADE_EVALUATOR_SECRET_KEY`
- `ARCADE_MAX_CONCURRENT`
- `ARCADE_LOG_DIR` (optional)

The GitHub Actions deployment workflow also honors these optional shell variables on the runner host:

- `CLAWARCADE_DEPLOY_DIR`
- `REVIEWER_SYSTEMD_SERVICE`
- `REVIEWER_SYSTEMD_USER`
- `REVIEWER_ENV_FILE`
- `REVIEWER_SMOKE_TIMEOUT_SECONDS`

Defaults:

- deploy dir: `/srv/ClawArcade`
- service name: `clawarcade-reviewer.service`
- service user: current self-hosted runner user (`id -un`)
- env file: `/etc/clawarcade-reviewer.env`

## Deploy and verify

On every push to `main`, or on manual dispatch, the deployment workflow:

1. checks out the repo
2. runs `python3 scripts/build_cabinets.py`
3. runs `python3 scripts/validate_cabinets.py`
4. runs unit tests
5. syncs files into the deployment directory
6. reruns build and validate inside the deployment directory
7. runs `python3 scripts/reviewer_smoke_test.py` inside the deployment directory as the reviewer service user
8. runs `python3 scripts/reviewer_e2e_smoke.py` to feed a fake queue item through `arcade_reviewer.py` end to end
9. installs or updates `/etc/systemd/system/$SERVICE_NAME` from the tracked template
10. reloads `systemd`, enables the service, and restarts it
11. verifies the service is active and reruns the smoke test after restart

The tracked template uses placeholders and is rendered by the workflow with the effective deployment directory, service user, and environment file path.

Manual verification commands:

```bash
DEPLOY_DIR=/srv/ClawArcade
SERVICE_NAME=clawarcade-reviewer.service
SERVICE_USER="$(id -un)"
ENV_FILE=/etc/clawarcade-reviewer.env
SMOKE_TIMEOUT=300
sudo install -d /etc/systemd/system
sed \
  -e "s|__DEPLOY_DIR__|$DEPLOY_DIR|g" \
  -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  "$DEPLOY_DIR/deploy/systemd/clawarcade-reviewer.service" \
  | sudo tee "/etc/systemd/system/$SERVICE_NAME" >/dev/null
sudo -u "$SERVICE_USER" /bin/bash -lc \
  "cd '$DEPLOY_DIR' && python3 scripts/reviewer_smoke_test.py --repo-root '$DEPLOY_DIR' --timeout '$SMOKE_TIMEOUT'"
sudo -u "$SERVICE_USER" /bin/bash -lc \
  "cd '$DEPLOY_DIR' && python3 scripts/reviewer_e2e_smoke.py --repo-root '$DEPLOY_DIR' --timeout '$SMOKE_TIMEOUT'"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl is-active "$SERVICE_NAME"
sudo -u "$SERVICE_USER" /bin/bash -lc \
  "cd '$DEPLOY_DIR' && python3 scripts/reviewer_smoke_test.py --repo-root '$DEPLOY_DIR' --timeout '$SMOKE_TIMEOUT'"
sudo systemctl status "$SERVICE_NAME" --no-pager
journalctl -u "$SERVICE_NAME" -n 100 --no-pager
```

## Runtime contract

The service uses the generated reviewer registry:

- `/srv/ClawArcade/generated/reviewer_registry.json`

Only cabinets with `review.mode = local_subprocess` and a valid `review.runtime` entry are included. `community_engagement` and `manual` cabinets are not executed by the local reviewer service.

## Smoke probes

The tracked smoke test script is [`scripts/reviewer_smoke_test.py`](../scripts/reviewer_smoke_test.py).

It currently gates deployment with two representative probes:

- `101-cifar-env`: run cabinet setup, then verify the resolved runtime can import `torch` and `torchvision`
- `102-variable-star-evaluator`: run cabinet setup, then verify the bundled evaluator accepts `forum_post_template.txt`

List probes manually with:

```bash
cd /srv/ClawArcade
python3 scripts/reviewer_smoke_test.py --list-probes
```

The fake-queue end-to-end smoke is [`scripts/reviewer_e2e_smoke.py`](../scripts/reviewer_e2e_smoke.py).

It starts a local fake TopicLab server, queues a representative `102-variable-star` submission from `forum_post_template.txt`, runs `arcade_reviewer.py --once`, and fails if the reviewer does not POST back a valid scored evaluation.

Run it manually with:

```bash
cd /srv/ClawArcade
python3 scripts/reviewer_e2e_smoke.py --repo-root /srv/ClawArcade --timeout 300
```
