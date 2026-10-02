#!/usr/bin/env bash
set -euo pipefail
umask 077

commit=507be6bca8ce1fb977a061481f1d79e8c610e309
checksum=d9ee6a2c5ee411f29f3ef31b4a43c238d8aaf942640792012db1ee348b147a38
destination=${1:-"${XDG_DATA_HOME:-$HOME/.local/share}/aurora-relay/silk-$commit"}
if [[ "$destination" != /* ]]; then
    printf '%s\n' 'Build destination must be an absolute path outside Git.' >&2
    exit 1
fi
mkdir -p "$destination"
if git -C "$destination" rev-parse --show-toplevel >/dev/null 2>&1; then
    printf '%s\n' 'Build destination must be outside Git.' >&2
    exit 1
fi
if [[ -e "$destination/decoder" ]]; then
    printf '%s\n' 'Destination already contains a decoder; choose a fresh directory.' >&2
    exit 1
fi
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --location \
    --connect-timeout 15 --max-time 120 \
    "https://codeload.github.com/kn007/silk-v3-decoder/tar.gz/$commit" \
    --output "$scratch/source.tar.gz"
printf '%s  %s\n' "$checksum" "$scratch/source.tar.gz" | sha256sum --check --status
tar -xzf "$scratch/source.tar.gz" -C "$scratch"
source_directory="$scratch/silk-v3-decoder-$commit"
make -C "$source_directory/silk" -j2
make -C "$source_directory/silk" decoder
install -m 700 "$source_directory/silk/decoder" "$destination/decoder"
install -m 600 "$source_directory/LICENSE" "$destination/LICENSE-MIT"
sed -n '1,/^\*\*.*\/$/p' "$source_directory/silk/src/SKP_Silk_decode_frame.c" > "$destination/LICENSE-SILK"
printf 'commit=%s\narchive_sha256=%s\n' "$commit" "$checksum" > "$destination/BUILD-INFO"
printf '%s\n' "$destination/decoder"
