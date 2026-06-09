FROM python:3.14-slim

RUN <<EOF
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends curl git
    rm -rf /var/lib/apt/lists/*
EOF

# --- Install jujutsu — update JJ_VERSION to the desired release
ARG JJ_VERSION=0.42.0
RUN ARCH=$(uname -m) && \
    curl -fsSL \
    "https://github.com/jj-vcs/jj/releases/download/v${JJ_VERSION}/jj-v${JJ_VERSION}-${ARCH}-unknown-linux-musl.tar.gz" \
    | tar -xz -C /usr/local/bin ./jj

ENV PATH="/root/.local/bin:$PATH"

# --- Install repoactive
RUN --mount=type=bind,from=ghcr.io/astral-sh/uv:latest,source=/uv,target=/uv \
    --mount=type=bind,target=/app \
    /uv --no-cache tool install /app

WORKDIR /src

ENTRYPOINT ["repoactive"]
