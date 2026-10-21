"""faster-whisper access without PyAV.

faster-whisper imports PyAV when it loads, but only uses it to decode audio files
into samples. HERMES already has the audio as a 16 kHz mono 16-bit WAV (made by
ffmpeg), so it reads the samples itself and hands faster-whisper the array.

That lets the release build leave PyAV out entirely: about 69 MB, and its bundled
FFmpeg carries GPL video encoders (x264, x265) that HERMES never used. When PyAV is
not installed, a placeholder module stands in so the faster-whisper import still
succeeds; anything that actually tries to use it raises ImportError.
"""

import sys
import types
import wave

import numpy as np

SAMPLE_RATE = 16000


class _NoPyAV(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise ImportError("PyAV is not bundled; pass decoded samples to faster-whisper")


def _ensure_av_importable():
    try:
        import av  # noqa: F401
    except ImportError:
        sys.modules["av"] = _NoPyAV("av")


def whisper_model_class():
    """faster_whisper.WhisperModel, importable with or without PyAV installed."""
    _ensure_av_importable()
    from faster_whisper import WhisperModel
    return WhisperModel


def load_audio(wav_path):
    """Samples of a 16 kHz mono 16-bit WAV as float32 in [-1, 1), the same values
    faster_whisper.decode_audio returns for that file. Raises ValueError for any
    other format so a wrong file can never be transcribed at the wrong speed."""
    with wave.open(wav_path, "rb") as wf:
        if (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError(
                f"expected 16 kHz mono 16-bit audio, got {wf.getframerate()} Hz, "
                f"{wf.getnchannels()} channel(s), {8 * wf.getsampwidth()}-bit")
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
