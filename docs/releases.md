# Release record

Published artifacts for every release, kept in the repository so that recovering them
does not depend on CI artifact retention. The [rollback procedure](rollback.md) needs the
last known-good image digest; these are those digests.

GHCR version tags are immutable, but a digest is stronger than a tag: a tag names
content, a digest is the content. Deploy by digest when the exact bits matter.

```bash
export CAMOUFLARE_IMAGE='ghcr.io/mehmetcansahin/camouflare@<index digest below>'
docker compose pull
docker compose up -d
```

Confirm what actually started. The label must equal the commit recorded for that release:

```bash
docker inspect <container> \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
```

The per-architecture digests are recorded for evidence and for pinning a single platform.
Normal deployments use the index digest and let the runtime select the platform.

## 2.0.0 - 2026-09-06

- Commit: `94c32f185b4fb6a23e45e1b7f8c8fa5b5b83434e`
- Release run: https://github.com/mehmetcansahin/camouflare/actions/runs/34041579853
- Index: `sha256:eaa2260c0fa110ce9abc5bf3616e071c1ef6a12528f69d9ff88d4e33c14f7534`
- linux/amd64: `sha256:918d8457c9225296ab0c38e3aecf3c0fef09082a287ec564748086dae20bfb64`
- linux/arm64: `sha256:63588123d7d049eb51394a7fbf7d5893fd8c37598243e935ef9146bebf9460a8`

## 1.4.0 - 2026-08-27

- Commit: `bb0e992040bb59a11df92ea215da56e154f4d768`
- Release run: https://github.com/mehmetcansahin/camouflare/actions/runs/33051160006
- Index: `sha256:4d97100173a191b15163d2c1500c74ae02074803608cf5a7cb693c458e5985fe`
- linux/amd64: `sha256:4dd38f76c70fc1e9985184feb762fdc4d1b8fb3c50e9fdac8aa95ac0d4c4e0a2`
- linux/arm64: `sha256:3de6fbee88ac26c5b53b92a9d0ee7d8664e9c9eab879a57018b87f92a50be2d0`

## 1.3.3 - 2026-08-25

- Commit: `2eee835212edd95c4f112d6e0ce08b27393b0a59`
- Release run: https://github.com/mehmetcansahin/camouflare/actions/runs/32882899139
- Index: `sha256:82cf3cc28a6da5cb89cbbef820e6cfd158c61fa39e30479b5f4238e090213e42`
- linux/amd64: `sha256:39cf89d93ae28b493feb8ff3d3e51e8bcc3b7473e048e2986f9b9b0677857253`
- linux/arm64: `sha256:f60485d935430c3c83f65793889b451edbab53c07cd2307086df3a85d50446af`

## 1.3.2 - 2026-08-25

- Commit: `53df0cf758e8112be798929d43f79326ac4e326e`
- Release run: https://github.com/mehmetcansahin/camouflare/actions/runs/32832420311
- Index: `sha256:3225a20829db139b705c2fd68f494d24ac596efd7b5dc2945b43a8573c764745`
- linux/amd64: `sha256:3e8cf34dfe6fcd42c32cf1e2cfdbc56bbad393831c9f52388c02a436a5a57feb`
- linux/arm64: `sha256:cb6996d3a053f78b9ff004d80a419326dc8ae17115924ed023a6ebf4aec33187`

## 1.3.1 - 2026-07-26

- Commit: `01bbeb807a94ef3e19674486a92e3bc83acbe178`
- Release run: https://github.com/mehmetcansahin/camouflare/actions/runs/30210675066
- Index: `sha256:8f7c05dd3e785e4b27934c12811b07c733470182a7d09a522feb3bbffbf9b177`
- linux/amd64: `sha256:69e10c955c75f61bbe87b0a2e0f76b98d5c64fa867175a63eac493aebf1cd07a`
- linux/arm64: `sha256:21708b9d8b1526cd678d01ec4bad64481d641c8dcda50bab2f208cdfdcf4b94c`

## 1.3.0 - 2026-07-22

- Commit: `a31ed87ffe4eed93e7240808977cee23a9dcb222`
- Release run: https://github.com/mehmetcansahin/camouflare/actions/runs/29953896489
- Index: `sha256:37a57f2b3d430761ad5595e0863aaf79829eccd153163a7cda8cd7e733ca0c43`
- linux/amd64: `sha256:ce1afea75f0afe0418a02e157c95f0323cdad38ca120b1fc5b0ef532b7cfe6ba`
- linux/arm64: `sha256:e934fd9f4a5bf157fba2a45210ee6354ced5503b5975991049af388e163fff5e`

## 1.2.0 - 2026-07-18

- Commit: `87ab20fc2f8ac448352e083713c14cc5fb665619`
- Release run: https://github.com/mehmetcansahin/camouflare/actions/runs/29645428644
- Index: `sha256:accd869e3c1affac1884e5a666a898c25ceb5e80f11f383c5ee75cf0e5ae479d`
- linux/amd64: `sha256:f43dfadd8976235b30c0cc59f2cb87ccaf2ce53854a608dd7e41c0cf76a07838`
- linux/arm64: `sha256:0c1b4303fab69a67bfc5c9306bb6c37997b724bfc9e2cf98673b62641e45f5d1`

## 1.1.0 - 2026-07-16

- Commit: `11cf8e8e313437559e82f840b0ed7e102f7c31e0`
- Release run: https://github.com/mehmetcansahin/camouflare/actions/runs/29492502081
- Index: `sha256:76d38b0639a88fd95b1abba7d7d3eb055318ab4aa6252e5a509bc50300fbe40b`
- linux/amd64: `sha256:69bd4bf72cd5fd054fce28e4701aa9cdee16b93f564035e46065a404752f3d29`
- linux/arm64: `sha256:b776b49d105408db2c97403e6afdcde8a1bbab6557c76e642bd978f7d5626171`

## 1.0.0 - 2026-07-14

- Commit: `39791cac711f57c3676c6277a6a9f53ff8981664`
- Release run: https://github.com/mehmetcansahin/camouflare/actions/runs/29401128357
- Index: `sha256:8a606025617ba6a85906d111c12df4612255ab37bbe20fce1b204b1d7676afc5`
- linux/amd64: `sha256:19ec2d77627236dc90fbb56a1e57a01d5eae815d3e1db41bf0957b08706f77c2`
- linux/arm64: `sha256:589370a626f4395c84813de3c0051f52aae067803a69f34b5a21729ebb9434d8`
