# ============================================================
# Selkies Chromium Bridge
#
# Builds Selkies, pixelflux and pcmflux from pinned source
# revisions and adds the Chromium integration bridge.
#
# Target: Linux amd64 / Ubuntu 24.04
# ============================================================


# ------------------------------------------------------------
# SELKIES SOURCE
# ------------------------------------------------------------
FROM ubuntu:24.04 AS selkies-source

ARG DEBIAN_FRONTEND=noninteractive
ARG SELKIES_REF=5b17d3ea6a39c759abd99b12e9250978dd3dd8a5

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

RUN git clone \
      https://github.com/selkies-project/selkies.git \
      selkies && \
    cd selkies && \
    git checkout --detach "${SELKIES_REF}" && \
    test "$(git rev-parse HEAD)" = "${SELKIES_REF}"


# ------------------------------------------------------------
# SELKIES WEB CLIENT
# ------------------------------------------------------------
FROM node:26-alpine AS selkies-web-builder

WORKDIR /build

COPY --from=selkies-source /src/selkies/ ./

RUN sh scripts/ci/build-web.sh


# ------------------------------------------------------------
# SELKIES PYTHON WHEEL
# ------------------------------------------------------------
FROM python:3.12-slim AS selkies-wheel-builder

ENV PIP_RETRIES=5
ENV PIP_TIMEOUT=60

RUN python3 -m pip install \
      --no-cache-dir \
      --upgrade \
      build

WORKDIR /build

COPY --from=selkies-source /src/selkies/ ./

# Replace the ignored/non-generated directory with the web
# bundle produced by the upstream build script.
COPY --from=selkies-web-builder \
     /build/src/selkies/selkies_web \
     ./src/selkies/selkies_web

RUN python3 -m build \
      --wheel \
      --outdir /wheels


# ------------------------------------------------------------
# BUILD PIXELFLUX + PCMFLUX
# ------------------------------------------------------------
FROM ubuntu:24.04 AS native-builder

ARG DEBIAN_FRONTEND=noninteractive

ARG PIXELFLUX_REF=40f01f7752370b3c6962671ca282a4f32456e72d
ARG PCMFLUX_REF=c5487f28b71e5f82211f21b08a8b84c9e08fa48f

ENV PATH=/root/.cargo/bin:$PATH

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      build-essential \
      pkg-config \
      python3 \
      python3-dev \
      python3-pip \
      cmake \
      nasm \
      libclang-dev \
      libavcodec-dev \
      libavfilter-dev \
      libavutil-dev \
      libx264-dev \
      libgbm-dev \
      libdrm-dev \
      libwayland-dev \
      libinput-dev \
      libpixman-1-dev \
      libxkbcommon-dev \
      libva-dev \
      libpulse-dev \
      libopus-dev \
      libx11-dev \
      libxext-dev \
      libxfixes-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl \
      --proto '=https' \
      --tlsv1.2 \
      -sSf \
      https://sh.rustup.rs \
    | sh -s -- -y --profile minimal

WORKDIR /src

RUN git clone \
      https://github.com/selkies-project/pixelflux.git \
      pixelflux && \
    cd pixelflux && \
    git checkout --detach "${PIXELFLUX_REF}" && \
    test "$(git rev-parse HEAD)" = "${PIXELFLUX_REF}"

RUN git clone \
      https://github.com/selkies-project/pcmflux.git \
      pcmflux && \
    cd pcmflux && \
    git checkout --detach "${PCMFLUX_REF}" && \
    test "$(git rev-parse HEAD)" = "${PCMFLUX_REF}"

RUN mkdir -p /wheels && \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    pip3 wheel \
      --no-cache-dir \
      --no-deps \
      --wheel-dir=/wheels \
      ./pixelflux && \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    pip3 wheel \
      --no-cache-dir \
      --no-deps \
      --wheel-dir=/wheels \
      ./pcmflux


# ------------------------------------------------------------
# RUNTIME
# ------------------------------------------------------------
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV XDG_RUNTIME_DIR=/tmp/selkies-runtime
ENV PULSE_RUNTIME_PATH=/run/pulse
ENV PULSE_SERVER=unix:/run/pulse/native

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      python3 \
      python3-pip \
      gnupg \
      xvfb \
      openbox \
      x11-utils \
      x11-xkb-utils \
      x11-xserver-utils \
      libx11-xcb1 \
      libxcb-dri3-0 \
      libxkbcommon0 \
      libxdamage1 \
      libpixman-1-0 \
      libxfixes3 \
      libxtst6 \
      libxext6 \
      libpulse0 \
      libopus0 \
      libx264-164 \
      libavcodec60 \
      libavfilter9 \
      libavutil58 \
      libgbm1 \
      libdrm2 \
      libva2 \
      pulseaudio \
      pulseaudio-utils \
      procps \
      tini \
    && rm -rf /var/lib/apt/lists/*


# ------------------------------------------------------------
# GOOGLE CHROME
# ------------------------------------------------------------
RUN curl -fsSL \
      https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor \
      > /usr/share/keyrings/google-chrome.gpg && \
    echo \
      "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*


# ------------------------------------------------------------
# INSTALL PINNED SELKIES COMPONENTS
# ------------------------------------------------------------
COPY --from=native-builder \
     /wheels \
     /tmp/native-wheels

COPY --from=selkies-wheel-builder \
     /wheels \
     /tmp/selkies-wheels

RUN PIP_BREAK_SYSTEM_PACKAGES=1 \
    pip3 install \
      --no-cache-dir \
      /tmp/native-wheels/pixelflux-*.whl \
      /tmp/native-wheels/pcmflux-*.whl && \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    pip3 install \
      --no-cache-dir \
      /tmp/selkies-wheels/selkies-*.whl && \
    rm -rf \
      /tmp/native-wheels \
      /tmp/selkies-wheels


# ------------------------------------------------------------
# CHROMIUM BRIDGE
# ------------------------------------------------------------
COPY filebridge.py /filebridge.py
COPY start.sh /start.sh

RUN chmod +x /start.sh

EXPOSE 8080 9231

ENTRYPOINT ["/usr/bin/tini","--","/start.sh"]
