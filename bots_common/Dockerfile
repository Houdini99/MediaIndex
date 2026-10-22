# One image recipe for both bots. Built from the repository root, so the
# image can carry bots_common/ beside the bot's own package; BOT picks the
# bot (./build.sh passes it):
#   docker build --build-arg BOT=discord --build-arg YTDLP_REFRESH=$(date +%s) \
#     -f bots_common/Dockerfile -t index-discord-bot:latest .
#   docker build --build-arg BOT=telegram --build-arg YTDLP_REFRESH=$(date +%s) \
#     -f bots_common/Dockerfile -t index-telegram-bot:latest .
FROM python:3.11-slim

# ffmpeg: yt-dlp merges separate video+audio streams into one mp4.
# deno: yt-dlp's YouTube extractor runs site JavaScript through it —
# without a JS runtime, extraction is deprecated and formats go missing.
# Both are here only for the link resolver (a sender pasting a YouTube
# URL); an uploaded photo or video is still moved byte-for-byte and
# decoded by the web container, which is hardened for it.
#
# deno is pinned and checksum-verified: `releases/latest` with no check
# would mean an unreviewed binary, different on every rebuild, running in
# the container that fetches attacker-chosen URLs. To upgrade,
# bump DENO_VERSION and take both sums from the release's
# deno-<arch>-unknown-linux-gnu.zip.sha256sum assets.
#
# Nothing above `ARG BOT` depends on the bot, so both images share these
# layers.
ARG DENO_VERSION=2.9.6
ARG DENO_SHA256_X86_64=394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2
ARG DENO_SHA256_AARCH64=9a46afc6c392c7cd2ff71a31558935545b46408d0e87f7a86908c712721c046e
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && case "$(dpkg --print-architecture)" in \
         arm64) DENO_ARCH=aarch64; DENO_SHA256="$DENO_SHA256_AARCH64" ;; \
         *)     DENO_ARCH=x86_64;  DENO_SHA256="$DENO_SHA256_X86_64" ;; esac \
    && curl -fsSL "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${DENO_ARCH}-unknown-linux-gnu.zip" \
       -o /tmp/deno.zip \
    && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip

WORKDIR /bot

# The Telegram bot does NOT run as root (it shares the Bot API server's
# uid so it can delete what that server downloads), and uid 101 has no
# home directory here. Both yt-dlp and deno want somewhere to cache; left
# to default they would try to write under / and spend every YouTube
# resolve re-fetching the JS challenge solver. The Discord bot runs as
# root and caches in the same place.
ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    DENO_DIR=/tmp/.cache/deno

ARG BOT
RUN case "$BOT" in discord|telegram) ;; \
      *) echo "build with --build-arg BOT=discord or BOT=telegram" >&2; exit 1 ;; esac

COPY ${BOT}_bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp is installed here and not in requirements.txt: sites break old
# extractors constantly, so the newest yt-dlp is always wanted, but the
# layer above is cached on requirements.txt, which rarely changes, so a
# plain rebuild would happily reuse a months-old resolver. Changing this
# arg busts the cache from here down, and ./build.sh passes the current
# timestamp, so the resolver really is refreshed on every rebuild.
# yt-dlp-ejs bundles the YouTube JS-challenge solver scripts (run through
# deno); the curl-cffi extra enables Chrome impersonation for sites that
# 403 non-browser clients.
#
# bgutil-ytdlp-pot-provider is yt-dlp's plugin for the `bgutil` PO-token
# sidecar (see docker-compose.yml.example). Unlike yt-dlp it is pinned,
# to the version of the server pinned by digest in the compose file: a
# plugin refuses a server of another major version outright, and YouTube
# then silently goes without PO tokens. Upgrade the two together.
ARG YTDLP_REFRESH=0
RUN pip install --no-cache-dir --upgrade 'yt-dlp[curl-cffi]' yt-dlp-ejs \
        'bgutil-ytdlp-pot-provider==2.0.2' \
    && python -c "import yt_dlp; print('yt-dlp', yt_dlp.version.__version__)"

# Both packages live under /bot and the bot runs with -m, so its
# package-relative imports and `import bots_common` both resolve.
COPY bots_common ./bots_common/
COPY ${BOT}_bot ./${BOT}_bot/

# CMD can't read a build arg, so the module is kept as an env var; `exec`
# replaces the shell, so python is still PID 1 and gets the stop signal.
ENV BOT_MODULE=${BOT}_bot.bot
CMD ["sh", "-c", "exec python -m \"$BOT_MODULE\""]
