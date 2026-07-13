# Release rollback procedure

PyPI versions are immutable and cannot be reused. A rollback therefore restores the
previous container channel, yanks the affected PyPI release, and prepares a new patch
version. Do not overwrite an immutable version tag to make it point at different content.

## Container rollback

1. Identify the last known-good image digest from release evidence and verify its
   provenance.
2. Deploy that digest directly while investigating the failed release. With Compose, set
   `CAMOUFLARE_IMAGE` to the known-good digest or prior exact version tag.
3. Leave the affected exact version tag unchanged for auditability; release automation
   refuses to overwrite an existing PyPI version or GHCR version tag.
4. Run `/health`, authenticated `/ready`, and a representative local `/v1` request.

Example using placeholders that must be replaced with reviewed values:

```bash
export CAMOUFLARE_IMAGE='ghcr.io/mehmetcan/camouflare@sha256:KNOWN_GOOD_INDEX_DIGEST'
docker compose pull
docker compose up -d
```

## PyPI rollback

1. Yank the affected version in PyPI and provide a concise reason approved by a maintainer.
2. Restore the previous known-good package in deployment constraints; do not delete local
   evidence or attempt to upload replacement files under the same version.
3. Fix the issue, increment the patch version, update the changelog, and run the complete
   release workflow again.

## Incident evidence

Preserve the tag, commit, workflow run, checksums, SBOMs, attestations, scan output, image
digests, and observed failure. If credentials or signing identity may be compromised, stop
publication, rotate affected credentials, and verify all prior attestations before resuming.

## Partial publication recovery

PyPI and GHCR cannot be updated atomically. If one destination succeeds and the other fails,
preserve the uploaded release evidence and re-run the same tag-push workflow event. The
workflow skips complete PyPI releases and safely retries matching partial uploads with
`skip-existing`; any unexpected filename or digest blocks recovery. It reuses a GHCR version
only after both platform manifests pass smoke,
security, and source-revision checks. A mismatch is an incident: do not overwrite either
destination; investigate and publish a new patch version if recovery cannot be proven safe.
