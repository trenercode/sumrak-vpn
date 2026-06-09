FROM python:3.12-slim

ARG GRPCURL_VERSION=1.9.3
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL "https://github.com/fullstorydev/grpcurl/releases/download/v${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION}_linux_x86_64.tar.gz" \
      | tar -xz -C /usr/local/bin grpcurl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml .
COPY app app
RUN pip install --no-cache-dir .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
