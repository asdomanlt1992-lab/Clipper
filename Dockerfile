FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY clipper ./clipper
# Includes the Claude classifier and local transcription (faster-whisper).
RUN pip install --no-cache-dir '.[ai]'
ENV CLIPPER_DATA_DIR=/data \
    HF_HOME=/data/models
VOLUME /data
EXPOSE 8000
CMD ["clipper"]
