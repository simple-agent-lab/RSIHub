from __future__ import annotations

import functools
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import typer
from dotenv import dotenv_values

from .archive import archive_path, merged_rows, verify_integrity
from .candidate.smoke import run_candidate_smoke
from .composition.cli import build_recipe_app
from .config import DEFAULT_RECIPE, RECIPE_NAMES, experiment_int
from .doctor import DoctorProfile, run_doctor
from .driver import RunOptions
from .driver import repair as repair_workspace
from .driver import run as driver_run
from .experiment_smoke import run_experiment_smoke
from .git import head_tag, working_tree_changed_paths
from .operator_cli import attach_orchestration_commands
from .orchestration import commit_agent_child, eval_agent_child, fork_agent_child, record_agent_fields
from .population import best_row, fixed_evaluation_identity
from .report import format_report, format_status
from .run_summary import assert_run_success, write_run_summary
from .surface import check_paths, surface_patterns
from .workspace import InitOptions, init_workspace

app = typer.Typer(add_completion=False, no_args_is_help=True, help="RSIHub mechanism CLI")
DEFAULT_WORKSPACE = Path("~/.evolve-workspace")


def _enable_live_output(enabled: bool) -> None:
    if enabled:
        os.environ["EVOLVE_LIVE_OUTPUT"] = "1"


@contextmanager
def _workspace_environment(workspace: Path) -> Iterator[None]:
    workspace_env = workspace.resolve() / ".env"
    added: list[str] = []
    try:
        for name, value in dotenv_values(workspace_env).items():
            if value is not None and name not in os.environ:
                os.environ[name] = value
                added.append(name)
        yield
    finally:
        for name in reversed(added):
            os.environ.pop(name, None)


