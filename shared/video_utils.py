"""Shared video toolbox: ffmpeg discovery + wrappers, frame extraction, sharpen.

Pure helpers - no UI, no module globals, no shared mutable state. Anything that opens a
VideoCapture opens its OWN and releases it (never share a capture across threads). The
ffmpeg mux recipe is the proven one from claude-guidance/5_OLD_APP_REFERENCE.md.
"""

import os
import shutil
import subprocess
import time

import cv2
import numpy as np

from shared import paths

# Hide the console window ffmpeg would otherwise flash on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def find_ffmpeg():
    """Locate the ffmpeg binary: system PATH first, then assets/ffmpeg.exe. None if absent."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.path.join(paths.get_path("assets", create=False), "ffmpeg.exe")
    return local if os.path.exists(local) else None


def _run(cmd):
    """Run an ffmpeg command, return (ok, stderr_tail). Never raises on a nonzero exit."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=_NO_WINDOW)
    if proc.returncode == 0:
        return True, ""
    return False, proc.stderr.decode(errors="replace")[-400:]


def extract_audio(src, out_wav):
    """Extract `src` audio to a 16-bit PCM WAV. Returns out_wav on success, None on failure."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    ok, _ = _run([ffmpeg, "-y", "-i", src, "-vn", "-acodec", "pcm_s16le", out_wav])
    return out_wav if ok else None


def has_audio(src):
    """True if `src` has at least one audio stream. Probes with ffmpeg (`-i` prints stream
    info to stderr; the command 'fails' with no output file - expected). False when ffmpeg is
    absent or the probe errors. Used by H264PipeWriter's multi-segment path, where a
    `-filter_complex` referencing `[0:a]` would crash on a silent source (the single-range
    path's `1:a:0?` optional map degrades gracefully on its own, so it needs no probe)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", src],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              creationflags=_NO_WINDOW)
        return b"Audio:" in (proc.stderr or b"")
    except Exception:  # noqa: BLE001 - a failed probe just means "mux video-only"
        return False


def compress_kbps(target_mb, duration_s, audio_kbps=96, margin=0.92):
    """Video bitrate (int kbps) that fits `target_mb`, or None when the result would
    land below the 150 kbps quality floor (the clip is too long for the target).
    1 MB = 8000 kbit (decimal; conservative for platform upload limits). `margin`
    absorbs container overhead and encoder overshoot."""
    if duration_s <= 0:
        return None
    video = (target_mb * 8000.0 * margin) / duration_s - audio_kbps
    return int(video) if video >= 150 else None


def compress_to_size(src, out, video_kbps, audio_kbps=96):
    """Single-pass H.264 re-encode of `src` to a target bitrate (the share
    compressor, 5.5.1d). maxrate/bufsize cap the rate so short spikes cannot blow
    the size budget; faststart keeps the copy web-playable. (ok, err_tail)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg not found (PATH or assets/ffmpeg.exe)"
    v, a = int(video_kbps), int(audio_kbps)
    return _run([ffmpeg, "-y", "-i", src, "-c:v", "libx264", "-preset", "medium",
                 "-b:v", f"{v}k", "-maxrate", f"{v}k", "-bufsize", f"{2 * v}k",
                 "-c:a", "aac", "-b:a", f"{a}k", "-movflags", "+faststart", out])


def mux_audio(video_in, src_for_audio, out, start=None, end=None):
    """Mux audio from `src_for_audio` onto the video-only `video_in`, writing `out`.

    Copies the video stream (no re-encode) and re-encodes audio to AAC 192k. If start/end
    (seconds) are given, the source audio is trimmed to that window - the recipe lifted
    from the old app. The trailing '?' on the audio map makes a missing audio track
    non-fatal (silent video still muxes). Returns (ok, error_tail)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg not found (PATH or assets/ffmpeg.exe)"

    cmd = [ffmpeg, "-y", "-i", video_in]
    if start is not None:
        cmd += ["-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += [
        "-i", src_for_audio,
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        out,
    ]
    return _run(cmd)


# Per-codec quality flags for H264PipeWriter (2.8.5). libx264's row is the original
# 2.6 command line, byte-for-byte; the hw rows target comparable visual quality.
_CODEC_FLAGS = {
    "libx264":    ["-preset", "veryfast", "-crf", "20"],
    "h264_nvenc": ["-preset", "p4", "-cq", "23"],
    "h264_qsv":   ["-global_quality", "23"],
    "h264_amf":   ["-quality", "balanced", "-rc", "cqp",
                   "-qp_i", "23", "-qp_p", "23", "-qp_b", "23"],
}

_HW_CANDIDATES = ("h264_nvenc", "h264_qsv", "h264_amf")
_hw_encoder_cache = "unprobed"   # sentinel; becomes str | None after the first probe


def detect_hw_encoder():
    """First WORKING hardware H.264 encoder of (nvenc, qsv, amf), else None. Cached
    per process. Listed in `ffmpeg -encoders` only proves compiled-in, not usable
    (nvenc stays listed with no NVIDIA GPU) - so each candidate gets a tiny null
    encode probe (~0.2-0.4s, lavfi black 0.1s). 256×256, NOT smaller: NVENC rejects
    128×128 as below its minimum frame dimension - a working GPU would probe as
    absent (found empirically in 2.8.5). Lazy by design: call this only when a hw
    codec is actually wanted, never at startup."""
    global _hw_encoder_cache
    if _hw_encoder_cache != "unprobed":
        return _hw_encoder_cache
    found = None
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        for codec in _HW_CANDIDATES:
            try:
                ret = subprocess.run(
                    [ffmpeg, "-v", "error", "-f", "lavfi",
                     "-i", "color=c=black:s=256x256:r=30:d=0.1",
                     "-c:v", codec, "-f", "null", "-"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15, creationflags=_NO_WINDOW)
                if ret.returncode == 0:
                    found = codec
                    break
            except Exception:  # noqa: BLE001 - a hung/odd probe just means "not this one"
                continue
    _hw_encoder_cache = found
    return found


class H264PipeWriter:
    """Single-pass encoder: raw BGR frames in (stdin) → H.264 mp4 out, with audio muxed
    from a source file in the SAME ffmpeg process - no intermediate video file, exactly
    one encode (added in 2.6; the Phase-3 subtitle burn-in encodes the same way).

    ffmpeg -y -f rawvideo -pix_fmt bgr24 -s WxH -r FPS -i -        ← video: our frames
           [-ss START -to END] -i AUDIO_SRC                        ← audio: trimmed source
           -map 0:v:0 -map 1:a:0? [-af volume=GdB]                 ← '?': missing audio OK
           -c:v CODEC <per-codec quality flags> -pix_fmt yuv420p
           -c:a aac -b:a 192k -shortest OUT

    `codec` (2.8.5) defaults to libx264 with the original flags - only the `-c:v`
    block varies; mux/audio/abort paths are codec-agnostic. `-nostats -loglevel error`
    keeps stderr tiny so reading it only at close() can't deadlock the pipe. Raises
    RuntimeError if ffmpeg is absent - callers pre-flight with find_ffmpeg(), so
    that's a programming-error guard, not a user path.

    `segments` (2.10.2b) makes the mux CUT-AWARE: a list of (start_s, end_s) SECOND ranges
    of source audio to KEEP, in order, joined to match a cut-down video. Each range may
    carry an optional third element seg_db (5.5.2b): a per-segment volume offset applied
    in that range's filter branch, additive with `gain_db`. None → the single
    -ss/-to path above, byte-for-byte unchanged. Given, the audio is `atrim`'d per range,
    `concat`'d, optionally gained, then `apad`'d (silence-padded ≥ the piped video) so
    `-shortest` pins the output to the exact video frame count - which also fixes the
    pre-existing 2.8.5 final-frame-drop edge for cut renders. A segmented render whose
    source has NO audio degrades to video-only (mirrors the single-range '?' optional map)."""

    def __init__(self, out_path, w, h, fps, *, audio_src=None, start=None, end=None,
                 gain_db=0.0, codec="libx264", segments=None):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found (PATH or assets/ffmpeg.exe)")
        self.out_path = out_path
        self.codec = codec
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
               "-r", f"{fps:.6f}", "-i", "-"]
        if segments is None:
            # ── Single-range path (2.6/2.8.5) - UNCHANGED, byte-for-byte ──
            if audio_src is not None:
                if start is not None:
                    cmd += ["-ss", str(start)]
                if end is not None:
                    cmd += ["-to", str(end)]
                cmd += ["-i", audio_src]
            cmd += ["-map", "0:v:0"]
            if audio_src is not None:
                cmd += ["-map", "1:a:0?"]
                if gain_db:
                    cmd += ["-af", f"volume={gain_db}dB"]
                cmd += ["-c:a", "aac", "-b:a", "192k"]
        elif audio_src is not None and has_audio(audio_src):
            # ── Multi-segment path: atrim each kept range → concat → gain → apad ──
            cmd += ["-i", audio_src,
                    "-filter_complex", self._segment_filtergraph(segments, gain_db),
                    "-map", "0:v:0", "-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        else:
            # ── Segmented render, no source audio → video only ──
            cmd += ["-map", "0:v:0"]
        cmd += ["-c:v", codec, *_CODEC_FLAGS.get(codec, []),
                "-pix_fmt", "yuv420p", "-shortest", out_path]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE,
                                      creationflags=_NO_WINDOW)

    @staticmethod
    def _segment_filtergraph(segments, gain_db):
        """Build the cut-aware audio `-filter_complex` for K kept ranges: atrim each,
        reset PTS, concat in order, optional whole-stream gain, then apad. apad pads
        with silence so the audio is ≥ the piped video → `-shortest` clamps output to
        the exact video length (no final-frame drop).

        Ranges are (start_s, end_s) or (start_s, end_s, seg_db) tuples (5.5.2b): a
        nonzero seg_db adds a per-branch volume BEFORE the concat, additive with the
        post-concat whole-clip gain by construction. All offsets zero produces the
        exact pre-5.5.2b graph.

        Audio is input #1 - the rawvideo stdin is input #0 - so the source label is `[1:a]`."""
        parts, labels = [], []
        for i, seg in enumerate(segments):
            s, e = seg[0], seg[1]
            db = float(seg[2]) if len(seg) > 2 else 0.0
            vol = f",volume={db}dB" if db else ""
            parts.append(f"[1:a]atrim=start={s:.6f}:end={e:.6f},"
                         f"asetpts=N/SR/TB{vol}[a{i}]")
            labels.append(f"[a{i}]")
        parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=0:a=1[ac]")
        gain = f"volume={gain_db}dB," if gain_db else ""
        parts.append(f"[ac]{gain}apad[aout]")
        return ";".join(parts)

    def write(self, bgr_frame):
        """Pipe one raw BGR frame. Raises OSError (broken pipe) if ffmpeg died -
        callers catch and pull the reason from close()."""
        self._proc.stdin.write(bgr_frame.tobytes())

    def close(self):
        """Finish the encode: close stdin, wait, return (ok, stderr_tail)."""
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        err = self._proc.stderr.read()
        ret = self._proc.wait()
        if ret == 0:
            return True, ""
        return False, err.decode(errors="replace")[-400:]

    def abort(self):
        """Kill the encode and remove the partial output. Never raises (used from
        cancel paths and shutdown, where nothing may crash). Kill comes FIRST -
        closing stdin first would signal EOF and race ffmpeg's finalize against the
        kill. Windows releases the output file handle a beat after TerminateProcess,
        so the remove retries briefly."""
        try:
            self._proc.kill()
        except Exception:  # noqa: BLE001 - cleanup must never crash a cancel
            pass
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(10):
            try:
                if os.path.exists(self.out_path):
                    os.remove(self.out_path)
                break
            except OSError:
                time.sleep(0.1)


def probe_video(path):
    """Read basic metadata from a video: (width, height, fps, frame_count) or None.

    Opens and releases its OWN capture (never shares one across threads). Returns None if
    the file can't be opened or decoded - never raises, so a bad path can't crash a caller.
    `fps` is 0.0 and `frame_count` may be 0 when the container doesn't report them; callers
    treat those as 'unknown'."""
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return None
        ok, _frame = cap.read()
        if not ok:
            return None
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        return w, h, fps, n
    except Exception:  # noqa: BLE001 - never raise; a bad file just yields None
        return None
    finally:
        cap.release()


def extract_frame(path, index):
    """Decode a single BGR frame at `index` from `path`. Opens and releases its own
    capture. Returns the frame (np.ndarray) or None if it can't be read."""
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(index)))
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


def sharpen(frame, strength=1.0):
    """Unsharp-mask sharpen a BGR frame. strength 0 = no-op; ~0.5-1.5 typical.

    out = frame + strength * (frame - gaussian_blur(frame)), clipped to uint8."""
    if strength <= 0:
        return frame
    blurred = cv2.GaussianBlur(frame, (0, 0), sigmaX=3)
    sharpened = cv2.addWeighted(frame, 1.0 + strength, blurred, -strength, 0)
    return np.clip(sharpened, 0, 255).astype(np.uint8)
