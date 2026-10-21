"""Shared video toolbox: ffmpeg discovery + wrappers, frame extraction, sharpen.

Pure helpers - no UI, no module globals, no shared mutable state. Anything that opens a
VideoCapture opens its OWN and releases it (never share a capture across threads). The
ffmpeg mux recipe is the one proven in the original clip tool.
"""

import os
import re
import shutil
import subprocess
import threading
import time
import zipfile

import cv2
import numpy as np

from shared import downloader, paths

# Hide the console window ffmpeg would otherwise flash on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Pinned ffmpeg source for on-demand download (9.3b). A SPECIFIC versioned gyan.dev
# essentials build (a .zip, so stdlib zipfile extracts it - the full build is .7z and
# would need a non-stdlib extractor). Pinned by URL AND sha256 so the download is
# integrity-verified; the moving "latest" URL can't be pinned since its checksum changes
# on every ffmpeg release. If gyan ever rotates this exact package out, the fetch fails
# gracefully (checksum/404) and a future release updates these three constants.
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip"
# Same file on gyan.dev's GitHub release archive, tried when the main URL fails (gyan.dev
# drops older packages over time; the archive keeps them). The sha256 below still has to
# match, so a different file is rejected exactly as before.
FFMPEG_FALLBACK_URLS = (
    "https://github.com/GyanD/codexffmpeg/releases/download/8.1.2/"
    "ffmpeg-8.1.2-essentials_build.zip",
)
FFMPEG_SHA256 = "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec"
FFMPEG_DL_BYTES = 109728040


def ffmpeg_download_target():
    """Absolute path where a downloaded ffmpeg.exe lives."""
    return os.path.join(paths.get_path("bin"), "ffmpeg.exe")


def find_ffmpeg():
    """Locate the ffmpeg binary: system PATH first, then a previously downloaded copy,
    then assets/ffmpeg.exe (legacy/dev fallback - the build no longer bundles it as of
    9.3b). None if absent."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    downloaded = ffmpeg_download_target()
    if os.path.exists(downloaded):
        return downloaded
    local = os.path.join(paths.get_path("assets", create=False), "ffmpeg.exe")
    return local if os.path.exists(local) else None


def has_downloaded_ffmpeg():
    """True if HERMES's own downloaded copy exists at ffmpeg_download_target()."""
    return os.path.exists(ffmpeg_download_target())


def find_ffmpeg_excluding_downloaded():
    """Same discovery order as find_ffmpeg() but skips the downloaded bin/ffmpeg.exe
    copy. Used to check whether removing the downloaded copy would leave any
    ffmpeg-needing module without a working ffmpeg (PATH or the assets/ dev fallback)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.path.join(paths.get_path("assets", create=False), "ffmpeg.exe")
    return local if os.path.exists(local) else None


def _extract_ffmpeg_exe(zip_path, target):
    """Pull the ffmpeg.exe member out of `zip_path` to `target` via a temp .part +
    atomic os.replace (never leaves a half-written exe find_ffmpeg() could pick up).
    Returns (ok, error). Cleans up its own .part on any failure."""
    part = target + ".part"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            member = next(
                (n for n in zf.namelist() if n.replace("\\", "/").endswith("bin/ffmpeg.exe")),
                None)
            if member is None:
                return False, "ffmpeg.exe not found inside the downloaded archive"
            with zf.open(member) as src, open(part, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.replace(part, target)
        return True, ""
    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"extraction failed: {exc}"
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass


def download_ffmpeg(on_progress=None, cancel_event=None):
    """Fetch the pinned ffmpeg zip, verify it, and extract just ffmpeg.exe to
    ffmpeg_download_target(). Headless - never touches tkinter, never raises. Returns a
    downloader.DownloadResult-shaped object (ok / canceled / error); on success `.path`
    is the extracted exe. on_progress/cancel_event pass straight through to the fetch
    phase (downloader.download_file); extraction itself is fast enough not to need
    progress or cancellation of its own."""
    zip_path = os.path.join(paths.get_path("bin"), "_ffmpeg_dl.zip")
    try:
        result = None
        for url in (FFMPEG_URL,) + FFMPEG_FALLBACK_URLS:
            result = downloader.download_file(
                url, zip_path,
                expected_sha256=FFMPEG_SHA256, expected_size=FFMPEG_DL_BYTES,
                on_progress=on_progress, cancel_event=cancel_event)
            if result.ok or getattr(result, "canceled", False):
                break
        if not result.ok:
            return result

        target = ffmpeg_download_target()
        ok, err = _extract_ffmpeg_exe(zip_path, target)
        if not ok:
            return downloader.DownloadResult(ok=False, error=err)
        return downloader.DownloadResult(ok=True, path=target)
    finally:
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass


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


def _cut_audio_filtergraph(segments):
    """Pure filter_complex string builder for extract_cut_audio: atrim each KEPT
    (start_s, end_s) range off the single audio input `[0:a]`, reset PTS, concat in
    order. Kept separate from `H264PipeWriter._segment_filtergraph` on purpose - that
    one is hardcoded to input `[1:a]` (audio sits behind the rawvideo pipe) and ends
    in `apad` for `-shortest`; here audio is the ONLY input and no pad is wanted, so
    parameterising the render's hot path just to share this is higher risk than a
    small parallel builder. Split out from extract_cut_audio so the exact string is
    unit-testable without ffmpeg."""
    parts, labels = [], []
    for i, (s, e) in enumerate(segments):
        parts.append(f"[0:a]atrim=start={float(s):.6f}:end={float(e):.6f},"
                     f"asetpts=N/SR/TB[a{i}]")
        labels.append(f"[a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=0:a=1[out]")
    return ";".join(parts)


def extract_cut_audio(src, segments, out_wav):
    """Build the FINISHED-clip audio (removed ranges dropped) as a mono 16kHz 16-bit
    PCM WAV, so word timestamps land on the finished timeline. `segments` is a list
    of (start_s, end_s) KEPT ranges in seconds, in order; None or a single whole-clip
    range behaves like extract_audio (no filtergraph, just a resample). Returns
    out_wav on success, None on failure, when ffmpeg is absent, or when `src` has no
    audio stream (a clean "nothing to transcribe" state, never a crash)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not has_audio(src):
        return None
    if not segments or (len(segments) == 1 and segments[0][0] <= 0):
        ok, _ = _run([ffmpeg, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
                      "-acodec", "pcm_s16le", out_wav])
        return out_wav if ok else None
    graph = _cut_audio_filtergraph(segments)
    ok, _ = _run([ffmpeg, "-y", "-i", src, "-filter_complex", graph,
                  "-map", "[out]", "-ac", "1", "-ar", "16000",
                  "-acodec", "pcm_s16le", out_wav])
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


