FROM ghcr.io/astral-sh/uv:trixie-slim

RUN <<EOF
    set -eu
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends curl git-core ca-certificates
    rm -rf /var/lib/apt/lists/*
EOF

# --- Install jujutsu — update JJ_VERSION to the desired release
ARG JJ_VERSION=0.42.0
RUN ARCH=$(uname -m) && \
    curl -fsSL \
    "https://github.com/jj-vcs/jj/releases/download/v${JJ_VERSION}/jj-v${JJ_VERSION}-${ARCH}-unknown-linux-musl.tar.gz" \
    | tar -xz -C /usr/local/bin ./jj


# --- Install repoactive
RUN --mount=type=bind,target=/app <<EOF
    set -eu
    export HOME=/usr/local/uv-home
    uv --no-cache tool install --compile-bytecode /app
EOF

WORKDIR /src
ENV HOME="/tmp/"

ENTRYPOINT ["repoactive"]
