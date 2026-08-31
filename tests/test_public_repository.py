import re
import shlex
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from evolve import __version__

ROOT = Path(__file__).resolve().parents[1]
RELATIVE_LINK = re.compile(r"\[[^\]]+\]\((?!https?://|mailto:|#)([^)#]+)")
SVG_NS = {"svg": "http://www.w3.org/2000/svg"}


def maintained_current_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "ARCHITECTURE.md", ROOT / "CONTRIBUTING.md", ROOT / "QUICKSTART.md"]
    files.extend(path for path in (ROOT / "docs").rglob("*.md") if "superpowers" not in path.parts)
    files.extend((ROOT / "library").rglob("*.md"))
    files.extend((ROOT / "library").rglob("*.py"))
    files.extend((ROOT / "recipes").rglob("README.md"))
    for root in (ROOT / "scaffolds", ROOT / "skills"):
        files.extend(
            path for path in root.rglob("*") if path.is_file() and path.suffix in {".md", ".py", ".sh", ".yaml"}
        )
    files.extend((ROOT / "src" / "evolve").rglob("*.py"))
    return sorted(set(files))


def test_tracked_files_use_only_current_project_identity() -> None:
    retired = ("evolve" + "x", "simple-" + "evolve-agent")
    standalone = "Evo" + "lve"
    allowed_standalone_uses = {
        "README.md": (f"What Can {standalone}",),
        "evals/skills/make-paper-poster/recipe/evaluator/doctor_smoke.py": (f">{standalone}<",),
        "library/PROTOCOL.md": (f"{standalone} freely",),
        "skills/evolve-agent/agents/openai.yaml": (f"{standalone} agents",),
        "src/evolve/viewer/app.py": (
            f"X-{standalone}-Artifact-Truncated",
            f"X-{standalone}-Diff-Base",
        ),
    }
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = [Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]
    stale: list[str] = []
    for relative in paths:
        folded_path = relative.as_posix().casefold()
        for identity in retired:
            if identity in folded_path:
                stale.append(f"path:{relative}")
        try:
            text = (ROOT / relative).read_text()
        except UnicodeDecodeError:
            continue
        folded_text = text.casefold()
        for identity in retired:
            if identity in folded_text:
                stale.append(f"text:{relative}")
        for line_number, line in enumerate(text.splitlines(), start=1):
            without_method_name = line.replace(f"A-{standalone}", "")
            if not re.search(rf"\b{standalone}\b", without_method_name):
                continue
            allowed = allowed_standalone_uses.get(relative.as_posix(), ())
            if not any(phrase in line for phrase in allowed):
                stale.append(f"semantic:{relative}:{line_number}")
    assert sorted(set(stale)) == []


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_architecture_visual_uses_identity_palette() -> None:
    svg = (ROOT / "docs" / "assets" / "architecture.svg").read_text()
    for color in ("#10372e", "#19785a", "#65ce9f", "#b5d3c7", "#f2fbf7"):
        assert color in svg
    description = ET.parse(ROOT / "docs" / "assets" / "architecture.svg").find("svg:desc", SVG_NS).text
    assert "Recipes select permitted targets, operators, and stages." in description
    assert "rewrite any stage" not in description


def test_readme_visual_assets_have_accessible_svg_metadata() -> None:
    for relative in (
        "docs/rsihub-mark.svg",
        "docs/rsihub-wordmark.svg",
        "docs/rsihub-lockup.svg",
        "docs/evolve-lineage.svg",
    ):
        root = ET.parse(ROOT / relative).getroot()
        assert root.attrib["role"] == "img"
        assert root.attrib["viewBox"]
        labelled_by = root.attrib["aria-labelledby"].split()
        assert len(labelled_by) == 2
        assert root.find("svg:title", SVG_NS).attrib["id"] == labelled_by[0]
        assert root.find("svg:desc", SVG_NS).attrib["id"] == labelled_by[1]


