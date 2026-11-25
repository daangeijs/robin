# Wildcam dashboard. CPU-only image (fine for a handful of clips a day on a mini PC).
#   docker build -t ghcr.io/<you>/wildcam:0.1.0 app/
# Build without the model for a small test image:
#   docker build --build-arg WITH_SPECIESNET=0 -t wildcam:mock app/
FROM python:3.12-slim

ARG WITH_SPECIESNET=1
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WILDCAM_DATA_DIR=/data \
    KAGGLEHUB_CACHE=/models \
    HF_HOME=/models/hf

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN useradd --uid 10001 --create-home wildcam \
 && mkdir -p /data /models \
 && chown -R wildcam /data /models

# Dependencies first, against an empty package, so a code change does not reinstall them
# (that is what made every build slow, especially an amd64 build on an arm Mac).
# CPU wheels of torch keep the image a few GB smaller than the default CUDA build.
COPY pyproject.toml ./
RUN mkdir wildcam && touch wildcam/__init__.py \
 && if [ "$WITH_SPECIESNET" = "1" ]; then \
      pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision \
      && pip install ".[speciesnet,deepfaune]"; \
    else \
      pip install .; \
    fi \
 && pip uninstall -y wildcam && rm -rf wildcam

# Claude Code, for reading stills with the token of a Claude subscription instead of an API key.
USER wildcam
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH=/home/wildcam/.local/bin:$PATH

# The code itself: the only layer a normal change rebuilds.
USER root
COPY wildcam ./wildcam
RUN pip install --no-deps .
USER wildcam

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "wildcam.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
