FROM ghcr.io/astral-sh/uv:0.9.6 AS uv

# Build the official stable CLI on the same Debian release as the runtime.
# Bookworm's handbrake-cli is 1.6.1; keep this release and checksum pinned together.
FROM debian:bookworm-slim AS handbrake-build
ARG HANDBRAKE_VERSION=1.11.2
ARG HANDBRAKE_SHA256=12b046350f2422dc28783ff94229aff4ba5fe5e683431e057355d36163b2593a
ARG HANDBRAKE_BUILD_JOBS=4
RUN apt-get update && apt-get install -y --no-install-recommends \
    autoconf automake build-essential cmake curl ca-certificates git \
    libass-dev libbz2-dev libfontconfig-dev libfreetype6-dev libfribidi-dev \
    libharfbuzz-dev libjansson-dev liblzma-dev libmp3lame-dev libnuma-dev \
    libogg-dev libopus-dev libsamplerate0-dev libspeex-dev libtheora-dev \
    libtool libtool-bin libturbojpeg0-dev libvorbis-dev libx264-dev libxml2-dev \
    libvpx-dev m4 make meson nasm ninja-build patch pkg-config python3 tar zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl --fail --location --retry 3 \
    "https://github.com/HandBrake/HandBrake/releases/download/${HANDBRAKE_VERSION}/HandBrake-${HANDBRAKE_VERSION}-source.tar.bz2" \
    --output handbrake.tar.bz2 \
    && echo "${HANDBRAKE_SHA256}  handbrake.tar.bz2" | sha256sum --check --strict \
    && mkdir source && tar -xjf handbrake.tar.bz2 --strip-components=1 -C source \
    && rm handbrake.tar.bz2
WORKDIR /build/source
RUN ./configure --disable-gtk && make --directory=build --jobs=1 contrib.fetch
RUN make --directory=build --jobs="${HANDBRAKE_BUILD_JOBS}" \
    && install -D build/HandBrakeCLI /opt/handbrake/bin/HandBrakeCLI \
    && install -D COPYING /opt/handbrake/share/COPYING \
    && /opt/handbrake/bin/HandBrakeCLI --version

# Official headless Subtitle Edit; both architectures are pinned and verified.
FROM debian:bookworm-slim AS subtitleedit
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
      amd64) asset=x64; checksum=54cc660132e6fed2c24eeca8e869a65b99ff5d3195ac34a962b7e749f1dede7a ;; \
      arm64) asset=ARM64; checksum=6316965ccbf9378468e939749ace34c7c8e1dabdfeede0d8f1c28be46b1674e5 ;; \
      *) exit 1 ;; \
    esac \
    && curl --fail --location --retry 3 \
      "https://github.com/SubtitleEdit/subtitleedit/releases/download/v5.2.0/SeConv-Linux-${asset}.tar.gz" \
      --output /tmp/seconv.tar.gz \
    && echo "$checksum  /tmp/seconv.tar.gz" | sha256sum --check --strict \
    && mkdir /opt/subtitleedit && tar -xzf /tmp/seconv.tar.gz -C /opt/subtitleedit \
    && rm /tmp/seconv.tar.gz

FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    mkvtoolnix ffmpeg mediainfo fonts-liberation fonts-liberation2 fonts-dejavu-core \
    tesseract-ocr tesseract-ocr-all opencc libgomp1 ca-certificates git fonts-noto-core fonts-noto-cjk \
    libass9 libfontconfig1 libfreetype6 libfribidi0 libharfbuzz0b libjansson4 \
    liblzma5 libmp3lame0 libnuma1 libogg0 libopus0 libsamplerate0 libspeex1 \
    libtheora0 libturbojpeg0 libvorbis0a libvorbisenc2 libvpx7 libx264-164 libxml2 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=subtitleedit /opt/subtitleedit /opt/subtitleedit
COPY --from=handbrake-build /opt/handbrake /opt/handbrake
COPY worker/handbrake_cli.py /usr/local/bin/HandBrakeCLI
RUN chmod 755 /usr/local/bin/HandBrakeCLI && HandBrakeCLI --version
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY shared shared
COPY backend backend
COPY worker worker
COPY agent agent
COPY profiles profiles
COPY config config
# Separate locked environments: CRF Studio needs PyAV 18, Sup2Sup needs PyAV <18.
ARG BDRIP_SCRIPTS_REV=529552b7b219f3fb9d7df8ce651c4e5fb265d484
ARG SUP2SUP_REV=608477404527f68d7c4764f694a01118b39c67ba
RUN git init /opt/crf-studio && git -C /opt/crf-studio remote add origin https://github.com/gdjs2/BDRip_Scripts.git && \
    git -C /opt/crf-studio fetch --depth 1 origin "$BDRIP_SCRIPTS_REV" && git -C /opt/crf-studio checkout FETCH_HEAD && \
    uv sync --frozen --no-dev --project /opt/crf-studio --python /usr/local/bin/python
RUN git init /opt/sup2sup && git -C /opt/sup2sup remote add origin https://github.com/gdjs2/Sup2Sup.git && \
    git -C /opt/sup2sup fetch --depth 1 origin "$SUP2SUP_REV" && git -C /opt/sup2sup checkout FETCH_HEAD && \
    uv sync --frozen --no-dev --project /opt/sup2sup --python /usr/local/bin/python
# Local CPU speech recognition stays separate from the media-tool Python environments.
RUN uv venv /opt/audio-review && uv pip install --python /opt/audio-review/bin/python faster-whisper==1.2.1
# Module startup keeps /app on sys.path for imports made later by worker tasks.
CMD ["python", "-m", "celery", "-A", "worker.tasks:celery", "worker", "--loglevel=INFO", "--autoscale=8,1"]
