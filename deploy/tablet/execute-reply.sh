#!/bin/bash
set -euo pipefail
project="$HOME/Aurora/wechat-ipad-relay-research"
exec /bin/bash "$project/android-pad-probe/guest-ssh.sh" python3 /home/probe/tablet-original-sync/guest_reply.py
