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
The GitLab release job rejects a lightweight tag, a dirty checkout, a mismatched version, additional distribution artifacts, or a tag that does not point to the pipeline commit.

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

## One-Time GitLab and PyPI Setup

PyPI Trusted Publishing supports `gitlab.com`, but not the self-managed `gitlab.inria.fr` instance used by this repository.
The release job therefore reads a PyPI API token from a protected GitLab variable.

Complete these steps once:

1. Ensure the PyPI account that owns the BioImageFlow projects has a verified email address and two-factor authentication.
2. Create a PyPI API token named for this GitLab release job.
3. Select the entire-account scope because one job publishes several independently named PyPI projects and because the first upload creates projects that do not exist yet.
4. In GitLab, open **Settings > CI/CD > Variables** and add the token as `UV_PUBLISH_TOKEN`.
5. Mark the variable **Masked** and **Protected**, and scope it to the `pypi` environment.
6. In GitLab, protect the `pypi` environment and limit deployment access to the maintainers allowed to publish.
7. Protect release tags matching `bioimageflow*-v*` and limit tag creation to the maintainers allowed to publish.

The account-wide token is a consequence of using one dynamic release job with a self-managed GitLab instance.
If narrower project-scoped tokens become necessary, replace the dynamic job with one statically named protected environment and one token per distribution.

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

## Publish from GitLab

The pushed tag creates a pipeline.
All ordinary quality, test, packaging, and documentation stages must pass.

Then:

1. Open the tag pipeline in GitLab.
2. Find the manual `release:pypi` job.
3. Confirm the tag names the intended package and version.
4. Run the job.
5. Wait for the job to validate Python 3.11, build the selected package with workspace sources disabled, validate exactly one wheel and one source distribution, and upload them to PyPI.
6. Run `uv run python scripts/package_status.py` locally after PyPI has indexed the release.

Publication is retry-safe for identical files because `uv publish` checks PyPI before uploading.
Never move or reuse a release tag, and never attempt to replace an existing PyPI file.
