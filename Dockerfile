FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        glib-networking \
        gstreamer1.0-libav \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-base-apps \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-tools \
    && rm -rf /var/lib/apt/lists/*

RUN command -v gst-discoverer-1.0 \
    && gst-inspect-1.0 hlsdemux >/dev/null \
    && gst-inspect-1.0 souphttpsrc >/dev/null \
    && gst-inspect-1.0 avdec_h264 >/dev/null

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config

RUN python -m pip install --no-cache-dir .

ENTRYPOINT ["iptv-pipeline-ci"]
