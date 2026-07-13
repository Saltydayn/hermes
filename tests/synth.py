"""Test-only helpers: a tiny synthetic clip + a representative render param set.

No new pip deps (cv2/numpy are already core). The clip is MJPG/.avi - the most
universally available cv2.VideoWriter combo on Windows - so the suite never depends
on the user's real clip in data/Downloads (locked decision 4, BRIEF_phase2.9).
"""

import os
import tempfile

import cv2
import numpy as np

SRC_W, SRC_H = 320, 180   # synthetic source frame size (16:9, tiny → fast)
SRC_FPS = 30.0
SRC_N = 40                # frames; render tests trim 0..29 inside this


def write_synthetic_clip(path, w=SRC_W, h=SRC_H, fps=SRC_FPS, n=SRC_N):
    """Write a small deterministic clip (gradient + a per-frame moving block, so
    successive frames differ → decode order actually matters). Returns the path.
    Raises RuntimeError if cv2 can't open a writer (codec missing)."""
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    if not vw.isOpened():
        raise RuntimeError("cv2.VideoWriter (MJPG/.avi) unavailable on this machine")
    xs = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
    ys = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    try:
        for i in range(n):
            frame = np.zeros((h, w, 3), np.uint8)
            frame[:, :, 0] = xs           # B ramps across width
            frame[:, :, 1] = ys           # G ramps down height
            x = int((i / max(1, n - 1)) * (w - 20))
            frame[10:40, x:x + 20] = (0, 0, 255)   # moving red block
            vw.write(frame)
    finally:
        vw.release()
    return path


def make_temp_clip(**kw):
    """Create a synthetic clip at a fresh temp path; caller removes it."""
    fd, path = tempfile.mkstemp(suffix=".avi", prefix="sc_test_")
    os.close(fd)
    return write_synthetic_clip(path, **kw)


def make_temp_tone_wav(seconds=1.5, freq=440, rate=44100):
    """A sine-tone WAV (a known audio track for the mux/cut tests), generated via ffmpeg.
    Returns the path; raises RuntimeError if ffmpeg is absent or generation fails - callers
    skip on that. The caller removes the file."""
    import subprocess
    from shared import video_utils
    ff = video_utils.find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg unavailable - skipping audio-mux test")
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="sc_tone_")
    os.close(fd)
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           f"sine=frequency={freq}:duration={seconds}:sample_rate={rate}", path]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc.returncode != 0 or os.path.getsize(path) == 0:
        raise RuntimeError("ffmpeg tone generation failed")
    return path


def sample_watermark():
    """A full watermark dict (text + colors + position) for the compositor tests."""
    return {"text": "DAYN", "scale": 1.0, "icons": (), "icon_mode": "row",
            "avatar_bgra": None, "text_bgr": (255, 255, 255),
            "plate_bgr": (12, 12, 12), "position": "center"}


def sample_render_params(in_f=0, out_f=SRC_N - 10, segments=None):
    """A representative params dict + static layout for the feed-path tests. Two fluid
    keyframes (a cam pan) + a highlight + watermark + border exercise the whole
    compositor; scale_x/y = 1 keeps boxes in source pixels for clarity.

    `segments` (2.10.2b) = the kept source-frame ranges; default = one segment [[in_f,out_f]]
    (today's full-window render). Pass a multi-range list to exercise cut-aware feeds; in_f/
    out_f are re-derived from its outer bounds."""
    if segments is None:
        segments = [[in_f, out_f]]
    in_f, out_f = segments[0][0], segments[-1][1]
    model_highlights = [{"src_box": [0, 0, 80, 45], "dest_box": [40, 40, 200, 200]}]
    keyframes = [
        {"frame": 0, "type": "fluid", "game_box": [0, 0, 320, 90],
         "cam_box": [0, 90, 320, 90], "split_ratio": 50,
         "highlights": [{"src_box": [0, 0, 80, 45], "dest_box": [40, 40, 200, 200]}]},
        {"frame": 20, "type": "fluid", "game_box": [0, 0, 320, 90],
         "cam_box": [20, 90, 300, 90], "split_ratio": 55,
         "highlights": [{"src_box": [10, 0, 80, 45], "dest_box": [60, 40, 200, 200]}]},
    ]
    params = {
        "in_f": in_f, "out_f": out_f,
        "segments": [list(s) for s in segments],
        "keyframes": keyframes,
        "scale_x": 1.0, "scale_y": 1.0,
        "single_panel": False,   # 2.10.1: split-mode render (the production default)
        "melt": True, "melt_px": 50,
        "border": True, "border_color": (15, 15, 15), "border_px": 12,
        "watermark": sample_watermark(),
        "hl_on": True, "highlights": model_highlights,
    }
    static = {"game_box": [0, 0, 320, 90], "cam_box": [0, 90, 320, 90],
              "split_ratio": 50, "highlights": model_highlights}
    return params, static
