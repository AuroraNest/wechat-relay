#!/usr/bin/env bash
set -euo pipefail
umask 077
project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
/usr/bin/python3 "$project/spikes/tablet-original-sync/collector.py" pair
systemctl --user restart aurora-tablet-relay.service
systemctl --user is-active aurora-tablet-relay.service
