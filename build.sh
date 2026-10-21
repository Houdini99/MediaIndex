#!/usr/bin/env bash
#
# Build every image the stack builds locally, in one go.
#
# docker-compose.yml deploys *prebuilt* images rather than building them —
# that is what keeps it usable as a Portainer web-editor stack — so the
# three images below have to exist in the local daemon before the stack
# comes up. Each Dockerfile documented its own build command and they were
# copied out by hand one at a time; this is those commands, in one place,
# with the arguments that are easy to forget.
#
#   ./build.sh                  build all three
#   ./build.sh web              build only index-app
#   ./build.sh discord telegram build a subset
#   ./build.sh --no-cache       any -flag is passed through to docker build
#   ./build.sh --pull web       flags and targets can be mixed
#
# The other services in the stack (postgres, redis, the backup sidecar,
# telegram-bot-api, bgutil) run upstream images and are pulled, never built.

set -euo pipefail

# Always build from the repo root: it is every image's context, so running
# this from anywhere else would either fail or — worse — build a different
# directory tree.
cd "$(dirname "$(readlink -f "$0")")"

# name|tag|dockerfile. The name is what you type on the command line. Every
# image builds from the repo root: the bots copy bots_common/ in beside their
# own package, so their context cannot be their own directory any more.
IMAGES=(
  "web|index-app:latest|Dockerfile"
  "discord|index-discord-bot:latest|discord_bot/Dockerfile"
  "telegram|index-telegram-bot:latest|telegram_bot/Dockerfile"
)

DOCKER_ARGS=()
TARGETS=()

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      cat <<'USAGE'
Build every image the stack builds locally, in one go.

  ./build.sh                   build all three
  ./build.sh web               build only index-app
  ./build.sh discord telegram  build a subset
  ./build.sh --no-cache        any -flag is passed through to docker build
  ./build.sh --pull web        flags and targets can be mixed

Images: web (index-app), discord (index-discord-bot), telegram (index-telegram-bot).
postgres, redis, the backup sidecar, telegram-bot-api and bgutil are upstream
images and are pulled by the stack, never built here.
USAGE
      exit 0
      ;;
    -*)
      DOCKER_ARGS+=("$arg")
      ;;
    *)
      TARGETS+=("$arg")
      ;;
  esac
done

# No targets named means all of them.
if [ ${#TARGETS[@]} -eq 0 ]; then
  for entry in "${IMAGES[@]}"; do TARGETS+=("${entry%%|*}"); done
fi

# Validate every target before building anything, so a typo in the third
# argument does not surface only after two images have already been built.
for target in "${TARGETS[@]}"; do
  found=""
  for entry in "${IMAGES[@]}"; do
    [ "${entry%%|*}" = "$target" ] && found=1 && break
  done
  if [ -z "$found" ]; then
    echo "!! unknown image '$target'" >&2
    echo "   known: web discord telegram" >&2
    exit 2
  fi
done

if ! command -v docker >/dev/null 2>&1; then
  echo "!! docker is not on PATH" >&2
  exit 1
fi

build_one() {
  local name="$1" tag="$2" dockerfile="$3"
  local extra=()

  # yt-dlp is pinned with a floor, not an exact version, because sites
  # break old extractors constantly — but pip's layer is cached on
  # requirements.txt, which never changes, so a plain rebuild would reuse a
  # months-old resolver. Changing this arg busts the cache from there down.
  # This is the argument that gets forgotten when building by hand: both
  # bot Dockerfiles declare it, and a bot built without it stays on
  # whatever yt-dlp it was first built with.
  if [ "$name" = "discord" ] || [ "$name" = "telegram" ]; then
    extra+=(--build-arg "YTDLP_REFRESH=$(date +%s)")
  fi

  echo
  echo "→ building $tag  (dockerfile: $dockerfile)"
  docker build "${extra[@]}" "${DOCKER_ARGS[@]+"${DOCKER_ARGS[@]}"}" \
    -f "$dockerfile" -t "$tag" .
}

start=$SECONDS
built=()

for target in "${TARGETS[@]}"; do
  for entry in "${IMAGES[@]}"; do
    IFS='|' read -r name tag dockerfile <<< "$entry"
    if [ "$name" = "$target" ]; then
      build_one "$name" "$tag" "$dockerfile"
      built+=("$tag")
    fi
  done
done

echo
echo "→ built ${#built[@]} image(s) in $((SECONDS - start))s:"
for tag in "${built[@]}"; do
  # Report the id and size actually in the daemon now, so a silently
  # cached no-op build is distinguishable from a real one.
  printf '   %s\n' "$(docker image ls --format '{{.Repository}}:{{.Tag}}  {{.ID}}  {{.Size}}' "$tag" | head -1)"
done
echo
echo "   next: update the stack in Portainer, or 'docker compose up -d'"
