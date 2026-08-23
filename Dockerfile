FROM python:3.11-slim

# WITH_ARBITER=false builds a ~250MB image with classical CV only.
ARG WITH_ARBITER=true
ARG CLIP_MODEL=openai/clip-vit-base-patch32

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
    TRANSFORMERS_OFFLINE=0

RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-arbiter.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# CPU-only torch + CLIP weights baked in, so the container never needs
# internet at runtime.
RUN if [ "$WITH_ARBITER" = "true" ]; then \
      pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch && \
      pip install --no-cache-dir -r requirements-arbiter.txt && \
      python -c "from transformers import CLIPModel, CLIPProcessor; \
                 CLIPModel.from_pretrained('${CLIP_MODEL}'); \
                 CLIPProcessor.from_pretrained('${CLIP_MODEL}')" ; \
    fi

COPY app ./app

ENV BEDCHECK_CONFIG=/config/config.yaml \
    BEDCHECK_DATA=/data
VOLUME ["/config", "/data"]
EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:8790/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8790"]