def _guard(fn):
    """Wrap a command so any error prints `evolve: <error>` and exits 1, while
    Typer/Click control-flow exceptions (Exit, usage errors like BadParameter)
    pass through unchanged."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.BadParameter):  # control flow: exit code / usage error
            raise
        except Exception as exc:
            print(f"evolve: {exc}", file=sys.stderr)
            raise typer.Exit(1) from exc

    return wrapper


app.add_typer(build_recipe_app(_guard), name="recipe")
attach_orchestration_commands(app, _guard, _workspace_environment, _enable_live_output)


@app.command()
@_guard
def init(
    workspace: Path | None = typer.Argument(
        None,
        help="workspace directory (default: ~/.evolve-workspace)",
    ),
    recipe: str | None = typer.Option(
        None,
        help=f"supported public recipe to scaffold (default: {DEFAULT_RECIPE})",
    ),
    recipe_path: Path | None = typer.Option(
        None,
        "--recipe-path",
        help="opt-in recipe directory or evolve.yaml path",
    ),
    seed: str | None = typer.Option(None, help="git URL to vendor into target/; local target dir; builtin-codex"),
    dataset: str | None = typer.Option(None, help="local Harbor task directory to split and freeze"),
    tasks: int | None = typer.Option(None, "--tasks", min=1, help="limit evaluator tasks per round"),
) -> None:
    """Scaffold a new RSIHub workspace."""
    workspace = (workspace or DEFAULT_WORKSPACE).expanduser()
    if recipe is not None and recipe_path is not None:
        raise typer.BadParameter(
            "cannot combine --recipe with --recipe-path",
            param_hint="--recipe-path",
        )
    selected_recipe = recipe or DEFAULT_RECIPE
    if recipe_path is None and selected_recipe not in RECIPE_NAMES:
        raise typer.BadParameter(
            f"invalid choice: {selected_recipe!r} (choose from {', '.join(RECIPE_NAMES)})",
            param_hint="--recipe",
        )
    init_workspace(
        InitOptions(
            workspace=workspace,
            recipe=selected_recipe if recipe_path is None else None,
            seed=seed,
            dataset=dataset,
            recipe_path=recipe_path,
            tasks_per_round=tasks,
        )
    )
    print(f"Initialized RSIHub workspace at {workspace}")


@app.command()
@_guard
def preflight(
    workspace: Path | None = typer.Argument(
        None,
        help="workspace directory (default: ~/.evolve-workspace)",
    ),
    recipe: str | None = typer.Option(
        None,
        help=f"supported public recipe to scaffold (default: {DEFAULT_RECIPE})",
    ),
    recipe_path: Path | None = typer.Option(
        None,
        "--recipe-path",
        help="opt-in recipe directory or evolve.yaml path",
    ),
    seed: str | None = typer.Option(None, help="git URL to vendor into target/; local target dir; builtin-codex"),
    dataset: str | None = typer.Option(None, help="local Harbor task directory to split and freeze"),
    tasks: int | None = typer.Option(None, "--tasks", min=1, help="limit evaluator tasks per round"),
    smoke: bool = typer.Option(
        False, "--smoke", help="include one isolated model request for an initialized workspace"
    ),
    init_check: bool = typer.Option(
        False,
        "--init-check",
        help="check prospective evolve init inputs even when the workspace is initialized",
    ),
) -> None:
    """Check prospective init inputs or validate an initialized workspace.

    Uninitialized paths and init-specific options use the read-only init
    checklist. An initialized workspace uses the typed runtime preflight; pass
    ``--smoke`` to include exactly one model-backed task.
    """
    from .preflight import (
        PreflightMode,
        PreflightStatus,
        render_init_preflight,
        run_init_preflight,
    )
    from .preflight import (
        run_preflight as run_runtime_preflight,
    )

    selected_workspace = (workspace or DEFAULT_WORKSPACE).expanduser()
    initialized = (selected_workspace / "evolve.yaml").is_file() and (selected_workspace / ".git").exists()
    init_options_supplied = any(value is not None for value in (recipe, recipe_path, seed, dataset, tasks))
    if initialized and not init_check and not init_options_supplied:
        mode = PreflightMode.SMOKE if smoke else PreflightMode.ORDINARY
        with _workspace_environment(selected_workspace):
            result = run_runtime_preflight(selected_workspace, mode=mode)
        receipt = result.receipt_path.resolve() if result.receipt_path is not None else "unwritten"
        print(f"preflight: {result.status.value} receipt={receipt}")
        if result.status is PreflightStatus.FAILED:
            raise typer.Exit(2 if smoke else 1)
        return
    if smoke:
        raise typer.BadParameter(
            "--smoke requires an initialized workspace without init-specific options",
            param_hint="--smoke",
        )

    checks = run_init_preflight(
        workspace=selected_workspace,
        recipe=recipe,
        recipe_path=recipe_path,
        seed=seed,
        dataset=dataset,
        tasks_per_round=tasks,
    )
    output, ready = render_init_preflight(checks)
    print(output)
    if not ready:
        raise typer.Exit(1)


@app.command()
@_guard
def run(
    workspace: Path,
    max_generations: int | None = typer.Option(None, "--max-generations"),
    children_per_gen: int | None = typer.Option(None, "--children-per-gen"),
    resume: bool = typer.Option(False, "--resume", help="accepted no-op; resume is the default"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="stream evaluator and operator output"),
    assert_success: bool = typer.Option(
        True,
        "--assert-success/--no-assert-success",
        help="fail unless every requested generation reached a recipe-valid terminal state",
    ),
) -> None:
    """Start or resume the driver, the unattended evolution loop."""
    gens = max_generations if max_generations is not None else experiment_int(workspace, "max_generations", 40)
    children = children_per_gen if children_per_gen is not None else experiment_int(workspace, "children_per_gen", 1)
    _enable_live_output(verbose)
    with _workspace_environment(workspace):
        driver_run(RunOptions(workspace=workspace, max_generations=gens, children_per_gen=children))
    summary, summary_path = write_run_summary(workspace, through=gens)
    print(f"Evolution loop stopped at generation {summary['completed_through']}; summary: {summary_path}")
    if assert_success and summary["status"] != "passed":
        details = "; ".join(str(finding) for finding in summary["findings"][:5])
        raise RuntimeError(f"run assertion failed: {details}")


@app.command("assert-run")
@_guard
def assert_run_cmd(
    workspace: Path = typer.Argument(Path(".")),
    through: int = typer.Option(..., "--through", min=0),
) -> None:
    """Assert recipe-aware completion and write runs/run-summary.json."""
    summary = assert_run_success(workspace, through=through)
    print(f"run assertion passed through generation {summary['completed_through']}")


@app.command()
@_guard
def fork(workspace: Path, parent: str, child_worktree: Path) -> None:
    """Create a child worktree from a parent generation."""
    fork_agent_child(workspace, parent, child_worktree)
    print(child_worktree)


@app.command()
@_guard
def commit(
    workspace: Path,
    child_worktree: Path,
    parent: str = typer.Option(..., "--parent"),
    genid: str = typer.Option(..., "--genid"),
) -> None:
    """Commit and tag a child worktree."""
    commit_agent_child(workspace, child_worktree, parent, genid)
    print(f"Committed gen/{genid}")


@app.command("eval")
@_guard
def eval_cmd(
    workspace: Path,
    genid: str,
    force: bool = typer.Option(False, "--force", help="re-run evaluation even when a scored row already exists"),
) -> None:
    """Evaluate a tagged child version."""
    with _workspace_environment(workspace):
        record = eval_agent_child(workspace, genid, force=force)
    print(f"Evaluated gen/{genid}" if record is not None else f"Skipped gen/{genid}; evaluation is already terminal")


@app.command()
@_guard
def retry(workspace: Path, genid: str) -> None:
    """Retry a failed or otherwise terminal evaluation as a new certified attempt."""
    with _workspace_environment(workspace):
        record = eval_agent_child(workspace, genid, force=True)
    if record is None:
        raise RuntimeError(f"gen/{genid} could not be retried")
    print(f"Retried gen/{genid}: {record.outcome.value} (attempt {record.attempt})")


@app.command()
@_guard
def record(
    workspace: Path,
    genid: str,
    fields: str = typer.Option(..., "--fields", help="JSON object of fields"),
) -> None:
    """Append non-stamped archive fields."""
    record_agent_fields(workspace, genid, json.loads(fields))
    print(f"Recorded fields for gen/{genid}")


@app.command("surface-check")
@_guard
def surface_check(
    workspace: Path = typer.Argument(Path(".")),
    parent: str | None = typer.Option(None, "--parent"),
) -> None:
    """Report pending out-of-surface edits."""
    include, exclude = surface_patterns(workspace)
    parent_ref = f"gen/{parent}" if parent and not parent.startswith("gen/") else parent
    parent_ref = parent_ref or head_tag(workspace) or "gen/0"
    mutated = working_tree_changed_paths(workspace, parent_ref)
    violations = check_paths(mutated, include, exclude)
    print({"ok": not violations, "mutated": mutated, "violations": violations})
    if violations:
        raise typer.Exit(1)


@app.command("candidate-smoke")
@_guard
def candidate_smoke(
    full: bool = typer.Option(False, "--full"),
    checkout: Path = typer.Option(Path("."), "--checkout"),
) -> None:
    """Run the evaluator-provided full smoke against an exact candidate snapshot."""
    if not full:
        raise typer.BadParameter("--full is required", param_hint="--full")
    checkout = checkout.resolve()
    workspace = Path(os.environ.get("EVOLVE_WORKSPACE", checkout)).resolve()
    with _workspace_environment(workspace):
        result = run_candidate_smoke(checkout, workspace=workspace)
    tail = result.stderr_path.read_text().splitlines()[-200:]
    if tail:
        print("\n".join(tail), file=sys.stderr)
    print(
        f"candidate-smoke: {result.status} tree={result.snapshot_tree} "
        f"result={(result.attempt_dir / 'result.json').resolve()} "
        f"stdout={result.stdout_path.resolve()} stderr={result.stderr_path.resolve()}"
    )
    if result.status == "failed":
        raise typer.Exit(2)
    if result.status == "unsupported":
        raise typer.Exit(3)


@app.command("smoke")
@_guard
def smoke(
    workspace: Path = typer.Argument(Path(".")),
    profile: str = typer.Option("experiment", "--profile", help="local or experiment"),
    task: str | None = typer.Option(None, "--task"),
) -> None:
    """Run a local candidate check or an isolated full-loop experiment canary."""
    if profile == "local":
        with _workspace_environment(workspace):
            result = run_candidate_smoke(workspace.resolve(), workspace=workspace.resolve())
        print(f"local smoke: {result.status} result={result.attempt_dir / 'result.json'}")
        if result.status != "passed":
            raise typer.Exit(2 if result.status == "failed" else 3)
        return
    if profile != "experiment":
        raise typer.BadParameter("choose local or experiment", param_hint="--profile")
    with _workspace_environment(workspace):
        result = run_experiment_smoke(workspace, task=task)
    print(f"experiment smoke: {result.status} task={result.task} workspace={result.workspace}")
    print(f"result: {result.result_path}")
    if result.status != "passed":
        raise typer.Exit(2)


@app.command()
@_guard
def status(workspace: Path = typer.Argument(Path("."))) -> None:
    """Show current population and best-ever score."""
    print(format_status(workspace), end="")


@app.command()
@_guard
def report(workspace: Path = typer.Argument(Path("."))) -> None:
    """Write an experiment report and research-claim checklist."""
    print(format_report(workspace), end="")


@app.command("doctor")
@_guard
def doctor_profile(
    workspace: Path = typer.Argument(Path(".")),
    profile: str = typer.Option("experiment", "--profile", help="local or experiment"),
    probe_model: bool = typer.Option(False, "--probe-model"),
) -> None:
    """Run read-only local or long-running experiment preflight checks."""
    if profile not in {"local", "experiment"}:
        raise typer.BadParameter("choose local or experiment", param_hint="--profile")
    report = run_doctor(workspace, profile=cast(DoctorProfile, profile), probe_model=probe_model)
    for check in report.checks:
        print(f"{check.status.upper():4} {check.name}: {check.detail}")
    print(f"doctor report: {report.report_path}")
    if not report.healthy:
        raise typer.Exit(2)


@app.command()
@_guard
def view(
    workspace: Path = typer.Argument(Path(".")),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: str = typer.Option("8080-8089", "--port"),
    catalog: Path | None = typer.Option(None, "--catalog", help="YAML catalog of experiment workspaces"),
) -> None:
    """Browse one workspace or a catalog of experiments without modifying them."""
    from .viewer import run_catalog_viewer, run_viewer

    if catalog is not None:
        run_catalog_viewer(catalog, host, port)
    else:
        run_viewer(workspace, host, port)


@app.command()
@_guard
def repair(workspace: Path = typer.Argument(Path("."))) -> None:
    """Explicitly repair interrupted state such as stale child worktrees."""
    actions = repair_workspace(workspace)
    for action in actions:
        print(action)
    print("repair: healthy" if not actions else f"repair: completed/observed {len(actions)} item(s)")


@app.command()
@_guard
def verify(workspace: Path = typer.Argument(Path("."))) -> None:
    """Integrity fsck: recompute the champion and expose any hand-edited archive."""
    findings = verify_integrity(workspace)
    identity_unverifiable = fixed_evaluation_identity(workspace.resolve()) is None
    champion = best_row(workspace)
    best_ever_path = workspace / "best_ever.json"
    try:
        materialized = json.loads(best_ever_path.read_text())
    except (OSError, json.JSONDecodeError):
        materialized = object()
    if materialized != champion:
        findings.append("best_ever.json does not match the mechanism-derived champion")
    for finding in findings:
        print(f"TAMPER: {finding}", file=sys.stderr)
    champ = f"gen {champion['genid']} score {champion.get('score')}" if champion else "none"
    print(f"champion: {champ}")
    if identity_unverifiable:
        print(
            "UNVERIFIABLE: fixed evaluation identity is unavailable; legacy v1 local split manifests "
            "require a new experiment with a v2 split manifest",
            file=sys.stderr,
        )
    failed = bool(findings) or identity_unverifiable
    print(f"rows: {len(merged_rows(archive_path(workspace)))}  integrity: {'FAIL' if failed else 'ok'}")
    if failed:
        raise typer.Exit(1)


def main(argv: list[str] | None = None) -> int:
    """Entry point: translate Click/Typer's SystemExit into an int exit code so
    `python -m evolve` (SystemExit(main())) and the console script agree."""
    try:
        app(args=argv)
    except SystemExit as exit_:
        code = exit_.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0
