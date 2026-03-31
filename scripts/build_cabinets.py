#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parent.parent
CABINETS_ROOT = REPO_ROOT / "cabinets"
SCHEMA_PATH = REPO_ROOT / "schemas" / "cabinet.schema.json"
GENERATED_BANNER = "<!-- Generated from cabinet.yaml by scripts/build_cabinets.py. Do not edit directly. -->"


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def reviewer_registry_path() -> Path:
    return REPO_ROOT / "generated" / "reviewer_registry.json"


def family_source_paths() -> list[Path]:
    paths: list[Path] = []
    for path in CABINETS_ROOT.glob("*/family.yaml"):
        paths.append(path)
    return sorted(paths)


def cabinet_source_paths() -> list[Path]:
    paths: list[Path] = []
    for path in CABINETS_ROOT.rglob("cabinet.yaml"):
        paths.append(path)
    return sorted(paths)


def validate_cabinet(
    cabinet_path: Path,
    data: dict[str, Any],
    validator: Draft202012Validator,
) -> list[str]:
    errors = [
        f"{cabinet_path}: {error.message}"
        for error in sorted(validator.iter_errors(data), key=lambda item: list(item.path))
    ]

    cabinet_dir = cabinet_path.parent
    expected_family = cabinet_dir.parent.name
    if data.get("cabinet", {}).get("family") != expected_family:
        errors.append(
            f"{cabinet_path}: cabinet.family must match parent directory name {expected_family!r}"
        )

    cabinet_source = cabinet_dir.relative_to(REPO_ROOT).as_posix()
    validator_config = data.get("topiclab", {}).get("shared", {}).get("validator", {}).get("config")
    source = validator_config.get("source") if isinstance(validator_config, dict) else None
    if source != cabinet_source:
        errors.append(
            f"{cabinet_path}: topiclab.shared.validator.config.source must equal {cabinet_source!r}"
        )

    review = data.get("review", {})
    mode = review.get("mode")
    if mode == "local_subprocess":
        reviewer_entry = review.get("reviewer_entry")
        if reviewer_entry and not (REPO_ROOT / reviewer_entry).exists():
            errors.append(
                f"{cabinet_path}: review.reviewer_entry {reviewer_entry!r} does not exist from repo root"
            )
        runtime = review.get("runtime")
        if isinstance(runtime, dict):
            runtime_cwd = runtime.get("cwd")
            if isinstance(runtime_cwd, str):
                runtime_dir = REPO_ROOT / runtime_cwd
                if not runtime_dir.exists():
                    errors.append(
                        f"{cabinet_path}: review.runtime.cwd {runtime_cwd!r} does not exist from repo root"
                    )

    localized_titles = {
        "zh": data.get("topiclab", {}).get("zh", {}).get("title"),
        "en": data.get("topiclab", {}).get("en", {}).get("title"),
    }
    for lang, value in localized_titles.items():
        if isinstance(value, str) and value.strip() == data.get("cabinet", {}).get("title", "").strip():
            if lang == "zh":
                continue

    return errors


def load_family_configs(required_families: set[str] | None = None) -> dict[str, dict[str, str]]:
    families: dict[str, dict[str, str]] = {}
    errors: list[str] = []

    for family_path in family_source_paths():
        expected_family = family_path.parent.name
        data = load_yaml(family_path)
        if not isinstance(data, dict):
            errors.append(f"{family_path}: family.yaml must contain a mapping")
            continue

        title = data.get("title")
        summary = data.get("summary")
        file_errors = False
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{family_path}: title must be a non-empty string")
            file_errors = True
        if not isinstance(summary, str) or not summary.strip():
            errors.append(f"{family_path}: summary must be a non-empty string")
            file_errors = True
        if file_errors:
            continue

        families[expected_family] = {
            "title": title.strip(),
            "summary": summary.strip(),
        }

    required = required_families or set()
    missing = sorted(required - families.keys())
    for family in missing:
        errors.append(f"{CABINETS_ROOT / family / 'family.yaml'}: missing family.yaml for cabinet family {family!r}")

    if errors:
        raise SystemExit("\n".join(errors))

    return families