def parse_frame_rates(ffmpeg_stderr):
    """(average fps, base rate "tbr") of the first video stream in `ffmpeg -i`
    output, or (None, None). Pure - split out so it is testable without ffmpeg."""
    for line in (ffmpeg_stderr or "").splitlines():
        if "Video:" not in line:
            continue
        fps = re.search(r",\s*([\d.]+)\s*fps", line)
        tbr = re.search(r",\s*([\d.]+)(k?)\s*tbr", line)
        if not fps or not tbr:
            return None, None
        base = float(tbr.group(1)) * (1000.0 if tbr.group(2) else 1.0)
        return float(fps.group(1)), base
    return None, None


def is_variable_frame_rate(path, tolerance=0.02):
    """True when the clip's average frame rate differs from its base rate by more
    than `tolerance` - a variable-frame-rate recording (phones, some screen
    recorders). The editor treats frames as evenly spaced, so on such a clip the
    picture drifts against the sound. False when unknown."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              creationflags=_NO_WINDOW, timeout=15)
    except Exception:  # noqa: BLE001 - a failed probe is "unknown", never an error
        return False
    fps, tbr = parse_frame_rates((proc.stderr or b"").decode(errors="replace"))
    if not fps or not tbr:
        return False
    return abs(fps - tbr) / tbr > tolerance


def peaks_from_pcm(pcm, buckets=2000):
    """Per-bucket |peak| amplitudes (float32 0..1) from an int16 PCM array
    (n_samples, n_channels) - peak across channels, then bucket max. Promoted
    out of the editor's waveform loader (6.5 C1) so Subtitle Studio's timeline
    can draw the same waveform from the PCM it already caches for playback,
    without duplicating the DSP. Fixed bucket count - the caller maps buckets
    to whatever pixel width it's drawing. None on empty/too-short input."""
    samples = np.abs(pcm.astype(np.int32)).max(axis=1)
    buckets = min(buckets, len(samples))
    if buckets <= 0:
        return None
    usable = (len(samples) // buckets) * buckets
    if usable == 0:
        return None
    peaks = samples[:usable].reshape(buckets, -1).max(axis=1)
    return (peaks / 32767.0).astype(np.float32)


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
        # stderr is drained continuously instead of only at close(): if ffmpeg ever
        # printed more than the pipe buffer (4 KB on Windows) mid-encode, it would
        # block on that write while we block on stdin - a render that never ends
        # and a Cancel that cannot interrupt it. Only the tail is kept.
        self._err_tail = bytearray()
        self._err_thread = threading.Thread(target=self._drain_stderr,
                                            name="sc-ffmpeg-stderr", daemon=True)
        self._err_thread.start()

    def _drain_stderr(self):
        try:
            for chunk in iter(lambda: self._proc.stderr.read(4096), b""):
                self._err_tail += chunk
                if len(self._err_tail) > 8192:
                    del self._err_tail[:-4096]
        except (OSError, ValueError):
            pass

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
        ret = self._proc.wait()
        self._err_thread.join(timeout=5)
        if ret == 0:
            return True, ""
        return False, bytes(self._err_tail).decode(errors="replace")[-400:]

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
