"""Generate a bounded playback copy without changing the original SILK bytes."""
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

MAX_INPUT_BYTES = 8 * 1024 * 1024
SAMPLE_RATE = 24_000
MAX_PCM_BYTES = SAMPLE_RATE * 2 * 600
DECODE_TIMEOUT = 30

# Set limits in a fresh process, avoiding preexec_fn in a threaded collector.
_LIMITED_EXEC = """
import os, resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_FSIZE, (28800002, 28800002))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.execv(sys.argv[1], sys.argv[1:])
"""


class AudioDecodeError(RuntimeError):
    """A playback copy could not be safely generated; retain the original."""


def decode_silk(data: bytes, decoder: Path) -> bytes:
    """Decode Tencent SILK into mono 24kHz signed 16-bit PCM WAV on Linux.

    The decoder must be the locally built, pinned kn007 executable. Errors never
    include decoder output or source content, because both may contain private data.
    """
    if not 10 < len(data) <= MAX_INPUT_BYTES or not data.startswith(b"\x02#!SILK_V3"):
        raise AudioDecodeError("invalid Tencent SILK input")
    executable = decoder.resolve()
    if not executable.is_file():
        raise AudioDecodeError("SILK decoder unavailable")
    with tempfile.TemporaryDirectory(prefix="relay-audio-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        source = root / "input.silk"
        output = root / "output.pcm"
        source.write_bytes(data)
        source.chmod(0o600)
        try:
            subprocess.run(
                [sys.executable, "-I", "-c", _LIMITED_EXEC, str(executable),
                 str(source), str(output), "-Fs_API", str(SAMPLE_RATE)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=DECODE_TIMEOUT, check=True,
            )
            if not output.is_file():
                raise AudioDecodeError("SILK decoder produced no PCM")
            size = output.stat().st_size
            if size == 0 or size % 2 or size > MAX_PCM_BYTES:
                raise AudioDecodeError("invalid SILK PCM length")
            pcm = output.read_bytes()
        except (OSError, subprocess.SubprocessError):
            raise AudioDecodeError("SILK decoding failed") from None
    result = io.BytesIO()
    with wave.open(result, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return result.getvalue()
