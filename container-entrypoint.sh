#!/bin/sh
set -eu
# The managed disk can initially be root-owned. Only its dedicated data
# directory is initialized as root; the MCP server always runs unprivileged.
mkdir -p /var/data/birdie
chown congress:congress /var/data/birdie
chmod 700 /var/data/birdie
exec su -p -s /bin/sh congress -c 'exec python -m congress_api.remote'
