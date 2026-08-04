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

The first-party DataFrame tool packages treat `DataFrameTool`, `Passthrough`, public input/output schema serialization, and their tested `Workflow` integration as a stable tool-authoring contract throughout BioImageFlow 0.x.
Their `bioimageflow>=0.1.6,<1` range therefore allows execution, launcher, and GUI-integration releases without republishing unchanged tools.
Changing that tool-authoring contract requires updating and releasing the affected tool packages.
This policy does not extend the compatibility range of `bioimageflow-core`; changing the core SDK boundary requires a separate compatibility review.

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

## Prepare a Coordinated Release Set

A release set contains every package that must change together.
It can contain one package for an independent release or several packages for a coordinated API or dependency change.
Packages that remain compatible and unchanged do not belong in the set.

For every selected package, decide its new version and whether its first-party dependency bounds still describe the supported versions.
Raise a dependency floor only when the package uses an API introduced by that version.
Keep the existing upper compatibility boundary unless compatibility has intentionally changed.

Start from an updated branch and inspect the workspace:

```bash
git switch main
git pull --ff-only
uv run python scripts/package_status.py
```

Update the versions of only the selected packages, for example:

```bash
uv version --package bioimageflow-core 0.1.8 --no-sync
uv version --package bioimageflow-segmentation-tools 0.2.0 --no-sync
```

When releasing `bioimageflow-core`, also update the root workspace dependency `bioimageflow-core==<version>` to the new local core version.
Update affected first-party dependency ranges in the same change, then regenerate `uv.lock`.

During development, run only tests focused on the changed code and package metadata.
Before review, run Ruff, Pyright, and the relevant focused tests locally.
The normal **CI** workflow is the authoritative shared validation and already runs the supported Python matrix, deterministic acceptance and package-tool tests, builds every distribution without workspace sources, and builds the documentation.
Do not rerun that complete set in a separate release-validation job.

Commit the versions, dependency ranges, code, and lockfile as one release commit and merge or push it through the normal review process:

```bash
uv lock
uv run ruff check .
uv run pyright
uv run pytest tests/unit/test_release_tooling.py tests/unit/test_release_tagging.py tests/unit/test_package_artifacts.py
git push origin main
```

Wait for the normal **CI** workflow to succeed on the exact release commit.
The coordinated release workflow refuses to publish a commit without a successful `ci.yml` run for that SHA.

Run an additional resource-dependent suite only when the release changes that runtime surface:

| Release surface | Additional suite in **Complete validation** |
| --- | --- |
| Wetlands execution, environment management, worker integration, or a tool's `EnvironmentSpec` | `wetlands` |
| Public dataset downloads, URLs, parsing, or data-dependent workflows | `public-data` |
| SAIRPICO wrappers or another non-Python executable integration | `external-binaries` |
| Model-backed tools or model environment declarations, currently including segmentation runtimes | `model-runtimes` |
| Ordinary deterministic package code, metadata, or documentation | None |

Resource-dependent failures are non-blocking during weekly monitoring, but a manually selected suite is blocking and must pass before release.
Do not make every package release wait for unrelated datasets, binaries, or models.

Preview the release set after CI succeeds:

```bash
uv run --no-project --with packaging python scripts/release_set.py tag --dry-run
```

With no package arguments, the command queries PyPI and selects every package whose local version is newer than PyPI.
It skips packages that are up to date, unpublished packages that require explicit bootstrap handling, and historical packages whose matching published source tag is unavailable.
It stops for packages that are behind PyPI, contain unversioned changes after their release tag, or cannot be checked safely.

Pass package names to prepare an intentional independent subset:

```bash
uv run --no-project --with packaging python scripts/release_set.py tag \
  --dry-run bioimageflow-core bioimageflow-segmentation-tools
```

The command verifies that every unselected workspace dependency has a compatible published version.
It asks the operator to include or bump a dependency when the selected artifact would otherwise be un-installable.

Create every annotated tag locally and atomically push the complete set with:

```bash
uv run --no-project --with packaging python scripts/release_set.py tag \
  --push origin bioimageflow-core bioimageflow-segmentation-tools
```

The JSON output contains `release_tags`, the exact space-separated value required by the GitHub workflow.
Explicit preview and push commands must repeat the same package names.
Omit package names from both commands when releasing every pending package discovered automatically.
`--push` accepts a configured Git remote name, not a URL, so credentials cannot enter output or diagnostics.
The operation preflights every tag before creating any, accepts exact existing annotated tags idempotently, and rolls back tags created by the current invocation if local creation fails.
An atomic push failure leaves the validated local tags in place; repair the remote or credentials and rerun the same command.
The script never falls back to a partial non-atomic push.
Pushing tags does not publish anything.

## Publish the Release Set from GitHub

Open **Actions > Publish coordinated package release** and run the workflow from `main`.
Enter every tag in the `release_tags` input, separated by spaces.
Normally select `publish`: the PyPI environment approval is the final publication gate.
Use `validate` only for an optional dry run; a later publish run must rebuild its artifacts, so running both modes routinely wastes time.

The workflow performs only release-specific work:

1. It resolves the tags to one commit and validates all versions, selected dependency ranges, and current PyPI versions.
2. It requires a successful normal CI workflow for the exact tagged commit instead of rerunning the workspace tests.
3. It builds and validates only the selected distributions, in parallel.
4. After approval of the `pypi` environment, it publishes dependencies before their selected dependants with short-lived trusted-publishing credentials.
5. It waits until every requested version is visible on PyPI.

If publication stops partway through, rerun the same workflow with the same release set.
The publisher checks PyPI before uploading, so already published identical files are skipped and remaining packages continue in dependency order.
Never move or reuse a release tag, and never attempt to replace an existing PyPI file.

After PyPI has indexed the release, verify the workspace status locally:

```bash
uv run python scripts/package_status.py
```
