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
5. Bootstrap projects that do not yet exist using one of the procedures below, then add the same normal GitHub Actions Trusted Publisher to every new project.

The same workflow identity can be registered for every independently versioned distribution in this repository.
Normal publishers support this one-repository-to-many-projects relationship.

PyPI currently prevents two pending GitHub publishers from using the same owner, repository, workflow, and environment for different future project names.
It also limits an account to three simultaneous pending publishers.
These restrictions are enforced by the [current Warehouse implementation](https://github.com/pypi/warehouse/blob/e77bccb0a64a585007c5f90b5ca7ac041f9a8d71/warehouse/accounts/views.py#L1838-L1911), although the general Trusted Publishing documentation does not emphasize the distinction between pending and normal publishers.

For a token-free bootstrap, register one pending publisher, publish that project through GitHub Actions, and repeat after the pending publisher becomes normal.
For a one-time batch bootstrap, publish locally as described next.

## Bootstrap Unpublished Projects Locally

The local batch publisher queries PyPI before building anything.
It skips a selected distribution when PyPI already has the requested version or a newer version, and it stops before uploading when a remaining local package does not declare the requested version.
It never edits package versions, commits, tags, or Git remotes.

Preview the batch first:

```bash
uv run --no-sync python scripts/publish_packages.py plan 0.1.6
```

Use repeated `--package` options to publish a same-version subset when the workspace contains independently versioned packages:

```bash
uv run --no-sync python scripts/publish_packages.py plan 0.1.6 \
  --package bioimageflow-io-tools \
  --package bioimageflow-measurement-tools
```

Before publishing, ensure every selected package declares the target version, review first-party dependency bounds, regenerate `uv.lock`, run the normal validation, commit the result, and require a clean working tree.
The script deliberately does not infer dependency floors or upper bounds because a coordinated API change still requires a compatibility decision.

Create a temporary account-scoped PyPI API token.
A project-scoped token cannot create projects that do not exist yet.
Expose it only through `UV_PUBLISH_TOKEN`, then run the explicit publish command:

```bash
export UV_PUBLISH_TOKEN
read -s UV_PUBLISH_TOKEN
uv run --no-sync python scripts/publish_packages.py publish 0.1.6
unset UV_PUBLISH_TOKEN
```

The script builds each selected distribution separately with workspace sources disabled, validates exactly one matching wheel and source distribution, and publishes sequentially with trusted publishing disabled.
It removes `UV_PUBLISH_TOKEN` from the build subprocess environment and exposes the credential only to `uv publish`.
If publication stops partway through, rerun the same command: the PyPI plan will skip packages that already reached the target version.

Immediately revoke the temporary token after the batch.
For every newly created PyPI project, add the normal `release.yml` Trusted Publisher before the next release.
The bootstrap upload has no package-specific release tag, so `package_status.py` will report that version as `unknown`; do not create a retrospective tag unless the published artifacts can be proven to match the tagged commit.

Use this local path only for initial project creation.
For later coordinated changes, such as a tool API compatibility boundary, update the affected packages in one reviewed commit and push one package-specific tag per affected distribution so the normal GitHub release workflow remains the publication authority.

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
Push the reviewed release commit to the branch intended for release.

```bash
git push origin main
```

Before creating the tag, open **Actions > Complete validation**, run the workflow on that branch, and select `release-validation`.
Wait for the full deterministic Python 3.11 job to pass.
The tag-triggered release workflow reruns the same deterministic gates, but the manual run catches problems before an immutable release tag is created.

Run additional resource-dependent suites when the release changes the corresponding runtime surface:

| Release surface | Additional suite |
| --- | --- |
| Wetlands execution, environment management, worker integration, or a tool's `EnvironmentSpec` | `wetlands` |
| Public dataset downloads, URLs, parsing, or data-dependent workflows | `public-data` |
| SAIRPICO wrappers or another non-Python executable integration | `external-binaries` |
| Model-backed tools or model environment declarations, currently including segmentation runtimes | `model-runtimes` |
| Ordinary deterministic package code, metadata, or documentation | None beyond `release-validation` |

Select only the suites relevant to the package and changes being released.
Resource-dependent failures are non-blocking during weekly monitoring, but a manually selected suite is blocking and must pass before release.
Do not make every package release wait for unrelated datasets, binaries, or models.

After the relevant validation has passed, create the annotated tag at the exact validated commit:

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
