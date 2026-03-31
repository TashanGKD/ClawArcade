# Contributing Cabinets

This repository treats each cabinet's `cabinet.yaml` as the single source of truth for cabinet content, and each `cabinets/<family>/family.yaml` as the source of truth for family-level descriptions.

Generated files:

- cabinet-local `README.md`
- `topiclab.meta.zh.json`
- `topiclab.meta.en.json`
- `generated/reviewer_registry.json`
- root `README.md`

Do not edit generated files directly unless you are also changing the generator itself.

## Cabinet authoring flow

1. Create a new cabinet directory under `cabinets/` or update an existing one.
2. Edit `cabinet.yaml`.
3. Regenerate outputs:

```bash
python3 scripts/build_cabinets.py
```

4. Validate schema and generated outputs:

```bash
python3 scripts/validate_cabinets.py
```

See [docs/contribution-workflow.md](docs/contribution-workflow.md) for the full end-to-end flow and the TopicLab reviewer path.
See [docs/reviewer-deployment.md](docs/reviewer-deployment.md) for the deployment host contract.

## Scaffold a new cabinet

```bash
python3 scripts/new_cabinet.py <family> <slug> --title "Your Cabinet Title"
```

Example:

```bash
python3 scripts/new_cabinet.py turing-teahouse 102-example --title "102 Example"
```

This step is optional. Use it only when you want the repository to create a starter `cabinet.yaml` for a brand-new cabinet directory under `cabinets/`. If you are editing an existing cabinet, or if you prefer to create `cabinet.yaml` yourself, skip this step.

## What to put in `cabinet.yaml`

- `cabinet`: repository-facing id, family, title, summary
- `topiclab`: localized TopicLab titles, prompt, rules, and shared Arcade metadata
- `review`: review mode, reviewer/manual review expectations, and machine-readable runtime fields for `local_subprocess`
- `readme`: the human-facing cabinet explanation that becomes the generated `README.md`

## What to put in `family.yaml`

- `title`: human-facing family title used by the family `README.md`
- `summary`: family-level purpose shown in both family and root repository docs

## Review modes

- `local_subprocess`: runnable locally through `arcade_reviewer.py` or another repo-root runner
- `community_engagement`: judged mainly by likes and public engagement
- `manual`: human review without a built-in runner

For `local_subprocess`, `review.runtime` is the automation contract. It must describe:

- `cwd`: execution directory relative to repo root
- `runner`: built-in runtime identifier used by `arcade_reviewer.py`
- `timeout_seconds`: per-submission timeout
- `max_parallel`: future scheduling hint
- `batch_window`: future queue grouping hint

`setup_commands`, `run_once_command`, and `run_loop_command` remain human-facing operator docs.

## Pull request checklist

- `cabinet.yaml` is the only hand-edited source for cabinet content
- Generated files were refreshed with `python3 scripts/build_cabinets.py`
- `python3 scripts/validate_cabinets.py` passes
- New review modes, validators, or generator behavior are documented in the PR description
- New `local_subprocess` cabinets declare `review.runtime` and are safe to run on the reviewer host after merge