def test_branding_assets_match_approved_ring_and_wordmark() -> None:
    mark_path = ROOT / "docs" / "rsihub-mark.svg"
    mark = ET.parse(mark_path).getroot()
    mark_text = mark_path.read_text()
    paths = mark.findall("svg:path", SVG_NS)
    assert mark.attrib["width"] == "128"
    assert mark.attrib["height"] == "128"
    assert mark.attrib["viewBox"] == "0 0 40 40"
    # The ramp is ordered so the two near-identical blues sit opposite each other
    # rather than adjacent; on a ring, #3c8cff beside #0095fd reads as one lump.
    assert [path.attrib["stroke"] for path in paths] == [
        "#3c8cff",
        "#00cbd4",
        "#0095fd",
        "#78e85c",
    ]
    assert all(path.attrib["stroke-width"] == "6" for path in paths)
    assert all(path.attrib["stroke-linecap"] == "round" for path in paths)
    # Each arc spans 42.6°, leaving a 47.4° geometric gap. A round cap extends
    # stroke-width/2 along the tangent past the endpoint — 12.7° at this radius —
    # so the gap measures 22° on screen, which is the number the design specifies.
    assert [path.attrib["d"] for path in paths] == [
        "M25.43 7.64 A13.5 13.5 0 0 1 32.36 14.57",
        "M32.36 25.43 A13.5 13.5 0 0 1 25.43 32.36",
        "M14.57 32.36 A13.5 13.5 0 0 1 7.64 25.43",
        "M7.64 14.57 A13.5 13.5 0 0 1 14.57 7.64",
    ]
    assert "selected lineage" not in mark_text.casefold()

    wordmark_path = ROOT / "docs" / "rsihub-wordmark.svg"
    wordmark_text = wordmark_path.read_text()
    wordmark = ET.parse(wordmark_path).getroot()
    assert "<text" not in wordmark_text
    assert "font-family" not in wordmark_text
    glyph_r = wordmark.find(".//svg:path[@id='glyph-r']", SVG_NS)
    assert glyph_r is not None
    assert glyph_r.attrib["d"].startswith("M58.594 0V-704.59")
    assert wordmark.find(".//svg:clipPath", SVG_NS) is None
    assert wordmark.find(".//svg:mask", SVG_NS) is None
    hub_word = wordmark.find(".//svg:path[@id='hub-word']", SVG_NS)
    assert hub_word is not None
    assert hub_word.attrib["fill"] == "url(#hub-ramp)"
    assert hub_word.attrib["fill-rule"] == "nonzero"
    assert hub_word.attrib["d"].count("M") == 3
    assert "m -47.61 -118.164" in hub_word.attrib["d"]
    for color in ("#1f2328", "#e6edf3", "#00a3b0", "#2fa844", "#00cbd4", "#78e85c"):
        assert color in wordmark_text
    assert "@media (prefers-color-scheme: dark)" in wordmark_text

    # The masthead is one lockup, not a mark stacked over a wordmark: two <img>
    # tags align on the text baseline rather than on each other, and GitHub strips
    # the attributes that would correct it. Both parts stay on disk — mkdocs takes
    # the mark for its logo and favicon.
    readme = (ROOT / "README.md").read_text()
    assert 'src="docs/rsihub-lockup.svg"' in readme
    assert (ROOT / "docs" / "rsihub-mark.svg").is_file()
    assert (ROOT / "docs" / "rsihub-wordmark.svg").is_file()

    lockup = ET.parse(ROOT / "docs" / "rsihub-lockup.svg").getroot()
    lockup_text = (ROOT / "docs" / "rsihub-lockup.svg").read_text()
    assert [path.attrib["stroke"] for path in lockup.findall("svg:path", SVG_NS)] == [
        "#3c8cff",
        "#00cbd4",
        "#0095fd",
        "#78e85c",
    ]
    assert lockup.find(".//svg:path[@id='hub-word']", SVG_NS) is not None
    assert "@media (prefers-color-scheme: dark)" in lockup_text
    assert '<h1 align="center">RSIHub</h1>' not in readme

    mkdocs = (ROOT / "mkdocs.yml").read_text()
    assert "logo: rsihub-mark.svg" in mkdocs
    assert "favicon: rsihub-mark.svg" in mkdocs

    inventory = (ROOT / "docs" / "development" / "documentation.md").read_text()
    assert "RSIHub Ring identity mark" in inventory
    assert "RSIHub gradient wordmark" in inventory


