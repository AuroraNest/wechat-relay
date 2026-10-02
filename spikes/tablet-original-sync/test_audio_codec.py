"""Synthetic decoder fixtures; never read account audio."""
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

import audio_codec as codec


class AudioCodecTests(unittest.TestCase):
    source = b"\x02#!SILK_V3" + b"synthetic"

    def decode_with_stub(self, code):
        with tempfile.TemporaryDirectory() as directory:
            decoder = Path(directory) / "decoder"
            decoder.write_text("#!/usr/bin/python3\nimport pathlib, sys, os, resource\n" + code)
            decoder.chmod(0o700)
            return codec.decode_silk(self.source, decoder)

    def test_wav_and_child_limits_preserve_original(self):
        original = self.source
        result = self.decode_with_stub(
            "assert os.stat('.').st_mode & 0o777 == 0o700\n"
            "assert resource.getrlimit(resource.RLIMIT_CPU) == (20, 20)\n"
            "assert resource.getrlimit(resource.RLIMIT_AS)[0] == 256 * 1024 * 1024\n"
            "assert resource.getrlimit(resource.RLIMIT_FSIZE)[0] == 28800002\n"
            "assert sys.argv[3:] == ['-Fs_API', '24000']\n"
            "assert pathlib.Path(sys.argv[1]).read_bytes() == b'\\x02#!SILK_V3synthetic'\n"
            "pathlib.Path(sys.argv[2]).write_bytes(b'\\x00\\x00\\x01\\x00')\n"
        )
        self.assertEqual(self.source, original)
        with wave.open(io.BytesIO(result), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 24_000))
            self.assertEqual(wav.readframes(2), b"\x00\x00\x01\x00")

    def test_invalid_input_and_missing_decoder(self):
        for data in (b"", b"not silk", b"\x02#!SILK_V3", self.source + b"x" * codec.MAX_INPUT_BYTES):
            with self.assertRaises(codec.AudioDecodeError):
                codec.decode_silk(data, Path("/nonexistent/decoder"))
        with self.assertRaises(codec.AudioDecodeError):
            codec.decode_silk(self.source, Path("/nonexistent/decoder"))

    def test_bad_decoder_output_is_rejected(self):
        cases = (
            "sys.exit(3)\n",
            "pass\n",
            "pathlib.Path(sys.argv[2]).write_bytes(b'')\n",
            "pathlib.Path(sys.argv[2]).write_bytes(b'x')\n",
            "with open(sys.argv[2], 'wb') as output: output.truncate(28800002)\n",
        )
        for code in cases:
            with self.subTest(code=code), self.assertRaises(codec.AudioDecodeError):
                self.decode_with_stub(code)

    def test_timeout_has_no_decoder_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            decoder = Path(directory) / "decoder"
            decoder.touch()
            with patch.object(codec.subprocess, "run", side_effect=subprocess.TimeoutExpired("private", 30, stderr=b"private content")):
                with self.assertRaisesRegex(codec.AudioDecodeError, "^SILK decoding failed$"):
                    codec.decode_silk(self.source, decoder)

    def test_temporary_files_removed_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            decoder = Path(directory) / "decoder"
            decoder.touch()
            scratch = Path(directory) / "scratch"
            scratch.mkdir()
            real_temporary = tempfile.TemporaryDirectory
            with patch.object(codec.tempfile, "TemporaryDirectory", side_effect=lambda **kwargs: real_temporary(dir=scratch, **kwargs)):
                with self.assertRaises(codec.AudioDecodeError):
                    codec.decode_silk(self.source, decoder)
            self.assertEqual(list(scratch.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
