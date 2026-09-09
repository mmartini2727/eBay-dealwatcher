FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app


COPY pyproject.toml ./
COPY dealwatch ./dealwatch
# V0.9a: baked into the image instead of docker cp'd in by hand after every
# deploy - see CLAUDE.md's Operational notes for why the manual copy step
# existed and is now obsolete. tests/ still isn't copied here (see
# CLAUDE.md's Conventions section) - scripts/ is operational tooling the
# running container needs, tests/ is dev-only and never runs inside it.
COPY scripts ./scripts

# data/ is a host bind mount; the host-side directory must also be
# writable by uid 10001. See README operational notes.
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin dealwatch \
    && mkdir -p /app/data \
    && chown -R dealwatch:dealwatch /app

USER dealwatch

EXPOSE 8000

CMD ["uvicorn", "dealwatch.main:app", "--host", "0.0.0.0", "--port", "8000"]