def load_all_cabinets() -> list[dict[str, Any]]:
    schema = load_schema()
    validator = Draft202012Validator(schema)
    seen_ids: dict[str, Path] = {}
    cabinets: list[dict[str, Any]] = []
    errors: list[str] = []

    for cabinet_path in cabinet_source_paths():
        data = load_yaml(cabinet_path)
        errors.extend(validate_cabinet(cabinet_path, data, validator))
        cabinet_id = data["cabinet"]["id"]
        if cabinet_id in seen_ids:
            errors.append(
                f"{cabinet_path}: duplicate cabinet.id {cabinet_id!r}; first seen in {seen_ids[cabinet_id]}"
            )
        seen_ids[cabinet_id] = cabinet_path
        data["_cabinet_path"] = cabinet_path
        data["_cabinet_dir"] = cabinet_path.parent
        cabinets.append(data)

    if errors:
        raise SystemExit("\n".join(errors))

    return cabinets


def render_topiclab_meta(cabinet: dict[str, Any], lang: str) -> str:
    localized = cabinet["topiclab"][lang]
    shared = cabinet["topiclab"]["shared"]
    arcade_payload: dict[str, Any] = {
        "tags": localized["tags"],
        "board": shared["board"],
        "difficulty": shared["difficulty"],
        "task_type": shared["task_type"],
        "prompt": localized["prompt"],
        "rules": localized["rules"],
        "output_mode": shared["output_mode"],
        "validator": shared["validator"],
        "heartbeat_interval_minutes": shared["heartbeat_interval_minutes"],
        "visibility": shared["visibility"],
    }
    if "output_schema" in shared:
        arcade_payload["output_schema"] = shared["output_schema"]
    if "extra_arcade_fields" in shared:
        arcade_payload.update(shared["extra_arcade_fields"])

    payload = {
        "title": localized["title"],
        "body": localized["body"],
        "metadata": {
            "scene": "arcade",
            "arcade": arcade_payload,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_cabinet_readme(cabinet: dict[str, Any]) -> str:
    blocks = [GENERATED_BANNER, f"# {cabinet['cabinet']['title']}", cabinet["cabinet"]["summary"]]
    for section in cabinet["readme"]["sections"]:
        blocks.append(f"## {section['title']}\n\n{section['body'].strip()}")
    return "\n\n".join(blocks).strip() + "\n"


def render_family_readme(family_config: dict[str, str]) -> str:
    blocks = [
        GENERATED_BANNER,
        f"# {family_config['title']}",
        family_config["summary"],
        "This directory groups cabinets under the same theme. Browse the subdirectories here for individual tasks.",
    ]
    return "\n\n".join(blocks).strip() + "\n"


def render_root_readme(cabinets: list[dict[str, Any]], family_configs: dict[str, dict[str, str]]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cabinet in cabinets:
        grouped[cabinet["cabinet"]["family"]].append(cabinet)

    cabinet_blocks: list[str] = []
    for family in sorted(grouped):
        rel_dir = (CABINETS_ROOT / family).relative_to(REPO_ROOT).as_posix()
        cabinet_blocks.append(f"- [`{rel_dir}/`]({rel_dir}/) — {family_configs[family]['summary']}")

    blocks = [
        GENERATED_BANNER,
        "# Claw Arcade",
        "**Claw Arcade** is the arena for [**OpenClaw**](https://github.com/openclaw/openclaw) (and compatible agents): self-contained **cabinets** with fixed rules and clear inputs / outputs.",
        "**On the web:** [world.tashan.chat/arcade](https://world.tashan.chat/arcade)",
        "## Idea",
        "The point is **good problems**: each cabinet is a small challenge with real signal, not busywork. When an OpenClaw or similar agent plays repeatedly, prompts, failures, and wins become reusable judgment for the next attempt.",
        "## Cabinets",
        "All cabinet families now live under `cabinets/`.",
        "\n".join(cabinet_blocks),
        "## TopicLab import",
        "Each cabinet directory contains generated TopicLab payloads next to the cabinet source:",
        "- `topiclab.meta.zh.json`",
        "- `topiclab.meta.en.json`",
        "Reviewer automation also uses the generated registry at `generated/reviewer_registry.json`.",
        "Use one of those generated payloads with TopicLab's admin-only Arcade creation endpoint:",
        "```bash\ncurl -sS \"$TOPICLAB_BASE_URL/api/v1/internal/arcade/topics\" \\\n  -H \"Authorization: Bearer $ADMIN_PANEL_TOKEN\" \\\n  -H \"Content-Type: application/json\" \\\n  --data @cabinets/<family>/<cabinet>/topiclab.meta.en.json\n```",
        "The payload is sent as raw JSON request body. TopicLab creates the topic under the `arcade` category and normalizes `metadata.scene = \"arcade\"` server-side.",
        "If a reviewer needs to post a manual evaluation to the current branch leaf, use:",
        "```bash\ncurl -sS \"$TOPICLAB_BASE_URL/api/v1/internal/arcade/topics/$TOPIC_ID/branches/$BRANCH_ROOT_POST_ID/evaluate\" \\\n  -H \"Authorization: Bearer $ADMIN_PANEL_TOKEN\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{\n    \"for_post_id\": \"'\"$SUBMISSION_POST_ID\"'\",\n    \"body\": \"Reviewer feedback here.\",\n    \"result\": {\n      \"passed\": true,\n      \"score\": 0.78,\n      \"feedback\": \"Structured feedback here.\"\n    }\n  }'\n```",
        "Runnable cabinets should normally be handled by `arcade_reviewer.py`. Text-only or engagement-driven cabinets may rely on manual review instead.",
        "## Reviewer deployment",
        "Merge to `main` can deploy reviewer changes to a self-hosted host. The deployment workflow rebuilds generated assets, validates the repo, runs unit tests, syncs the repo into the configured deploy directory, and restarts the `systemd` reviewer service.",
        "See [docs/reviewer-deployment.md](docs/reviewer-deployment.md) for the host contract and [deploy/systemd/clawarcade-reviewer.service](deploy/systemd/clawarcade-reviewer.service) for the service template.",
        "## Workflow overview",
        "```mermaid\nflowchart TD\n    A[\"Contributor or Agent\"] --> B[\"Create or edit cabinet.yaml\"]\n    B --> C[\"Optional: run scripts/new_cabinet.py\"]\n    B --> D[\"Run scripts/build_cabinets.py\"]\n    C --> D\n    D --> E[\"Generate cabinet README.md\"]\n    D --> F[\"Generate topiclab.meta.zh.json\"]\n    D --> G[\"Generate topiclab.meta.en.json\"]\n    D --> H[\"Generate root README.md\"]\n    D --> I[\"Run scripts/validate_cabinets.py\"]\n    I --> J{\"Valid?\"}\n    J -- No --> B\n    J -- Yes --> K[\"Open PR\"]\n    K --> L[\"CI validates schema and generated outputs\"]\n    L --> M[\"Merge\"]\n    M --> N[\"Reviewer uses generated topiclab.meta and README\"]\n```",
        "The `scripts/new_cabinet.py` step is optional because it is only a scaffold helper. Use it when you are creating a brand-new cabinet directory and want a starter `cabinet.yaml`. Skip it when you are editing an existing cabinet or when you prefer to create `cabinet.yaml` manually.",
        "## Contributing",
        "Cabinets are authored through `cabinet.yaml`, and each family also keeps a `family.yaml` for family-level docs. The generated `README.md`, `topiclab.meta.*.json`, and `generated/reviewer_registry.json` files should not be edited by hand.",
        "Runnable cabinets must declare machine-readable runtime fields under `review.runtime`. `community_engagement` and `manual` cabinets are documented and published, but they are not executed by the local reviewer service.",
        "Typical contribution paths:",
        "1. Path A: update an existing cabinet by editing its `cabinet.yaml`, then run `python3 scripts/build_cabinets.py`, `python3 scripts/validate_cabinets.py`, and open a PR.",
        "2. Path B: scaffold a brand-new cabinet with `python3 scripts/new_cabinet.py <family> <slug> --title \"Your Title\"`, fill in `cabinet.yaml`, then build, validate, and open a PR.",
        "3. Path C: open an issue first for a new cabinet idea, and let a maintainer or agent turn that proposal into a PR that follows the same build and validate flow.",
        "```bash\npython3 scripts/build_cabinets.py\npython3 scripts/validate_cabinets.py\n```",
        "For scaffolding and contribution conventions, see [CONTRIBUTING.md](CONTRIBUTING.md). For the full process, roles, and examples, see [docs/contribution-workflow.md](docs/contribution-workflow.md). For deployment details, see [docs/reviewer-deployment.md](docs/reviewer-deployment.md).",
    ]
    return "\n\n".join(blocks).strip() + "\n"


def render_reviewer_registry(cabinets: list[dict[str, Any]]) -> str:
    entries: dict[str, dict[str, Any]] = {}
    for cabinet in cabinets:
        review = cabinet.get("review", {})
        if review.get("mode") != "local_subprocess":
            continue
        validator_config = cabinet["topiclab"]["shared"]["validator"]["config"]
        source = validator_config["source"]
        entries[source] = {
            "cabinet_id": cabinet["cabinet"]["id"],
            "cabinet_title": cabinet["cabinet"]["title"],
            "family": cabinet["cabinet"]["family"],
            "review_mode": review["mode"],
            "reviewer_entry": review["reviewer_entry"],
            "runtime": review["runtime"],
        }

    payload = {
        "schema_version": 1,
        "cabinets": dict(sorted(entries.items())),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_if_changed(path: Path, content: str, check: bool) -> bool:
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    changed = existing != content
    if changed and not check:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return changed


def build(check: bool = False) -> int:
    cabinets = load_all_cabinets()
    family_configs = load_family_configs({cabinet["cabinet"]["family"] for cabinet in cabinets})
    changed_paths: list[Path] = []

    for cabinet in cabinets:
        cabinet_dir: Path = cabinet["_cabinet_dir"]
        targets = {
            cabinet_dir / "README.md": render_cabinet_readme(cabinet),
            cabinet_dir / "topiclab.meta.zh.json": render_topiclab_meta(cabinet, "zh"),
            cabinet_dir / "topiclab.meta.en.json": render_topiclab_meta(cabinet, "en"),
        }
        for path, content in targets.items():
            if write_if_changed(path, content, check):
                changed_paths.append(path)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cabinet in cabinets:
        grouped[cabinet["cabinet"]["family"]].append(cabinet)
    for family, family_cabinets in grouped.items():
        family_readme = CABINETS_ROOT / family / "README.md"
        if write_if_changed(family_readme, render_family_readme(family_configs[family]), check):
            changed_paths.append(family_readme)

    root_readme = REPO_ROOT / "README.md"
    if write_if_changed(root_readme, render_root_readme(cabinets, family_configs), check):
        changed_paths.append(root_readme)

    registry_path = reviewer_registry_path()
    if write_if_changed(registry_path, render_reviewer_registry(cabinets), check):
        changed_paths.append(registry_path)

    if check and changed_paths:
        print("Generated files are out of date:", file=sys.stderr)
        for path in changed_paths:
            print(f"- {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate README and TopicLab payloads from cabinet.yaml sources.")
    parser.add_argument("--check", action="store_true", help="Fail if generated files are not up to date.")
    args = parser.parse_args()
    return build(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
