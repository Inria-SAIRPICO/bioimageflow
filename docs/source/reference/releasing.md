# Releasing Python Packages

BioImageFlow distributions are versioned and published independently from the shared repository.
The repository is tested as one workspace, but a release tag selects and publishes exactly one package.

## Release Identity

Release tags use the distribution name followed by a stable three-part version:

```text
bioimageflow-core-v0.1.7
bioimageflow-v0.1.7
bioimageflow-segmentation-tools-v0.2.0
```

The tag must be annotated, point to the release commit, and match the selected package's `[project].version` exactly.
The GitHub release workflow rejects a lightweight tag, a dirty checkout, a mismatched version, additional distribution artifacts, or a tag that does not point to the workflow commit.

## Versioning Policy

Each distribution owns its version.
A change in one tool package does not require releases of unrelated packages.
The workspace integration tests still run before every publication.

Choose the version bump from the selected package's public behavior:

- Patch: compatible fixes, documentation corrections shipped in the source distribution, and compatible implementation improvements.
- Minor: new public tools or features, or a breaking change while the package remains on major version `0`.
- Major: breaking changes after the package reaches `1.0.0`.

First-party dependency ranges declare the oldest tested compatible version and an upper compatibility boundary, for example `bioimageflow-core>=0.1.7,<0.2`.
A downstream package needs a release when it starts using a newer first-party API or when its compatibility range changes.
A core or orchestrator release alone does not require downstream releases when their declared ranges remain accurate.

## Check Package Status

Run:

```bash
uv run python scripts/package_status.py
```

The report distinguishes unpublished packages, local versions newer than PyPI, local versions behind PyPI, tagged packages changed after release, and packages that match both PyPI and their package-specific tag.
Matching version numbers without a corresponding release tag are reported as unknown because PyPI does not identify the source commit used to build an older artifact.

Use `--json` for machine-readable output or `--check` to require every package to be up to date.

## One-Time GitHub and PyPI Setup

The release workflow uses PyPI Trusted Publishing through GitHub Actions.
It does not use a stored PyPI password or API token.

Complete these steps once:

1. Ensure the PyPI account that owns the BioImageFlow projects has a verified email address and two-factor authentication.
2. In the GitHub repository, create an environment named `pypi` and configure the maintainers who must approve deployments to it.
3. Create a GitHub repository ruleset for tags matching `bioimageflow*-v*` that restricts tag creation, update, and deletion to release maintainers.
4. For each existing BioImageFlow project on PyPI, add a GitHub Actions Trusted Publisher with owner `bioimageit`, repository `bioimageflow`, workflow `release.yml`, and environment `pypi`.
5. For each BioImageFlow project that does not yet exist on PyPI, add a pending publisher with the same owner, repository, workflow, and environment plus the exact future PyPI project name.

The same workflow identity can be registered for every independently versioned distribution in this repository.
Pending publishers create their PyPI projects during the first matching release and then become normal publishers.

## Prepare One Release

The release operator must decide four inputs:

1. Package name, such as `bioimageflow-segmentation-tools`.
2. New version, such as `0.2.0`.
3. Whether the package now requires newer `bioimageflow-core` or `bioimageflow` APIs.
4. Whether its current upper compatibility boundaries remain valid.

Start from an updated branch and inspect status:

```bash
git switch main
git pull --ff-only
uv run python scripts/package_status.py
```

Set only the selected distribution's version:

```bash
uv version --package bioimageflow-segmentation-tools 0.2.0 --no-sync
```

When releasing `bioimageflow-core`, also update the root workspace dependency `bioimageflow-core==<version>` to the new local core version.
If the package uses a newly introduced first-party API, update its dependency floor in the same `pyproject.toml`.
Do not raise dependency floors merely because another package published a compatible release.

Regenerate the lockfile and run the normal validation:

```bash
uv lock
uv run ruff check .
uv run pyright
uv run pytest
uv build --package bioimageflow-segmentation-tools --no-sources --clear --out-dir dist/release-check
BIOIMAGEFLOW_PACKAGE_ARTIFACTS_DIR=dist/release-check BIOIMAGEFLOW_PACKAGE_ARTIFACTS_PACKAGE=bioimageflow-segmentation-tools uv run pytest tests/unit/test_package_artifacts.py
```

Commit the version, dependency, and lockfile changes through the normal review process.
After that commit is on the branch intended for release, create the annotated tag at that exact commit:

```bash
git tag -a bioimageflow-segmentation-tools-v0.2.0 -m "Release bioimageflow-segmentation-tools 0.2.0"
uv run python scripts/check_package_release.py bioimageflow-segmentation-tools-v0.2.0
git push origin bioimageflow-segmentation-tools-v0.2.0
```

## Publish from GitHub

The pushed tag starts the **Publish package to PyPI** workflow.
Its build job must complete quality, deterministic test, documentation, package build, and artifact identity checks before publication is eligible for approval.

Then:

1. Open the workflow run in the GitHub **Actions** tab.
2. Confirm that the build job succeeded and that the tag names the intended package and version.
3. Review and approve the waiting deployment to the `pypi` environment.
4. Wait for GitHub Actions to obtain a short-lived PyPI credential and publish the validated wheel and source distribution.
5. Run `uv run python scripts/package_status.py` locally after PyPI has indexed the release.

Publication is retry-safe for identical files because `uv publish` checks PyPI before uploading.
Never move or reuse a release tag, and never attempt to replace an existing PyPI file.
