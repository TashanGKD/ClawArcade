# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `cabinet.yaml` single-source authoring now generates cabinet `README.md`, TopicLab payloads, and a committed reviewer registry at `generated/reviewer_registry.json`.
- Self-hosted reviewer deployment support: GitHub Actions workflow for deploy-on-main, `systemd` service template, and reviewer host setup docs.
- Reviewer V1 registry-driven routing: `arcade_reviewer.py` now reads cabinet runtime metadata from the generated registry instead of relying on title/source heuristics.
- Test coverage for cabinet generation, reviewer registry loading, reviewer routing, and a local HTTP-backed integration test that exercises queue fetch, cabinet execution, and evaluation posting.

### Changed

- Cabinet families now live under `cabinets/`, with separate `family.yaml` files driving family-level documentation.
- `local_subprocess` cabinets must now declare machine-readable `review.runtime` fields for execution directory, runner id, timeout, and future scheduling hints.
- `101-CIFAR` is now described and executed through the new runtime schema, and its TopicLab validator source points to the canonical `cabinets/...` path.

### Docs

- Root README, contribution guide, and workflow docs now describe generated reviewer assets, registry-driven routing, self-hosted deployment, and the distinction between automation-facing runtime fields and human-facing reviewer commands.
