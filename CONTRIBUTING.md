# Contributing

Thanks for considering a contribution to Camouflare.

## Ground Rules

- Use Camouflare only on systems you own, administer, or have permission to
  test.
- Do not open issues or pull requests asking for help bypassing a specific
  third-party site's access controls.
- Do not include real credentials, private proxy details, session cookies, or
  sensitive screenshots in issues, tests, or pull requests.
- Keep changes focused and include tests for behavior changes.

## Development

```bash
uv sync --group dev
uv run python -m pytest tests -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python -m pytest --cov=camouflare --cov-report=term-missing tests -q
```

For Docker changes, also run:

```bash
docker build -t camouflare:ci .
```

## Pull Requests

Before opening a pull request:

- Run the test suite and ruff.
- Update README or `/documentation` copy when public behavior changes.
- Keep active challenge handling opt-in; do not make target-specific bypass
  behavior part of the default path.
- Explain the user-visible behavior change and any operational risk.

## Security Issues

Do not report security vulnerabilities in public issues. Follow
[`SECURITY.md`](SECURITY.md) instead.
