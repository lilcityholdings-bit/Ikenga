#!/bin/sh
# Runs as root (the container's default user — see the Dockerfile's comment on why USER ikenga
# was removed) so it can fix ownership on /app/data AFTER a platform's volume gets mounted there,
# then drops to the unprivileged `ikenga` user before ever executing the actual binary.
set -e

chown -R ikenga:ikenga /app/data

exec setpriv --reuid=ikenga --regid=ikenga --init-groups /app/ikenga-matching-engine "$@"
