"""Pure 1080×1920 compositor + its image helpers (extracted from clip_editor in 2.9.2).

`composite_frame(...)` is the PURE module-level function - inputs in, frame out, zero
widget reads - so playback (2.4) and the render thread (2.6/2.8.4) can call it from
anywhere. Box coordinates are CANVAS space (relative to the displayed source image; the
caller subtracts off_x/off_y first) and the compositor multiplies by scale_x/scale_y to
reach source pixels. Out-px params (melt zone, watermark metrics, dest boxes) are defined
in the 1080×1920 reference space and scale to the actual out size.

Behavior is byte-for-byte identical to the pre-2.9.2 in-clip_editor version - this file is
a verbatim move, no logic changes.
"""

import os

import cv2
import numpy as np

OUT_W, OUT_H = 1080, 1920   # the vertical Short output geometry

# Platform icons the watermark can show left of the name. The user drops the PNGs into
# assets/ (transparency supported - alpha-composited). Each entry lists accepted
# filenames (first match wins). Loaded ONCE per run, never per frame.
_PLATFORM_FILES = {"twitch": ("twitch_icon.png", "twitch.png"),
                   "youtube": ("youtube_icon.png", "youtube.png")}
_ICON_CACHE = {}


def _platform_icon(assets_dir, name):
    """Load a platform icon ONCE (BGRA when the PNG has alpha). None when absent - the
    watermark then simply skips that icon (assets/ may legitimately ship empty)."""
    if name not in _ICON_CACHE:
        img = None
        for fname in _PLATFORM_FILES.get(name, ()):
            path = os.path.join(assets_dir, fname)
            if os.path.exists(path):
                img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
                break
        _ICON_CACHE[name] = img
    return _ICON_CACHE[name]


def _hex_to_bgr(hex_str, fallback):
    """'#rrggbb' → BGR tuple for cv2 drawing; anything malformed → fallback."""
    s = (hex_str or "").lstrip("#")
    if len(s) != 6:
        return fallback
    try:
        return (int(s[4:6], 16), int(s[2:4], 16), int(s[0:2], 16))
    except ValueError:
        return fallback


