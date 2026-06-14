FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir -U pip \
    && python -m pip install --no-cache-dir -e .

EXPOSE 8765

ENTRYPOINT ["python", "-m", "turtle_bot"]
