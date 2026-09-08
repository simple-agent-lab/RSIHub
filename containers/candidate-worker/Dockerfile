FROM ghcr.io/astral-sh/uv@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca
WORKDIR /opt/worker
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && .venv/bin/python -c "from importlib.metadata import version; assert version('harbor') == '0.18.0'"
ENV PATH="/opt/worker/.venv/bin:$PATH"
ENV PYTHONPATH="/input/framework"
WORKDIR /output