def _blit(canvas, img, x, y):
    """Paste BGR `img` onto `canvas` at (x, y), clipped to the canvas bounds (a huge
    watermark scale or long name must never index out of range)."""
    h, w = img.shape[:2]
    ch, cw = canvas.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(cw, x + w), min(ch, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    canvas[y0:y1, x0:x1] = img[y0 - y:y1 - y, x0 - x:x1 - x]


def _resize_smart(img, w, h):
    """cv2.resize with INTER_AREA when shrinking - markedly sharper on downscaled
    high-contrast content (game UI text) than the default bilinear, which is what the
    panels/PiPs do most of the time - INTER_LINEAR when growing (2.5c)."""
    ih, iw = img.shape[:2]
    interp = cv2.INTER_AREA if (w < iw or h < ih) else cv2.INTER_LINEAR
    return cv2.resize(img, (w, h), interpolation=interp)


def _blit_bgra(canvas, img, x, y):
    """Alpha-composite a BGRA `img` onto `canvas` at (x, y), clipped. BGR input falls
    back to a plain blit."""
    if img.ndim != 3 or img.shape[2] != 4:
        _blit(canvas, img if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), x, y)
        return
    h, w = img.shape[:2]
    ch, cw = canvas.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(cw, x + w), min(ch, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sub = img[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.float32)
    alpha = sub[:, :, 3:4] / 255.0
    region = canvas[y0:y1, x0:x1].astype(np.float32)
    canvas[y0:y1, x0:x1] = (sub[:, :, :3] * alpha + region * (1.0 - alpha)).astype(np.uint8)


def _burn_watermark(canvas, out_w, boundary_y, wm, k=1.0):
    """Burn the centered name plate onto the seam (ported from the old app's proven
    block; extended with multiple platform icons - side-by-side or stacked - left of the
    name, plus user-pickable text/plate colors). Every asset is optional: missing
    icons ⇒ skipped, text always draws - assets/ may legitimately ship empty.

    `k` = output scale (out_h / 1920): EVERY metric - font, icons, gaps, plate
    paddings - scales by it so preview-res composites (playback, fast drags) show the
    plate at the same relative size as the full-res render (2.5c; at k=1 the math is
    identical to the original)."""
    text = str(wm.get("text", "")).upper()
    icons = [i for i in (wm.get("icons") or ()) if i is not None]
    if not text and not icons:
        return
    try:
        font_scale = float(wm.get("scale", 1.0))
    except (TypeError, ValueError):
        font_scale = 1.0
    font_scale *= k
    text_bgr = wm.get("text_bgr") or (255, 255, 255)
    plate_bgr = wm.get("plate_bgr") or (12, 12, 12)
    stack = wm.get("icon_mode") == "stack"
    pad8 = max(1, int(round(8 * k)))
    pad15 = max(1, int(round(15 * k)))
    pad25 = max(1, int(round(25 * k)))

    font = cv2.FONT_HERSHEY_DUPLEX
    thickness = max(1, int(font_scale * 2))
    (text_w, text_h), _ = cv2.getTextSize(text, font, font_scale, thickness)
    icon_w = max(1, int(44 * font_scale))
    gap = max(max(1, int(round(4 * k))), int(6 * font_scale))

    if icons:
        # Scale every icon to a common HEIGHT, width follows its own aspect (the YouTube
        # logo is wide; forcing a square would squash it).
        sized = []
        for icon in icons:
            ih, iw = icon.shape[:2]
            w = max(1, int(round(icon_w * iw / max(1, ih))))
            sized.append(cv2.resize(icon, (w, icon_w)))
        n = len(sized)
        if stack:
            block_w = max(s.shape[1] for s in sized)
            block_h = n * icon_w + (n - 1) * gap
        else:
            block_w = sum(s.shape[1] for s in sized) + (n - 1) * gap
            block_h = icon_w
    else:
        sized = []
        block_w = block_h = 0

    total_w = text_w
    if icons:
        total_w += block_w + pad15
    # Plate position (2.8.8): left/right park the plate edge 24*k px from the
    # output edge (clamped so a wide plate never runs off-frame); center is the
    # ORIGINAL line untouched - old dicts without "position" render unchanged.
    position = wm.get("position", "center")
    margin = max(1, int(round(24 * k)))
    if position == "left":
        start_x = min(margin + pad25, max(pad25, out_w - total_w - pad25))
    elif position == "right":
        start_x = max(pad25, out_w - margin - total_w - pad25)
    else:
        start_x = (out_w - total_w) // 2

    plate_h = max(int(text_h * 1.4), block_h // 2 + pad8)
    cv2.rectangle(canvas, (start_x - pad25, boundary_y - plate_h),
                  (start_x + total_w + pad25, boundary_y + plate_h), plate_bgr, -1)

    x = start_x
    if sized:
        if stack:
            iy = boundary_y - block_h // 2
            for icon in sized:
                # Center each icon horizontally within the block column.
                _blit_bgra(canvas, icon, x + (block_w - icon.shape[1]) // 2, iy)
                iy += icon_w + gap
        else:
            ix = x
            for icon in sized:
                _blit_bgra(canvas, icon, ix, boundary_y - icon_w // 2)
                ix += icon.shape[1] + gap
        x += block_w + pad15
    if text:
        cv2.putText(canvas, text, (x, boundary_y + text_h // 2), font, font_scale,
                    text_bgr, thickness, cv2.LINE_AA)


def _draw_branding_item(canvas, out_w, out_h, item):
    """Blit one independent branding/watermark image overlay (5.5.1g: generalized from
    the single 5.5.1f avatar into a list, free width/height, opacity). `box` is
    [x, y, w, h] in the 1080x1920 reference space, scaled to the actual out size
    exactly like a highlight's dest_box, so overlays place/scale identically at any
    composite resolution. PURE; missing image or box ⇒ no-op.

    A resize is only as cheap as its target size, not its source size - dragging an
    overlay's POSITION (box w/h unchanged) must not re-resize a large source image
    every frame, or interactive placement gets laggy. `item["_cache"]` is a small
    mutable dict living on the caller's persistent item (NOT this snapshot dict, which
    is rebuilt every call) that memoizes the last resize by target (w_px, h_px); the
    caller resets it whenever the source image changes. This is a pure memoization
    (same key -> identical output as a fresh resize), the same pattern as the
    module-level `_ICON_CACHE` above, so composite_frame stays PURE/deterministic."""
    bgra = item.get("bgra")
    box = item.get("box")
    if bgra is None or bgra.size == 0 or not box:
        return
    kx, ky = out_w / OUT_W, out_h / OUT_H
    w_px = max(1, int(round(box[2] * kx)))
    h_px = max(1, int(round(box[3] * ky)))
    cache = item.get("_cache")
    key = (w_px, h_px)
    if cache is not None and cache.get("key") == key:
        resized = cache["img"]
    else:
        resized = cv2.resize(bgra, (w_px, h_px),
                             interpolation=cv2.INTER_AREA if w_px < bgra.shape[1]
                             else cv2.INTER_LINEAR)
        if cache is not None:
            cache["key"] = key
            cache["img"] = resized
    opacity = max(0.0, min(1.0, float(item.get("opacity", 1.0))))
    if opacity < 1.0:
        if resized.ndim == 3 and resized.shape[2] == 4:
            faded = resized.copy()
            faded[:, :, 3] = (faded[:, :, 3].astype(np.float32) * opacity).astype(np.uint8)
        else:
            faded = cv2.cvtColor(resized, cv2.COLOR_BGR2BGRA)
            faded[:, :, 3] = int(255 * opacity)
        resized = faded
    _blit_bgra(canvas, resized, int(round(box[0] * kx)), int(round(box[1] * ky)))


def _draw_branding(canvas, out_w, out_h, items):
    """Draw every branding/watermark overlay, in list order (later items on top)."""
    for item in items or ():
        _draw_branding_item(canvas, out_w, out_h, item)


def composite_frame(
    src_bgr,
    *,
    out_w=OUT_W, out_h=OUT_H,
    scale_x, scale_y,          # canvas→source factors (orig_w/disp_w, orig_h/disp_h)
    game_box, cam_box,         # [x, y, w, h] in CANVAS space
    split_ratio,               # int 30..80 - % of out_h given to the game panel
    melt=True, melt_px=50,     # cosine seam-melt on/off + zone half-width, in
                               # 1080×1920 REFERENCE px (auto-scales to the out size)
    solid_border=False,
    border_color=(15, 15, 15),  # BGR fill of the solid border bar
    border_px=12,              # half-thickness of the seam border in 1080×1920 REFERENCE
                               # px (auto-scales to the out size); default 12 = the
                               # original fixed value, so old callers are unchanged
    watermark=None,            # None | {"text", "scale", "icons", "icon_mode",
                               #         "text_bgr", "plate_bgr", "position"}
    branding=(),               # 5.5.1g: [{"bgra", "box", "opacity", "_cache"}, ...] -
                               # independent image overlays (logo/sponsor/avatar/etc.),
                               # box is [x, y, w, h] in the 1080x1920 reference space
                               # like a highlight's dest_box (5.5.1f generalized)
    highlights=(),             # PiP boxes: {"src_box": CANVAS space, "dest_box": OUTPUT
                               # 1080×1920 reference space (scaled to the actual out size)}
    single_panel=False,        # 2.10.1: one 9:16 game crop fills the WHOLE frame - no cam
                               # panel, no seam to melt; the line/plate still slide via
                               # split_ratio (now a free 0..100% position, not a division)
):
    """Compose the 1080×1920 vertical frame from a BGR source frame. PURE: everything
    comes in as arguments, nothing is read from widgets - callable from any thread.
    Returns an (out_h, out_w, 3) uint8 BGR array."""
    canvas = np.zeros((out_h, out_w, 3), dtype=np.uint8)

    g_x1 = int(game_box[0] * scale_x)
    g_y1 = int(game_box[1] * scale_y)
    g_x2 = g_x1 + int(game_box[2] * scale_x)
    g_y2 = g_y1 + int(game_box[3] * scale_y)
    c_x1 = int(cam_box[0] * scale_x)
    c_y1 = int(cam_box[1] * scale_y)
    c_x2 = c_x1 + int(cam_box[2] * scale_x)
    c_y2 = c_y1 + int(cam_box[3] * scale_y)

    game_target_h = int(out_h * split_ratio / 100)
    cam_target_h = out_h - game_target_h
    boundary_y = game_target_h   # split mode: panel division == line == plate anchor.
    # Single-panel: the game crop fills the whole frame; boundary_y stays at
    # int(out_h*split/100) so the decorative line + plate still slide 0..100%.
    fill_h = out_h if single_panel else game_target_h
    # Output-scale factors: out-px params (melt zone, watermark metrics, dest boxes)
    # are defined in the 1080×1920 reference space and shrink with smaller outputs.
    kx, ky = out_w / OUT_W, out_h / OUT_H

    game_scaled = None
    game_crop = src_bgr[g_y1:g_y2, g_x1:g_x2]
    if game_crop.size > 0 and fill_h > 0:
        game_scaled = _resize_smart(game_crop, out_w, fill_h)
        canvas[0:fill_h] = game_scaled

    cam_scaled = None
    if not single_panel:
        cam_crop = src_bgr[c_y1:c_y2, c_x1:c_x2]
        if cam_crop.size > 0 and cam_target_h > 0:
            cam_scaled = _resize_smart(cam_crop, out_w, cam_target_h)
            canvas[game_target_h:out_h] = cam_scaled

    # ── Seam melt: the VERBATIM cosine cross-blend from guidance 5 (verified; the strip
    # tiling avoids the negative-index bug when melt_px > a panel height). Don't improve it.
    if melt and melt_px > 0 and game_scaled is not None and cam_scaled is not None:
        half = min(max(1, int(round(melt_px * ky))), boundary_y, out_h - boundary_y)
        if half > 1:
            y0 = boundary_y - half
            y1 = boundary_y + half
            zone_h = y1 - y0   # == 2 * half

            top_strip = game_scaled[game_target_h - half:game_target_h].astype(np.float32)
            bot_strip = cam_scaled[0:half].astype(np.float32)

            top_full = np.concatenate([top_strip, top_strip], axis=0)
            bot_full = np.concatenate([bot_strip, bot_strip], axis=0)

            alpha = (0.5 - 0.5 * np.cos(
                np.linspace(0, np.pi, zone_h, dtype=np.float32)
            )).reshape(zone_h, 1, 1)

            canvas[y0:y1] = (
                top_full * (1.0 - alpha) + bot_full * alpha
            ).clip(0, 255).astype(np.uint8)

    # ── Highlight PiPs (2.5): crop the source, resize, paste. Drawn UNDER the border
    # and watermark (2.5b, user rule: the seam furniture stays on top). dest_box is in
    # the 1080×1920 reference space and maps proportionally to the actual out size
    # (playback parity for free). No burned border - the orange outline is a
    # preview-only guide.
    for hl in highlights or ():
        sb, db = hl.get("src_box"), hl.get("dest_box")
        if not sb or not db:
            continue
        sx1 = max(0, int(sb[0] * scale_x))
        sy1 = max(0, int(sb[1] * scale_y))
        sx2 = sx1 + int(sb[2] * scale_x)
        sy2 = sy1 + int(sb[3] * scale_y)
        crop = src_bgr[sy1:sy2, sx1:sx2]
        if crop.size == 0:
            continue
        dw = max(1, int(round(db[2] * kx)))
        dh = max(1, int(round(db[3] * ky)))
        _blit(canvas, _resize_smart(crop, dw, dh),
              int(round(db[0] * kx)), int(round(db[1] * ky)))

    if solid_border:
        # border_px at the full 1080×1920 (default 12); proportional at preview-res
        # playback (2.4b - a fixed px was 4× relatively thicker on a ~485px-tall preview
        # composite). 2.9.5: the half-thickness is now user-controllable.
        t = max(1, round(border_px * out_h / OUT_H))
        cv2.rectangle(canvas, (0, boundary_y - t), (out_w, boundary_y + t),
                      border_color, -1)

    if watermark:
        _burn_watermark(canvas, out_w, boundary_y, watermark, ky)

    if branding:
        _draw_branding(canvas, out_w, out_h, branding)

    return canvas
