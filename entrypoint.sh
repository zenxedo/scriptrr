#!/bin/sh
# Optional start-time dependency hook for the published scriptrr image.
#
#   EXTRA_APT_PACKAGES  space-separated apt packages (needs root)
#   EXTRA_PIP_PACKAGES  space-separated pip packages
#
# Both are optional and blank by default, so a plain `docker run` stays fast.
set -e

export DEBIAN_FRONTEND=noninteractive

if [ -n "$EXTRA_APT_PACKAGES" ] && [ "$(id -u)" = "0" ]; then
    echo "scriptrr: installing apt packages: $EXTRA_APT_PACKAGES"
    apt-get update -qq
    # shellcheck disable=SC2086
    apt-get install -y -qq --no-install-recommends $EXTRA_APT_PACKAGES
    rm -rf /var/lib/apt/lists/*
elif [ -n "$EXTRA_APT_PACKAGES" ]; then
    echo "scriptrr: EXTRA_APT_PACKAGES set but not running as root; skipping apt" >&2
fi

if [ -n "$EXTRA_PIP_PACKAGES" ]; then
    echo "scriptrr: installing pip packages: $EXTRA_PIP_PACKAGES"
    # shellcheck disable=SC2086
    python3 -m pip install --no-cache-dir --quiet --root-user-action=ignore $EXTRA_PIP_PACKAGES
fi

exec "$@"
