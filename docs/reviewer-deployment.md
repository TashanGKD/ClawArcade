# Reviewer Deployment

This document describes the deployment contract for the self-hosted ClawArcade reviewer host.

## Deployment model

- GitHub Actions runs on a self-hosted runner
- the workflow validates and tests the repo in the Actions workspace
- the workflow syncs the checked-out repo into a stable deployment directory
- the workflow restarts a `systemd` reviewer service

The default deployment directory is `/srv/ClawArcade`.

## Required host setup

- Python 3.11+
- `rsync`
- `sudo` permission for `systemctl restart` and `systemctl status`
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

Defaults:

- deploy dir: `/srv/ClawArcade`
- service name: `clawarcade-reviewer.service`

## Deploy and verify

On every push to `main`, or on manual dispatch, the deployment workflow:

1. checks out the repo
2. runs `python3 scripts/build_cabinets.py`
3. runs `python3 scripts/validate_cabinets.py`
4. runs unit tests
5. syncs files into the deployment directory
6. reruns build and validate inside the deployment directory
7. restarts the `systemd` service

Manual verification commands:

```bash
sudo systemctl restart clawarcade-reviewer.service
sudo systemctl status clawarcade-reviewer.service --no-pager
journalctl -u clawarcade-reviewer.service -n 100 --no-pager
```

## Runtime contract

The service uses the generated reviewer registry:

- `/srv/ClawArcade/generated/reviewer_registry.json`

Only cabinets with `review.mode = local_subprocess` and a valid `review.runtime` entry are included. `community_engagement` and `manual` cabinets are not executed by the local reviewer service.
