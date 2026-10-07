# Releasing Python Packages

BioImageFlow distributions are versioned and published independently from the shared repository.
The repository is tested as one workspace; each annotated release tag identifies one package version at the selected commit, and the explicitly dispatched coordinated workflow publishes the selected set.

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
Ordinary exact-head local CI and any selected affected runtime suite must pass before publication.
Distributed coverage remains visible in its separate workflow; the local/distributed CI partition does not certify or repair excluded distributed features.

Choose the version bump from the selected package's public behavior:

- Patch: fixes or metadata-only adoption of the tested current SDK cohort when the tool’s public scientific behavior is unchanged, documentation corrections shipped in the source distribution, and implementation improvements.
- Minor: new public tools or features, or a breaking change while the package remains on major version `0`.
- Major: breaking changes after the package reaches `1.0.0`.

First-party dependency ranges identify the single supported current release cohort.
Core is `>=0.5.0,<0.6`, the orchestrator is `>=0.9.0,<1`, and its Wetlands dependency is `>=2.6.1,<3`.
All nine first-party tool packages use that Core range; common, measurement, spot and tracking also use the current orchestrator range.
These ranges must resolve and pass current scientific and tool-authoring controls before publication.
There is no obligation to preserve old DTOs, wire schemas, aliases or dependency floors solely for backward compatibility.
Essential scientific types, file workflows, durable results and current ownership/cancellation semantics remain mandatory.
A downstream package needs a metadata release when its declared cohort changes.
The selected base release set contains Core, BioImageFlow and the eight already published tool packages; Phasor’s first publisher remains a separate gate.

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
2. In the GitHub repository, use the environment named `pypi`; deployment approval occurs only when required reviewers are actually configured.
3. Inspect the actual tag ruleset for `bioimageflow*-v*`; annotated exact-source tags are required by tooling even when a repository ruleset is not enforced.
4. For each existing BioImageFlow project on PyPI, add a GitHub Actions Trusted Publisher with owner `Inria-SAIRPICO`, repository `bioimageflow`, workflow `release.yml`, and environment `pypi`.
5. Bootstrap projects that do not yet exist using one of the procedures below, then add the same normal GitHub Actions Trusted Publisher to every new project.

GitHub inspection on October 3, 2026 found the `pypi` environment with no protection rules or required reviewers, and tag ruleset `19387789` disabled.
Do not describe those controls as enforced; recheck their actual state at publication.
Campaign package source review, exact-head CI, artifact checks and registry installability are technical release gates.
The user has authorized necessary reviewed campaign releases without another confirmation.
Trusted Publisher/account eligibility is still an external prerequisite, and no credential bypass is permitted.

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
For later coordinated changes, update the affected packages in one reviewed commit and create one annotated package-specific tag per affected distribution to identify that commit.
Tag push publishes nothing; the explicitly dispatched coordinated GitHub workflow is the publication authority.

## Prepare a Coordinated Release Set

A release set contains every package that must change together.
It can contain one package for an independent release or several packages for a coordinated API or dependency change.
Packages that remain compatible and unchanged do not belong in the set.

For every selected package, decide its new version and whether its first-party dependency bounds still describe the supported versions.
Declare the tested current dependency cohort and update all affected tool bounds coherently.
Do not retain historical floors or widen an upper bound without current consumer evidence.

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

When releasing `bioimageflow-core`, also update the lower bound of the root workspace dependency to the new local core version and keep its upper compatibility boundary.
Update affected first-party dependency ranges in the same change, then regenerate `uv.lock`.

During development, run only tests focused on the changed code and package metadata.
Before review, run Ruff, Pyright, and the relevant focused tests locally.
The ordinary local **CI** workflow (`ci.yml`) is the release-qualifying validation and runs the supported local Python matrix, deterministic acceptance and package-tool tests with fixed distributed-path exclusions, builds every distribution without workspace sources, and builds the documentation.
The separate **Distributed CI** workflow (`distributed.yml`) retains marked Parsl and complementary unmarked distributed tests, reports failures independently, and is not a substitute for local release authority.
Earlier failed combined CI runs remain failed; only a new successful ordinary exact-SHA local run can qualify the release after this partition.
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
The coordinated release workflow refuses to publish a commit without a successful ordinary push or pull-request local `ci.yml` run for that SHA.
It does not admit a manual capability run or the separate distributed workflow as release-qualifying evidence.
Manual capability runs, including floor-only dispatches, cannot satisfy this publication gate.

The Core 0.5 scoped array surface additionally requires source-disabled public annotation/default, recipe, viewer, selected-primary and numeric file/mmap lifetime witnesses on Linux and Windows, including Python 3.9/NumPy 1.26 and current Python, without broad model matrices.
To verify the same canonical wheel on every floor, dispatch `ci.yml` with `core_floor_only=true`, `candidate_run_id` naming a successful ordinary push/pull-request CI run at the exact current commit, and `candidate_core_sha256` naming its held Core wheel hash.
The paired optional inputs admit only that repository's ordinary `packages` artifact, the current Core filename/version, and the complete Git Python/typing member inventory before installing the unchanged wheel on each operating system.
Without the candidate inputs, the existing per-platform normal builds remain available; their Windows checkout line endings do not establish identical wheel bytes across platforms.
Candidate capability dispatches still cannot satisfy the ordinary exact-commit publication gate.
The eventual immutable PyPI Core wheel must match the exact candidate SHA256 tested on Windows; a different published wheel blocks adoption until that wheel is validated.
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
For a changed supported runtime, the Complete workflow accepts a fixed `runtime_gate` with `suite=model-runtimes` and requires the selected actual case to pass without skips.
InstanSeg, Nagini API, and LapTrack gates prepare isolated direct runtimes from their public recipes; StarDist keeps its managed worker boundary.
The Nagini API gate proves its adapter interface, not model inference, and external-binary evidence requires all eight SAIRPICO cases.

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
When publishing a set that includes Core, supply `expected_core_sha256` with the held SHA256 of the exact canonical wheel accepted by the required floor runs.
The digest is mandatory for Core publication selections and is not a substitute for those actual capability results; validate mode does not run this publisher gate.
Normally select `publish`: dispatching this workflow starts publication unless the GitHub `pypi` environment has required reviewers configured.
Use `validate` only for an optional dry run; a later publish run must rebuild its artifacts, so running both modes routinely wastes time.

The workflow performs only release-specific work:

1. It resolves the tags to one commit and validates all versions, selected dependency ranges, and current PyPI versions.
2. It requires a successful normal CI workflow for the exact tagged commit instead of rerunning the workspace tests.
3. It builds and validates only the selected distributions, in parallel; in publish mode, it refuses a selected Core wheel whose SHA256 differs from the held expected value.
4. Subject to the actual configured `pypi` environment controls, it publishes dependencies before their selected dependants with short-lived trusted-publishing credentials.
5. It waits until every requested version is visible on PyPI.

If publication stops partway through, rerun the same workflow with the same release set.
The release-set publisher admits every selected wheel/source-distribution pair before its first upload; an invalid later pair causes no earlier package upload.
Missing, malformed, or mismatched expected Core hashes also stop the whole selected set before any upload; reproducibility is checked rather than assumed.
For a direct operator invocation, pass the same held value through `scripts/release_set.py publish --expected-core-sha256`; sets without Core do not require it.
The publisher checks PyPI before uploading, so already published identical files are skipped and remaining packages continue in dependency order.
Never move or reuse a release tag, and never attempt to replace an existing PyPI file.

After PyPI has indexed the release, verify the workspace status locally:

```bash
uv run python scripts/package_status.py
```
