FROM python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates=20230311+deb12u1 \
        ffmpeg=7:5.1.9-0+deb12u1 \
        glib-networking=2.74.0-4 \
        gstreamer1.0-libav=1.22.0-2 \
        gstreamer1.0-plugins-bad=1.22.0-4+deb12u7 \
        gstreamer1.0-plugins-base=1.22.0-3+deb12u6 \
        gstreamer1.0-plugins-base-apps=1.22.0-3+deb12u6 \
        gstreamer1.0-plugins-good=1.22.0-5+deb12u3 \
        gstreamer1.0-plugins-ugly=1.22.0-2+deb12u2 \
        gstreamer1.0-tools=1.22.0-2+deb12u1 \
        libgstreamer1.0-0=1.22.0-2+deb12u1 \
        libgstreamer-plugins-base1.0-0=1.22.0-3+deb12u6 \
        libgstreamer-plugins-bad1.0-0=1.22.0-4+deb12u7 \
    && rm -rf /var/lib/apt/lists/*

RUN command -v gst-discoverer-1.0 \
    && ffmpeg -version | grep -q '^ffmpeg version 5\.1\.9' \
    && gst-inspect-1.0 --version | grep -q 'GStreamer 1\.22\.0' \
    && gst-inspect-1.0 hlsdemux >/dev/null \
    && gst-inspect-1.0 souphttpsrc >/dev/null \
    && gst-inspect-1.0 avdec_h264 >/dev/null

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config

RUN python -m pip install --no-cache-dir .

ENTRYPOINT ["iptv-pipeline-ci"]