def test_selected_and_explored_graphics_have_three_to_one_contrast() -> None:
    expected_state_counts = {
        "docs/evolve-lineage.svg": {"selected": 5, "explored": 4},
    }
    for relative, expected_counts in expected_state_counts.items():
        root = ET.parse(ROOT / relative).getroot()
        background = root.find("svg:rect", SVG_NS).attrib["fill"]
        states = root.findall(".//*[@data-state]")
        assert {
            state: sum(element.attrib["data-state"] == state for element in states)
            for state in ("selected", "explored")
        } == expected_counts
        for element in states:
            state = element.attrib["data-state"]
            color = element.attrib["stroke"]
            ratio = _contrast_ratio(color, background)
            assert ratio >= 3, f"{relative} {state} {color} on {background}: {ratio:.2f}:1"


def test_mkdocs_covers_custom_recipe_operator_and_experiment_workflows() -> None:
    mkdocs = (ROOT / "mkdocs.yml").read_text()
    expected_pages = (
        "docs/guides/custom-recipes.md",
        "docs/guides/recipe-to-experiment.md",
        "docs/reference/environment-variables.md",
        "docs/reference/operators.md",
    )
    for page in expected_pages:
        assert (ROOT / page).is_file()
        assert page.removeprefix("docs/") in mkdocs

    mutate_guide = ROOT / "docs/guides/mutate-operators.md"
    assert mutate_guide.is_file()
    assert "guides/mutate-operators.md" in mkdocs
    assert not (ROOT / "docs/guides/meta-agents.md").exists()

    custom_recipe = (ROOT / expected_pages[0]).read_text()
    assert "--recipe-path" in custom_recipe
    assert "surface:" in custom_recipe
    assert "editable_roots" in custom_recipe
    assert "evolve preflight" in custom_recipe

    experiment = (ROOT / expected_pages[1]).read_text()
    for command in ("evolve init", "./evolve doctor", "./evolve smoke", "./evolve run", "./evolve verify"):
        assert command in experiment

    environment = (ROOT / expected_pages[2]).read_text()
    for variable in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_AUTH_JSON_PATH",
        "EVOLVE_RUNTIME_DIGEST",
        "EVOLVE_HOME",
        "EVOLVE_UV_CACHE_DIR",
        "HTTP_PROXY",
        "NO_PROXY",
    ):
        assert variable in environment
    assert "Environment Variables" in experiment
    assert "environment-checklist" in experiment

    operators = (ROOT / expected_pages[3]).read_text()
    stages = {
        "select": "Select",
        "rollout": "Rollout",
        "analyze": "Analyze",
        "mutate": "Mutate",
        "validate": "Validate",
        "novelty": "Novelty",
        "gate": "Gate",
        "record": "Record",
        "reflect": "Reflect",
    }
    for stage, title in stages.items():
        assert stage in operators
        page = ROOT / "docs" / "reference" / "operators" / f"{stage}.md"
        assert page.is_file()
        assert page.read_text().startswith(f"# {title}\n")
        assert f"- {title}: reference/operators/{stage}.md" in mkdocs

    assert not (ROOT / "docs/reference/trace-analyzers.md").exists()


def test_maintained_public_material_uses_canonical_stage_identifiers() -> None:
    forbidden = (
        re.compile(r"\bmeta[-_ ]agent\b", re.IGNORECASE),
        re.compile(r"\btrace[-_ ]analyzer\b", re.IGNORECASE),
        re.compile(r"\bMetaAgentOperator\b"),
        re.compile(r"\bTraceAnalyzerOperator\b"),
    )
    # recipe.py names retired stages only so rejected legacy recipes receive a
    # precise migration diagnostic; tests/test_recipe_resolution.py exercises
    # that negative compatibility boundary.
    rejection_diagnostic = ROOT / "src" / "evolve" / "composition" / "recipe.py"
    for path in maintained_current_files():
        text = path.read_text()
        if path == rejection_diagnostic:
            for legacy_mapping in ('"trace_analyzer": "analyze"', '"meta_agent": "mutate"'):
                assert text.count(legacy_mapping) == 1
                text = text.replace(legacy_mapping, "")
        assert not [pattern.pattern for pattern in forbidden if pattern.search(text)], path


