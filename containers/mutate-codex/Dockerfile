ARG RUNTIME_BASE=ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90
FROM node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46 AS codex-build

ARG CODEX_VERSION=0.149.0
RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && codex --version

FROM ${RUNTIME_BASE}

ARG CODEX_VERSION=0.149.0
ARG SOURCE_REVISION=unknown
ARG BUILD_TIMESTAMP=unknown
ARG INSTALL_TOOLS=1

LABEL org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.created="${BUILD_TIMESTAMP}" \
      io.evolve.codex.version="${CODEX_VERSION}"

RUN if [ "$INSTALL_TOOLS" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        coreutils \
        curl \
        diffutils \
        file \
        findutils \
        git \
        jq \
        less \
        openssh-client \
        patch \
        procps \
        python3 \
        python3-pip \
        python3-venv \
        python-is-python3 \
        ripgrep \
        rsync \
        sed \
        tree \
        unzip \
        zip \
      && rm -rf /var/lib/apt/lists/*; \
    fi
COPY --from=codex-build /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-build /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex
RUN ln -sfn ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && codex --version
RUN git config --system --add safe.directory /app/task/workspace

WORKDIR /app
