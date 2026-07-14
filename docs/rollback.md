# Release rollback procedure

GHCR version tags are treated as immutable. A rollback therefore restores the previous
container digest and prepares a new patch version. Do not overwrite an immutable version
tag to make it point at different content.

## Container rollback

1. Identify the last known-good image digest from release evidence and verify its
   provenance.
2. Deploy that digest directly while investigating the failed release. With Compose, set
   `CAMOUFLARE_IMAGE` to the known-good digest or prior exact version tag.
3. Leave the affected exact version tag unchanged for auditability; release automation
   refuses to overwrite an existing GHCR version tag.
4. Run `/health`, authenticated `/ready`, and a representative local `/v1` request.

Example using placeholders that must be replaced with reviewed values:

```bash
export CAMOUFLARE_IMAGE='ghcr.io/mehmetcansahin/camouflare@sha256:KNOWN_GOOD_INDEX_DIGEST'
docker compose pull
docker compose up -d
```

## Incident evidence

Preserve the tag, commit, workflow run, checksums, SBOMs, attestations, scan output, image
digests, and observed failure. If credentials or signing identity may be compromised, stop
publication, rotate affected credentials, and verify all prior attestations before resuming.

## Interrupted publication recovery

If publication stops after a temporary candidate is pushed, preserve the uploaded release
evidence and re-run the same tag-push workflow event. The workflow reuses an existing GHCR
version only after both platform manifests pass smoke, security, and source-revision checks.
A mismatch is an incident: do not overwrite the destination; investigate and publish a new
patch version if recovery cannot be proven safe.
