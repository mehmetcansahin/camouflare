# Release checklist

The workflow publishes irreversible package versions. A maintainer must review the exact
change description and approve the protected `release` environment before publication.

## One-time repository configuration

- Create a GitHub environment named `release` and require a maintainer reviewer.
- Protect `v*` tags with a repository ruleset that blocks updates and deletion; the release
  workflow also resolves the current tag through the authenticated GitHub API and checks it
  against the event commit immediately before publication, including for private repositories.
- Configure a PyPI Trusted Publisher for this repository, `.github/workflows/release.yml`,
  and the `release` environment.
- Link the GHCR package to this repository, grant this repository's Actions workflow write
  access, and make the package public before advertising unauthenticated Compose/image pulls.
- Permit the workflow `packages: write`, `id-token: write`, and `attestations: write`
  permissions already declared in the workflow.
- Optionally set repository variables `SMOKE_URL` and `SMOKE_EXPECT` only for an
  operator-owned challenge test target.
- Set `IMAGE_SIZE_BASELINE_TAG` to the latest reviewed exact release tag when image-size
  comparisons should move beyond the default `1.0.0` baseline.

## Prepare

- [ ] Update `camouflare.__version__` and package metadata to the same semantic version.
- [ ] Move reviewed entries from `Unreleased` to a dated changelog heading.
- [ ] Obtain maintainer approval for the exact changelog/release wording.
- [ ] Confirm all CI jobs pass on Python 3.11–3.14.
- [ ] Confirm real-browser, package-install, Docker smoke, coverage, type, and format gates pass.
- [ ] Review high/critical scan results and remove obsolete security exceptions.
- [ ] Confirm every remaining exception has a specific reason and unexpired `expires_on` date.

## Publish

- [ ] Create and push an annotated `vMAJOR.MINOR.PATCH` tag at the reviewed commit.
- [ ] Inspect the release workflow's package checksums, SBOM, and security-gate output.
- [ ] Approve the protected `release` environment only after the pre-publication gates pass.
- [ ] Confirm release evidence was uploaded before either immutable destination was changed.
- [ ] Verify PyPI contains both the wheel and source distribution with attestations.
- [ ] Verify GHCR exposes linux/amd64 and linux/arm64 for the exact version tag.
- [ ] Verify image provenance, SBOM, digest, and per-architecture size evidence.

## After publication

- [ ] Install the PyPI artifact in a clean environment and run `camouflare --version`.
- [ ] Pull the immutable image digest and run `/health`, authenticated `/ready`, and local `/v1`.
- [ ] Record the published digests and workflow URL in the release record.
- [ ] If only PyPI or GHCR succeeds, re-run the same tag-push workflow event. The preflight
  accepts existing PyPI files only when they are a matching subset of `dist/`, then
  re-smokes/re-scans an existing image whose revision labels match the event commit and
  publishes only the missing destination.
- [ ] Follow the [rollback procedure](rollback.md) if either distribution is unhealthy.
