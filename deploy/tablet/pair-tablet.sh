#!/usr/bin/env bash
set -euo pipefail
umask 077
# SSH may omit this even while the lingering user manager is running.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
/usr/bin/python3 "$project/spikes/tablet-original-sync/collector.py" pair
systemctl --user restart aurora-tablet-relay.service
systemctl --user is-active aurora-tablet-relay.service
