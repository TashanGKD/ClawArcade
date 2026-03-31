#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_PATH = SCRIPT_DIR / "build_cabinets.py"


def load_builder_module():
    spec = importlib.util.spec_from_file_location("build_cabinets", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load build_cabinets from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_cabinets"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate cabinet.yaml sources and generated outputs.")
    parser.add_argument(
        "--skip-generated-check",
        action="store_true",
        help="Validate schema and repo rules only; skip generated file freshness checks.",
    )
    args = parser.parse_args()

    builder = load_builder_module()
    cabinets = builder.load_all_cabinets()
    builder.load_family_configs({cabinet["cabinet"]["family"] for cabinet in cabinets})
    if args.skip_generated_check:
        return 0
    return builder.build(check=True)


if __name__ == "__main__":
    raise SystemExit(main())
