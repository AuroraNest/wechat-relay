# SILK decoder dependency

Source: https://github.com/kn007/silk-v3-decoder

Pinned commit: `507be6bca8ce1fb977a061481f1d79e8c610e309`.
Archive SHA-256: `d9ee6a2c5ee411f29f3ef31b4a43c238d8aaf942640792012db1ee348b147a38`.

The repository wrapper is MIT, Copyright (c) 2020 Karl Chen. The bundled
SILK SDK sources carry Copyright (c) 2006-2012, Skype Limited, with a
BSD-3-Clause-like license that explicitly grants no express or implied patent
rights. The wrapper MIT license does not replace that SDK license.

`build-silk-decoder.sh` builds the host-native CLI outside Git, with the existing
GNU make/GCC/G++ toolchain. It downloads via HTTPS and verifies the pinned archive
checksum before extracting. It installs the executable alongside `LICENSE-MIT`,
`LICENSE-SILK`, and `BUILD-INFO`. No upstream source or binary is committed here.
Redistribution of the binary must retain both installed license notices.

`audio_codec.decode_silk` requires Linux resource limits, invokes the CLI at
24kHz mono signed 16-bit PCM, and uses Python's standard `wave` module to make a
WAV playback copy. It does not transcribe or replace the original SILK asset.
Decoder logs are suppressed, private temporary data is removed after success or
failure, input is capped at 8MiB, and output at ten minutes.
