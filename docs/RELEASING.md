# Releasing

Releases are built from a clean `main` checkout after the version in
`pyproject.toml` and `src/evolve/__init__.py` has been updated to the same value.
Do not publish from an unreviewed branch or a working tree with local changes.

## Validate the source tree

```bash
uv lock --check
uv sync --dev --locked
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
uv run --frozen pytest -q
```

Confirm that the release commit passes the required GitHub checks. A stub smoke
test is not evidence that a real Harbor, model, or benchmark run succeeded; link
separate reproducible experiment artifacts for any quality claim.

## Build and inspect artifacts

```bash
release_dist="$(mktemp -d)"
uv build --out-dir "$release_dist"
EVOLVE_RELEASE_DIST="$release_dist" \
  uv run --frozen pytest -q -n 0 tests/test_release_artifact.py -p no:cacheprovider
```

Install the wheel, rather than the source checkout, into a clean environment and
run at least `evolve --help`, workspace initialization, and `evolve status`.
Inspect the sdist and wheel before uploading them; both must contain the Apache
license and NOTICE, while the wheel must contain only the `evolve/library`
resource copy.

## Publish

Create an annotated `vX.Y.Z` tag only after the artifact checks pass. PyPI
publishing should use a repository-scoped Trusted Publisher and a protected
GitHub environment, not a long-lived API token. Record user-visible changes and
artifact checksums in the GitHub release. Publishing automation is intentionally
not enabled until the package name and Trusted Publisher are owned by the
maintainers.
