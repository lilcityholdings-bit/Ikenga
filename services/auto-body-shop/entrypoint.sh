#!/bin/sh
# Same fix as services/matching-engine/entrypoint.sh: a platform volume mounted at /app/data
# replaces the image's build-time ownership, so fix it after the mount, then drop privileges.
set -e

chown -R abs:abs /app/data

exec setpriv --reuid=abs --regid=abs --init-groups python3 /app/server.py "$@"
