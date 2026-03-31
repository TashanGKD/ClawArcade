#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = "generated/reviewer_registry.json"
DEFAULT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class SmokeProbe:
    name: str
    source: str
    description: str
    command_builder: Callable[[ModuleType, Path], list[str]]
    validator: Callable[[subprocess.CompletedProcess[str]], tuple[bool, str | None]]


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_reviewer_module(repo_root: Path) -> ModuleType:
    return load_module("clawarcade_reviewer_smoke_runtime", repo_root / "arcade_reviewer.py")


def validate_exit_code_only(completed: subprocess.CompletedProcess[str]) -> tuple[bool, str | None]:
    if completed.returncode == 0:
        return True, None
    return False, f"command exited with code {completed.returncode}"


def validate_variable_star_probe(completed: subprocess.CompletedProcess[str]) -> tuple[bool, str | None]:
    if completed.returncode != 0:
        return False, f"command exited with code {completed.returncode}"
    stdout_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not stdout_lines or stdout_lines[-1] != "SUCCESS":
        return False, "stdout must end with SUCCESS"
    return True, None


def build_cifar_env_probe_command(reviewer: ModuleType, cabinet_dir: Path) -> list[str]:
    return [
        *reviewer.resolve_cabinet_python(cabinet_dir),
        "-c",
        (
            "import torch, torchvision; "
            "print('torch=' + torch.__version__); "
            "print('torchvision=' + torchvision.__version__)"
        ),
    ]


def build_variable_star_probe_command(reviewer: ModuleType, cabinet_dir: Path) -> list[str]:
    return [
        *reviewer.resolve_cabinet_python(cabinet_dir),
        "evaluate_submission.py",
        "--submission",
        "forum_post_template.txt",
    ]


def get_default_probes() -> tuple[SmokeProbe, ...]:
    return (
        SmokeProbe(
            name="101-cifar-env",
            source="cabinets/turing-teahouse/101-CIFAR",
            description="run cabinet setup and verify torch/torchvision imports from the resolved runtime",
            command_builder=build_cifar_env_probe_command,
            validator=validate_exit_code_only,
        ),
        SmokeProbe(
            name="102-variable-star-evaluator",
            source="cabinets/citizen-science-harbor/102-variable-star-citizen-science",
            description="run cabinet setup and verify the bundled evaluator accepts the example submission",
            command_builder=build_variable_star_probe_command,
            validator=validate_variable_star_probe,
        ),
    )


def format_preview(text: str, *, max_chars: int = 240) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return "-"
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars]}..."


def run_probe(
    probe: SmokeProbe,
    *,
    reviewer: ModuleType,
    repo_root: Path,
    registry: dict[str, dict[str, object]],
    timeout: int,
) -> dict[str, object]:
    entry = registry.get(probe.source)
    if entry is None:
        return {
            "name": probe.name,
            "source": probe.source,
            "passed": False,
            "stage": "registry",
            "reason": "registry entry missing",
        }

    try:
        setup_error = reviewer.ensure_setup_commands(
            repo_root=repo_root,
            registry_entry=entry,
            cabinet_source=probe.source,
            timeout=timeout,
        )
        if setup_error is not None:
            _, result = setup_error
            return {
                "name": probe.name,
                "source": probe.source,
                "passed": False,
                "stage": "setup",
                "reason": result.get("runtime_error_reason") or result.get("format_error_reason") or "setup failed",
                "command": result.get("command_executed"),
                "duration_seconds": result.get("duration_seconds"),
                "exit_code": result.get("exit_code"),
                "stderr_tail": result.get("stderr_tail") or [],
            }

        runtime = entry.get("runtime") or {}
        cabinet_dir = repo_root / str(runtime.get("cwd") or "")
        command = probe.command_builder(reviewer, cabinet_dir)
        started_at = time.time()
        completed = subprocess.run(
            command,
            cwd=str(cabinet_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = round(time.time() - started_at, 3)
        passed, reason = probe.validator(completed)
        return {
            "name": probe.name,
            "source": probe.source,
            "passed": passed,
            "stage": "probe",
            "reason": reason or "",
            "command": " ".join(command),
            "duration_seconds": duration,
            "exit_code": completed.returncode,
            "stdout_preview": format_preview(completed.stdout, max_chars=240),
            "stderr_tail": reviewer.truncate_stderr(completed.stderr),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": probe.name,
            "source": probe.source,
            "passed": False,
            "stage": "exception",
            "reason": str(exc),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deployment smoke tests for representative reviewer cabinets.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="ClawArcade repository root")
    parser.add_argument("--registry-path", default=DEFAULT_REGISTRY_PATH, help="Reviewer registry path, relative to repo root by default")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-probe timeout in seconds")
    parser.add_argument("--list-probes", action="store_true", help="List available smoke probes and exit")
    parser.add_argument("--probe", action="append", default=[], help="Only run the named probe; may be repeated")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo_root = Path(args.repo_root).resolve()
    registry_path = Path(args.registry_path)
    if not registry_path.is_absolute():
        registry_path = repo_root / registry_path

    probes = {probe.name: probe for probe in get_default_probes()}
    if args.list_probes:
        for probe in probes.values():
            print(f"{probe.name}: {probe.source} - {probe.description}")
        return 0

    selected_names = args.probe or list(probes.keys())
    unknown_names = [name for name in selected_names if name not in probes]
    if unknown_names:
        raise SystemExit(f"unknown smoke probe(s): {', '.join(sorted(unknown_names))}")

    reviewer = load_reviewer_module(repo_root)
    reviewer._close_daily_log_file()
    registry = reviewer.load_reviewer_registry(registry_path)

    failures = 0
    for name in selected_names:
        probe = probes[name]
        print(f"[reviewer-smoke] start probe={probe.name} source={probe.source} desc={probe.description}", flush=True)
        result = run_probe(
            probe,
            reviewer=reviewer,
            repo_root=repo_root,
            registry=registry,
            timeout=max(1, int(args.timeout)),
        )
        if result.get("passed"):
            print(
                "[reviewer-smoke] pass "
                f"probe={probe.name} source={probe.source} duration={result.get('duration_seconds')!r} "
                f"command={format_preview(str(result.get('command') or '-'))} "
                f"stdout={result.get('stdout_preview') or '-'}",
                flush=True,
            )
            continue
        failures += 1
        stderr_tail = result.get("stderr_tail") or []
        stderr_summary = stderr_tail[-1] if isinstance(stderr_tail, list) and stderr_tail else "-"
        print(
            "[reviewer-smoke] fail "
            f"probe={probe.name} source={probe.source} stage={result.get('stage')} "
            f"reason={format_preview(str(result.get('reason') or '-'))} "
            f"exit_code={result.get('exit_code')!r} duration={result.get('duration_seconds')!r} "
            f"command={format_preview(str(result.get('command') or '-'))} "
            f"stderr_tail={format_preview(str(stderr_summary))}",
            flush=True,
        )

    passed = len(selected_names) - failures
    print(f"[reviewer-smoke] summary passed={passed} failed={failures} total={len(selected_names)}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
