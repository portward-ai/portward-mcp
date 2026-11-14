# Portward's hosted engine (#104): the free tool in server mode, one container per Personal user, run by Portward
# ("Portward runs this"). The same open-source engine as on the user's computer, so a profile means exactly the same.
#   docker build -t portward-engine free/
#   docker run -p 8080:8080 -v engine-data:/data \
#     -e PORTWARD_ACCOUNT_URL=https://signup.portward.ai -e PORTWARD_PUBLIC_URL=https://h-<id>.<hosted domain> \
#     -e PORTWARD_ENGINE_TOKEN=<the engine's token: a secret> portward-engine
# Portward's account service provisions it with exactly those three settings; the
# token is a secret at the host. Logins and profiles are never in the environment: they arrive sealed to the key the
# engine makes on /data the first time it starts, and stay in memory. /data holds that key and the engine's local file
# (history and AI log). TLS ends in front of it at the host (launch: a Fly Machine per user; see the #104 notes).
# Python source is shipped as is (not compiled): the engine is open source, and an outsider can read what runs.
# Before publishing: pin both images by digest and build in CI, so the build can be reproduced (encrypted-hosting
# research, "Do in either case").
FROM ghcr.io/astral-sh/uv:0.11.28 AS uv

FROM cgr.dev/chainguard/wolfi-base:latest AS build
RUN apk add --no-cache python-3.12
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=python3.12
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
 && /app/.venv/bin/python -c "import portward_free.hosted.engine"

FROM cgr.dev/chainguard/wolfi-base:latest
ARG REVISION=""
LABEL org.opencontainers.image.revision=$REVISION org.opencontainers.image.title="Portward hosted engine"
RUN apk add --no-cache python-3.12 \
 && adduser -D -H -u 10001 -h /data portward && mkdir -p /data && chown portward /data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORTWARD_HOME=/data PORT=8080
USER portward
WORKDIR /data
EXPOSE 8080
VOLUME ["/data"]
# Serving when the protected-resource metadata answers (everything else needs the account owner's token).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import http.client as h;c=h.HTTPConnection('127.0.0.1',8080,timeout=4);c.request('GET','/.well-known/oauth-protected-resource/mcp');raise SystemExit(c.getresponse().status!=200)"
ENTRYPOINT ["portward-free", "serve-hosted"]