def test_recipe_operator_blocks_never_use_variant_keys() -> None:
    def variant_paths(value: object, prefix: str = "operators") -> list[str]:
        if isinstance(value, dict):
            found = [f"{prefix}.variant"] if "variant" in value else []
            for key, item in value.items():
                found.extend(variant_paths(item, f"{prefix}.{key}"))
            return found
        if isinstance(value, list):
            return [path for index, item in enumerate(value) for path in variant_paths(item, f"{prefix}[{index}]")]
        return []

    failures: dict[str, list[str]] = {}
    for path in sorted((ROOT / "recipes").glob("*/evolve.yaml")):
        config = yaml.safe_load(path.read_text())
        assert isinstance(config, dict)
        paths = variant_paths(config.get("operators", {}))
        if paths:
            failures[path.relative_to(ROOT).as_posix()] = paths
    assert failures == {}


def test_operator_authoring_uses_only_declarative_config_schemas() -> None:
    forbidden = (
        "_CONFIG_KEYS",
        "validate_config=",
        "from library._shared.config import",
    )
    paths = list((ROOT / "library").rglob("*.py"))
    paths += list((ROOT / "library").rglob("*.md"))
    paths += [ROOT / "docs/guides/custom-recipes.md", ROOT / "docs/reference/operators.md"]

    failures = {
        path.relative_to(ROOT).as_posix(): [token for token in forbidden if token in path.read_text()]
        for path in paths
        if any(token in path.read_text() for token in forbidden)
    }

    assert failures == {}


def test_analyze_pipeline_uses_operator_vocabulary() -> None:
    files = [ROOT / "src" / "evolve" / "trace_analysis.py", ROOT / "src" / "evolve" / "feedback.py"]
    files.extend((ROOT / "library" / "analyze").rglob("*.py"))
    files.append(ROOT / "library" / "mutate" / "aevolve.py")

    forbidden = re.compile(r"\bvariants?\b|selected_variant|Trace Analyzer", re.IGNORECASE)
    failures = {
        path.relative_to(ROOT).as_posix(): sorted(set(forbidden.findall(path.read_text())))
        for path in files
        if forbidden.search(path.read_text())
    }
    assert failures == {}


def test_license_metadata_and_notice_are_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "rsihub"
    assert project["urls"] == {
        "Homepage": "https://simpleagentlab.com/rsihub/",
        "Documentation": "https://simpleagentlab.com/RSIHub/",
        "Repository": "https://github.com/simple-agent-lab/RSIHub",
        "Issues": "https://github.com/simple-agent-lab/RSIHub/issues",
    }
    assert project["version"] == __version__
    assert project["license"] == "Apache-2.0"
    assert "Apache License" in (ROOT / "LICENSE").read_text()
    assert (ROOT / "NOTICE").read_text().startswith("RSIHub\n")


def test_required_public_repository_files_exist() -> None:
    required = (
        "LICENSE",
        "NOTICE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "SUPPORT.md",
        "RELEASING.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        "src/evolve/frozen/config.py",
        "library/__init__.py",
    )
    assert [path for path in required if not (ROOT / path).is_file()] == []
    stages = ("select", "rollout", "analyze", "mutate", "validate", "novelty", "gate", "record", "reflect")
    assert [stage for stage in stages if (ROOT / "library" / stage / "_skeleton.py").exists()] == []


def test_public_markdown_relative_links_resolve() -> None:
    files = [
        ROOT / "README.md",
        ROOT / "QUICKSTART.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "recipes" / "README.md",
        ROOT / "SECURITY.md",
        ROOT / "CODE_OF_CONDUCT.md",
        ROOT / "SUPPORT.md",
        ROOT / "RELEASING.md",
    ]
    broken = []
    for source in files:
        if not source.is_file():
            broken.append(f"missing:{source.relative_to(ROOT)}")
            continue
        for target in RELATIVE_LINK.findall(source.read_text()):
            path = target.strip("<>")
            if not (source.parent / path).resolve().exists():
                broken.append(f"{source.relative_to(ROOT)} -> {target}")
    assert broken == []


