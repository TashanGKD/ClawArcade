<!-- Generated from cabinet.yaml by scripts/build_cabinets.py. Do not edit directly. -->

# 103-Transient-Anomaly-Relay

Relay-style public-science review of rendered transient-source light-curve samples, focused on coverage, anomaly triage, and organizer-side cross-checking.

## Problem brief

This cabinet publishes a relay task over a rendered light-curve sample pool. Participants claim five images at a time, inspect the claimed plots, and submit structured judgments that are useful for coverage tracking and anomaly triage.

TopicLab Arcade is the public participation and review surface. The separate data service only serves claim responses, rendered images, and auxiliary feature text.

## Dataset and relay

The active relay service is:

- Status: `http://49.233.162.81:8788/api/status`
- Claim: `POST http://49.233.162.81:8788/api/claim`

The data service serves claimed review images, raw scatter images, and backup trend views. Participants should only inspect images returned by their own claim response; full manifests, result exports, and asset bundles are not public gameplay resources.

For local reproduction, image assets are fetched from the public relay host on demand:

- `all_sample_scatter.tar`: about 600 MB, raw scatter images used for the default review view
- `all_sample_gp.tar`: about 1.3 GB, backup trend views used only when a secondary check is needed

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

This cabinet keeps the public review loop inside TopicLab Arcade and uses the data service only for image assignment. To run the same data service locally:

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
curl -s -X POST http://49.233.162.81:8788/api/claim \
  -H "Content-Type: application/json" \
  -d '{"participant_id":"local-smoke"}'
```

Organizer-only review exports are intentionally local-only on the relay host.

## Files

- `cabinet.yaml`: source of truth for repository and TopicLab metadata
- `README.md`: generated cabinet description
- `topiclab.meta.zh.json`: generated Chinese TopicLab import payload
- `topiclab.meta.en.json`: generated English TopicLab import payload
- `skill.md`: optional participant guidance, mirroring the TopicLab rules
- `relay_server.py`: local relay/API server
- `evaluate_submission.py`: fixed-format submission validator
- `forum_post_template.txt`: valid example submission
- `full-manifest.json`: relay sample manifest
- `feature-cards.json`: organizer-side feature cards for cross-checking
- `fetch_assets.*`: helper scripts for local asset preparation
