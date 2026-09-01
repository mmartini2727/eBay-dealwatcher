FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app


COPY pyproject.toml ./
COPY dealwatch ./dealwatch

# data/ is a host bind mount; the host-side directory must also be
# writable by uid 10001. See README operational notes.
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin dealwatch \
    && mkdir -p /app/data \
    && chown -R dealwatch:dealwatch /app

USER dealwatch

EXPOSE 8000

CMD ["uvicorn", "dealwatch.main:app", "--host", "0.0.0.0", "--port", "8000"]
