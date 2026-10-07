# Release checklist

The workflow publishes an immutable GHCR version tag and mirrors its digest to Docker Hub.
A maintainer must review the exact change description and approve the protected `release`
environment before publication.

## One-time repository configuration

- Create a GitHub environment named `release` and require a maintainer reviewer.
- Protect `v*` tags with a repository ruleset that blocks updates and deletion; the release
  workflow also resolves the current tag through the authenticated GitHub API and checks it
  against the event commit before building and again inside the promotion step, including
  for private repositories.
- Link the GHCR package to this repository, grant this repository's Actions workflow write
  access, and make the package public before advertising unauthenticated Compose/image pulls.
- Permit the workflow `packages: write`, `id-token: write`, and `attestations: write`
  permissions already declared in the workflow.
- Optionally set repository variables `SMOKE_URL` and `SMOKE_EXPECT` only for an
  operator-owned challenge test target.
- Set `IMAGE_SIZE_BASELINE_TAG` to the latest reviewed exact release tag when image-size
  comparisons should move beyond the default `1.0.0` baseline.

### Docker Hub

- Create a public `mehmetcansahin/camouflare` repository on Docker Hub.
- Enable **All tags are immutable** in the repository's **Settings > General > Tag
  mutability settings**. The mirror helper also refuses to replace any different digest.
- Create a Docker Hub personal access token with **Read & Write** permissions and an
  expiry date. Store it as `DOCKERHUB_TOKEN` in the GitHub `release` environment; the
  workflow uses `mehmetcansahin` as the login username. Do not paste the token into chat
  or commit it. Keep the token renewed before it expires.
- Confirm the mirror job runs only after the GHCR publish job succeeds. Only exact
  version tags are copied; there is no `latest` tag.

The tag-push workflow uses the workflow source stored in that tag's commit. Re-running
an older release therefore cannot pick up the newly added Docker Hub job. Once this
change reaches the default branch, run **Mirror existing release to Docker Hub** in
GitHub Actions with the exact source tag and its verified index digest from
[releases.md](releases.md). It uses the same protected `release` environment and checks
that the GHCR version tag matches the supplied digest before copying.

Alternatively, to backfill the already published 2.0.2 image from a checkout containing
the mirror helper, install Docker CLI with Buildx, sign in, and copy the recorded index:

```bash
docker login --username mehmetcansahin
python3 -m scripts.mirror_release_image \
  --tag v2.0.2 \
  --source-image ghcr.io/mehmetcansahin/camouflare \
  --digest sha256:f4c2b7acba6974f89dfc01c3dba72404290d0d3f6f8a6abb311e632478da7fc3 \
  --destination-image docker.io/mehmetcansahin/camouflare
```

Enter the token at the password prompt. GHCR's public image needs no login for this
manual copy. The helper verifies the source digest, copies the entire index, and
verifies the Docker Hub digest. Repeating the command is safe when the tag matches;
registry errors or a conflicting tag stop the copy.

This follows Docker's [registry-to-registry copy workflow](https://docs.docker.com/build/ci/github-actions/copy-image-registries/).

## Prepare

- [ ] Rotate every API token exposed in logs, chat, incident notes, or other non-secret storage;
  verify the old token is rejected before publishing.
- [ ] Update `camouflare.__version__` and package metadata to the same semantic version.
- [ ] Move reviewed entries from `Unreleased` to a dated changelog heading.
- [ ] Obtain maintainer approval for the exact changelog/release wording.
- [ ] Confirm CI passes unit tests on Python 3.11 and unit tests with coverage on Python 3.14,
  plus the linux/amd64 container checks. The release workflow covers Python 3.11–3.14
  and both linux/amd64 and linux/arm64 before publication.
- [ ] Confirm real-browser, package-install, Docker smoke, coverage, type, and format gates pass.
- [ ] Confirm the exact Camoufox `release_tag`, every archive, and every `addons` pin in
  `scripts/camoufox-artifacts.json` match independently verified upstream metadata and
  SHA-256 digests; update the reviewed tag and pins together when changing the browser
  release. When changing an addon, re-verify its version, versioned XPI URL, and digest
  together: hash the downloaded XPI independently and compare it with the AMO file hash
  and the upstream release asset digest.
- [ ] Review high/critical scan results and remove obsolete security exceptions.
- [ ] Confirm every remaining exception has a specific reason and unexpired `expires_on` date.

## Publish

- [ ] Create and push an annotated `vMAJOR.MINOR.PATCH` tag at the reviewed commit.
- [ ] Inspect the release workflow's source-package checksums, SBOM, and security-gate output.
- [ ] Approve the protected `release` environment only after the pre-publication gates pass.
- [ ] Confirm release evidence was uploaded before the immutable GHCR version tag was created.
- [ ] Verify GHCR exposes linux/amd64 and linux/arm64 for the exact version tag.
- [ ] Confirm the Docker Hub mirror job succeeded and the exact version tag has the same
  index digest as GHCR, with linux/amd64 and linux/arm64 manifests.
- [ ] Verify image provenance, SBOM, digest, and per-architecture size evidence.

## After publication

- [ ] Pull the immutable image digest and run `/health`, authenticated `/ready`, and local `/v1`.
- [ ] Run the canary with a one-minute browser max age and low max-use limit for at least three
  complete lifecycle cycles. Accept only with zero unexpected acquire timeouts, zero unhandled
  futures/tasks, stable browser-process counts, and no cleanup backlog.
- [ ] Enable Prometheus alerts for two consecutive readiness failures, `active=0 && usable=0`,
  cleanup timeouts, and a growing browser-process count.
- [ ] Observe production for at least one complete configured browser max-age window. If readiness
  or cleanup regresses, roll back to the recorded immutable digest using the documented procedure.
- [ ] Record the published digests and workflow URL in the [release record](releases.md).
- [ ] If publication is interrupted, re-run the same tag-push workflow event. The preflight
  reuses an existing GHCR version only after its platform manifests pass smoke, security,
  and source-revision checks. A run that failed before promotion leaves only an untagged
  candidate digest; delete that package version from GHCR if it is not needed as evidence.
- [ ] Follow the [rollback procedure](rollback.md) if the published image is unhealthy.
