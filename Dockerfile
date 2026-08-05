FROM python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates=20230311+deb12u1 \
        ffmpeg=7:5.1.9-0+deb12u1 \
        gcc=4:12.2.0-3 \
        gir1.2-gst-plugins-base-1.0=1.22.0-3+deb12u6 \
        gir1.2-gstreamer-1.0=1.22.0-2+deb12u1 \
        glib-networking=2.74.0-4 \
        gstreamer1.0-libav=1.22.0-2 \
        gstreamer1.0-plugins-bad=1.22.0-4+deb12u7 \
        gstreamer1.0-plugins-base=1.22.0-3+deb12u6 \
        gstreamer1.0-plugins-base-apps=1.22.0-3+deb12u6 \
        gstreamer1.0-plugins-good=1.22.0-5+deb12u3 \
        gstreamer1.0-plugins-ugly=1.22.0-2+deb12u2 \
        gstreamer1.0-tools=1.22.0-2+deb12u1 \
        libcairo2-dev=1.16.0-7 \
        libffi-dev=3.4.4-1 \
        libgirepository1.0-dev=1.74.0-3 \
        libgstreamer1.0-0=1.22.0-2+deb12u1 \
        libgstreamer-plugins-base1.0-0=1.22.0-3+deb12u6 \
        libgstreamer-plugins-bad1.0-0=1.22.0-4+deb12u7 \
        pkg-config=1.8.1-1 \
    && rm -rf /var/lib/apt/lists/*

RUN command -v gst-discoverer-1.0 \
    && ffmpeg -version | grep -q '^ffmpeg version 5\.1\.9' \
    && gst-inspect-1.0 --version | grep -q 'GStreamer 1\.22\.0' \
    && gst-inspect-1.0 hlsdemux >/dev/null \
    && gst-inspect-1.0 souphttpsrc >/dev/null \
    && gst-inspect-1.0 playbin >/dev/null \
    && gst-inspect-1.0 avdec_h264 >/dev/null

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
COPY uv.lock ./

# PyGObject 必须装进镜像自带的 Python 3.12（Debian python3-gi 绑的是 3.11）。
# 版本与 uv.lock 中 gst extra 对齐，避免 pip 解析漂到需要 girepository-2.0 的 3.52+。
RUN python -m pip install --no-cache-dir ".[gst]" "PyGObject==3.50.2" \
    && python -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None); assert Gst.ElementFactory.find('playbin') is not None"

ENTRYPOINT ["iptv-pipeline-ci"]
