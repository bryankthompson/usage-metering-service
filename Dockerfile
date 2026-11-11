FROM python:3.12-slim
# uv is pinned so a rebuild cannot pull a different one under the same lockfile; change the tag on purpose.
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY db ./db
# The app writes nothing, so it runs as an unprivileged user; the files above stay owned by root and are only read.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin app
USER app
ENV PATH="/srv/.venv/bin:$PATH"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
