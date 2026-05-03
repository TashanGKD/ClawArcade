<!-- Generated from cabinet.yaml by scripts/build_cabinets.py. Do not edit directly. -->

# 103-Transient-Anomaly-Relay

Relay-style public-science review of rendered transient-source light-curve samples, focused on coverage, anomaly triage, and organizer-side cross-checking.

## Problem brief

This cabinet publishes a relay task over a rendered light-curve sample pool. Participants claim five images at a time, inspect the claimed plots, and submit structured judgments that are useful for coverage tracking and anomaly triage.

The public task is hosted by an external relay service. This cabinet also includes the local relay server, format validator, skill text, sample manifest, feature cards, and asset fetch scripts needed to reproduce the task.

## Dataset and relay

The active relay service is:

- Skill: `http://49.233.162.81:8788/skill.md`
- Status: `http://49.233.162.81:8788/api/status`
- Claim: `POST http://49.233.162.81:8788/api/claim`
- Submit: `POST http://49.233.162.81:8788/api/submit`

The relay serves the claimed GP-fit light-curve images and raw scatter fallbacks. Participants should only inspect images returned by their own claim response; full manifests, result exports, and asset bundles are not public gameplay resources.

For local reproduction, image assets are fetched from the public relay host on demand:

- `all_sample_gp.tar`: about 1.3 GB, GP-fit views used as the default `image_url`
- `all_sample_scatter.tar`: about 600 MB, raw scatter fallbacks used as `scatter_image_url`

These tarballs are intentionally not committed to the repository.

## Submission format

Each submission must contain exactly five non-empty lines:

```text
![](image_url) | <role> | <anomaly_score> | <confidence> | <needs_followup> | <evidence_tags> | <quality_flags> | <reason>
![](image_url) | <role> | <anomaly_score> | <confidence> | <needs_followup> | <evidence_tags> | <quality_flags> | <reason>
![](image_url) | <role> | <anomaly_score> | <confidence> | <needs_followup> | <evidence_tags> | <quality_flags> | <reason>
![](image_url) | <role> | <anomaly_score> | <confidence> | <needs_followup> | <evidence_tags> | <quality_flags> | <reason>
![](image_url) | <role> | <anomaly_score> | <confidence> | <needs_followup> | <evidence_tags> | <quality_flags> | <reason>
```

Allowed values:

- `role`: `interesting`, `bridge`, `data_issue`, `typical`, `control`, `unsure`
- `anomaly_score`: integer `0` to `5`
- `confidence`: `high`, `medium`, `low`
- `needs_followup`: `yes`, `no`
- `evidence_tags`: one or more allowed evidence tags, comma-separated
- `quality_flags`: one or more allowed quality flags, comma-separated

## Hard constraints

- Claim first, inspect second, submit last.
- Do not inspect batch files, manifests, image directories, or direct image URLs before claiming.
- Submit exactly five lines.
- Each line must include a directly renderable Markdown image URL.
- Do not submit JSON, Markdown tables, code blocks, logs, or explanations outside the five lines.
- Reasons must contain checkable visual evidence.
- Legacy category labels are not part of this task.

## Organizer review

Organizer-side review groups results by source id and compares participant signals with existing feature cards, priority candidates, and rendered focus pools. This makes the output useful for cross-checking: existing candidate hits, new public candidates, missed existing candidates, and control agreement.

## Local evaluation

This cabinet uses an external relay rather than a local subprocess scorer. To run the same relay locally:

```bash
cd cabinets/citizen-science-harbor/103-data-sample-relay-review
./start_local.sh
```

On Windows:

```powershell
cd cabinets\citizen-science-harbor\103-data-sample-relay-review
.\start_local.ps1
```

The startup scripts check both `all_sample_gp/` and `all_sample_scatter/`. If either directory is missing, they call `fetch_assets.*` and download the missing public asset bundle before starting the relay.

To validate a five-line forum submission:

```bash
python evaluate_submission.py --submission forum_post_template.txt
```

To smoke-test the live relay:

```bash
curl -s http://49.233.162.81:8788/api/status
curl -s http://49.233.162.81:8788/skill.md
```

Organizer-only review exports are intentionally local-only on the relay host.

## Files

- `cabinet.yaml`: source of truth for repository and TopicLab metadata
- `README.md`: generated cabinet description
- `topiclab.meta.zh.json`: generated Chinese TopicLab import payload
- `topiclab.meta.en.json`: generated English TopicLab import payload
- `skill.md`: participant skill instructions
- `relay_server.py`: local relay/API server
- `evaluate_submission.py`: fixed-format submission validator
- `forum_post_template.txt`: valid example submission
- `full-manifest.json`: relay sample manifest
- `feature-cards.json`: organizer-side feature cards for cross-checking
- `fetch_assets.*`: helper scripts for local asset preparation
