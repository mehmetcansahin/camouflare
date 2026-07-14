# syntax=docker/dockerfile:1

FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ARG TARGETARCH
ARG UV_VERSION=0.11.16

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    XDG_CACHE_HOME=/cache \
    HOME=/tmp \
    HOST=0.0.0.0 \
    PORT=8191

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY --chown=1000:1000 camouflare ./camouflare
COPY scripts/fetch_camoufox.py ./scripts/fetch_camoufox.py

RUN --mount=type=secret,id=camoufox_releases,required=false \
    set -eux; \
    case "${TARGETARCH}" in \
        amd64) uv_target="x86_64-unknown-linux-gnu" ;; \
        arm64) uv_target="aarch64-unknown-linux-gnu" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    uv_archive="uv-${uv_target}.tar.gz"; \
    uv_release_url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl dumb-init; \
    curl --proto '=https' --tlsv1.2 -fsSLo "/tmp/${uv_archive}" \
        "${uv_release_url}/${uv_archive}"; \
    curl --proto '=https' --tlsv1.2 -fsSLo "/tmp/${uv_archive}.sha256" \
        "${uv_release_url}/${uv_archive}.sha256"; \
    cd /tmp; \
    sha256sum -c "${uv_archive}.sha256"; \
    tar -xzf "${uv_archive}"; \
    install -m 0755 "/tmp/uv-${uv_target}/uv" /usr/local/bin/uv; \
    install -m 0755 "/tmp/uv-${uv_target}/uvx" /usr/local/bin/uvx; \
    cd /app; \
    mkdir -p /cache && \
    uv sync --frozen --no-dev && \
    python_target="$(readlink -f /app/.venv/bin/python)" && \
    case "${python_target}" in /opt/uv-python/*) ;; *) exit 1 ;; esac && \
    /app/.venv/bin/python --version && \
    CAMOUFLARE_CAMOUFOX_RELEASES_FILE=/run/secrets/camoufox_releases \
        /app/.venv/bin/python scripts/fetch_camoufox.py && \
    /app/.venv/bin/playwright install-deps firefox && \
    uv cache clean && \
    rm -f /usr/local/bin/uv /usr/local/bin/uvx && \
    rm -rf /tmp/uv-* && \
    apt-get purge -y --auto-remove curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    chown -R 1000:1000 /cache /tmp && \
    chmod -R a+rwX /cache /tmp

RUN chown -R 1000:1000 /app

USER 1000
EXPOSE 8191
HEALTHCHECK --interval=1m --timeout=30s --start-period=30s --retries=3 \
    CMD /app/.venv/bin/python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8191') + '/health', timeout=30).read()"
ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/app/.venv/bin/python", "-m", "camouflare"]
