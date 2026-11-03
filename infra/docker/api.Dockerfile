FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ENV PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /app

RUN set -eux; \
  if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    sed -i \
      -e 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' \
      -e 's|http://security.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' \
      -e 's|https://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' \
      -e 's|https://security.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' \
      /etc/apt/sources.list.d/debian.sources; \
  else \
    printf '%s\n' \
      'deb http://mirrors.aliyun.com/debian trixie main' \
      'deb http://mirrors.aliyun.com/debian trixie-updates main' \
      'deb http://mirrors.aliyun.com/debian-security trixie-security main' \
      > /etc/apt/sources.list; \
  fi; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    fontconfig \
    fonts-noto-cjk \
    fonts-noto-color-emoji; \
  fc-cache -f; \
  rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY apps ./apps
COPY packages ./packages
COPY pipelines ./pipelines
COPY tool_catalog ./tool_catalog
COPY infra ./infra
COPY scripts ./scripts
COPY alembic.ini ./

RUN python -m pip install --no-cache-dir --upgrade pip \
  && python -m pip install --no-cache-dir -e .

CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8080"]
