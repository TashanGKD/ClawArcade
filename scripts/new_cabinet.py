#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CABINETS_ROOT = REPO_ROOT / "cabinets"
TEMPLATE_PATH = REPO_ROOT / "templates" / "cabinet.template.yaml"


def slug_to_title(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-"))


def scaffold_cabinet(family: str, slug: str, *, title: str = "", summary: str = "Fill in a one-line summary.") -> Path:
    cabinet_dir = CABINETS_ROOT / family / slug
    cabinet_dir.mkdir(parents=True, exist_ok=True)
    cabinet_path = cabinet_dir / "cabinet.yaml"
    if cabinet_path.exists():
        raise SystemExit(f"{cabinet_path} already exists")

    resolved_title = title.strip() or slug_to_title(slug)
    content = TEMPLATE_PATH.read_text(encoding="utf-8")
    content = content.replace("example-family", family)
    content = content.replace("example-cabinet", slug)
    content = content.replace("Example Cabinet", resolved_title)
    content = content.replace("One-line summary for repository lists and generated docs.", summary)
    cabinet_path.write_text(content, encoding="utf-8")
    return cabinet_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a new cabinet.yaml from the repository template.")
    parser.add_argument("family", help="Family directory under cabinets/, for example turing-teahouse")
    parser.add_argument("slug", help="Cabinet directory name, for example 102-new-task")
    parser.add_argument("--title", default="", help="Repository-facing cabinet title")
    parser.add_argument("--summary", default="Fill in a one-line summary.", help="Repository-facing one-line summary")
    args = parser.parse_args()

    cabinet_path = scaffold_cabinet(
        args.family,
        args.slug,
        title=args.title,
        summary=args.summary,
    )
    print(cabinet_path.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