def test_lint_ci_checks_lock_lint_format_and_types() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "lint.yml").read_text())
    commands = [step.get("run") for step in workflow["jobs"]["lint"]["steps"] if step.get("run")]
    assert commands == [
        "uv lock --check",
        "uv sync --dev --locked",
        "uv run --frozen ruff check --output-format=github .",
        "uv run --frozen ruff format --check .",
        "uv run --frozen ty check",
    ]


def test_ci_warms_clean_python_312_cache_before_offline_workspace_probes() -> None:
    assert (ROOT / ".python-version").read_text() == "3.12\n"
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())
    job = workflow["jobs"]["test"]
    assert "runner.temp" not in yaml.safe_dump(job.get("env", {}))

    steps = job["steps"]
    setup_uv = next(step for step in steps if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"))
    assert setup_uv["with"]["python-version"] == "3.12"
    assert setup_uv["with"]["enable-cache"] is False

    indexes = {step.get("id"): index for index, step in enumerate(steps)}
    assert (
        indexes["configure-uv-cache"]
        < indexes["install-python-312"]
        < indexes["reset-uv-cache"]
        < indexes["check-root-lock"]
        < indexes["warm-root-runtime"]
        < indexes["offline-workspace-probe"]
    )
    by_id = {step.get("id"): step for step in steps}
    assert by_id["configure-uv-cache"]["run"] == (
        'echo "UV_CACHE_DIR=$RUNNER_TEMP/evolve-clean-uv-cache" >> "$GITHUB_ENV"'
    )
    assert shlex.split(by_id["install-python-312"]["run"]) == [
        "uv",
        "python",
        "install",
        "3.12",
    ]
    assert shlex.split(by_id["reset-uv-cache"]["run"]) == ["rm", "-rf", "$UV_CACHE_DIR"]
    assert shlex.split(by_id["warm-root-runtime"]["run"]) == [
        "uv",
        "sync",
        "--dev",
        "--locked",
        "--python",
        "3.12",
    ]
    assert shlex.split(by_id["warm-scaffold-runtime"]["run"]) == [
        "uv",
        "sync",
        "--project",
        "scaffolds/workspace",
        "--frozen",
        "--no-install-project",
        "--python",
        "3.12",
    ]
    assert by_id["warm-scaffold-runtime"]["env"] == {
        "UV_PROJECT_ENVIRONMENT": "${{ runner.temp }}/evolve-scaffold-venv"
    }
    assert shlex.split(by_id["check-scaffold-lock"]["run"]) == [
        "uv",
        "lock",
        "--project",
        "scaffolds/workspace",
        "--check",
        "--offline",
    ]
    assert by_id["check-scaffold-lock"]["env"]["UV_OFFLINE"] == "1"
    assert shlex.split(by_id["offline-workspace-probe"]["run"]) == [
        "uv",
        "run",
        "--offline",
        "pytest",
        "-q",
        "tests/test_recipe_composition.py",
    ]
    assert by_id["offline-workspace-probe"]["env"]["UV_OFFLINE"] == "1"


def test_ci_self_driving_smoke_requires_real_candidate_progress() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    smoke = next(step for step in steps if step.get("name", "").startswith("deterministic mechanism smoke"))

    assert "tests/fixtures/smoke_agent.py" in smoke["env"]["EVOLVE_AGENT_COMMAND"]
    command = smoke["run"]
    assert 'EVOLVE_HOME="$RUNNER_TEMP/ci-smoke-home" uv run --frozen evolve init' in command
    assert "--recipe-path tests/fixtures/recipes/hill_climb-smoke" in command
    assert "--seed tests/fixtures/seeds/dummy" in command
    assert 'tests/assert_self_driving_smoke.py "$RUNNER_TEMP/ci-smoke" 3' in command
    assert "--recipe hill_climb" not in command


def test_root_lock_warms_every_generated_workspace_runtime_version() -> None:
    def registry_versions(path: Path) -> dict[str, str]:
        lock = tomllib.loads(path.read_text())
        return {
            package["name"]: package["version"]
            for package in lock["package"]
            if "version" in package and package.get("source", {}).get("registry") == "https://pypi.org/simple"
        }

    root = registry_versions(ROOT / "uv.lock")
    generated = registry_versions(ROOT / "scaffolds" / "workspace" / "uv.lock")
    assert {name: (root.get(name), version) for name, version in generated.items() if root.get(name) != version} == {}
