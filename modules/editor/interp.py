"""Pure keyframe interpolation + highlight visibility filtering (extracted in 2.9.2).

No self, no widgets, no imports - safe from any thread with a snapshotted keyframe list
(the 2.6 render worker relies on this). Verbatim move from clip_editor; math unchanged.
"""


def interpolate_layout(keyframes, frame):
    """PURE keyframe interpolation: layout dict at `frame` from a sorted keyframe
    list, or None when the list is empty (caller keeps its static layout). No self,
    no widgets - safe from any thread with a snapshotted list (2.6 render worker).
    Math lifted from the old app's interpolate_layout_at_time, converted to integer
    frames: fluid = linear lerp of the four box components + split; jump = hold the
    previous keyframe, then cut."""
    kfs = keyframes
    if not kfs:
        return None
    if frame <= kfs[0]["frame"]:
        src = kfs[0]
    elif frame >= kfs[-1]["frame"]:
        src = kfs[-1]
    else:
        k1 = k2 = kfs[0]
        for i in range(len(kfs) - 1):
            k1, k2 = kfs[i], kfs[i + 1]
            if k1["frame"] <= frame <= k2["frame"]:
                break
        if k2["type"] == "jump":
            src = k2 if frame == k2["frame"] else k1   # hold k1, cut on k2's frame
        else:
            alpha = (frame - k1["frame"]) / (k2["frame"] - k1["frame"])
            lerp = lambda a, b: a + alpha * (b - a)  # noqa: E731 - tiny, local
            # Highlights interpolate by INDEX; if the keyframes disagree on the
            # count (one added later), common indices lerp and the rest hold the
            # value from whichever keyframe has them - never crash (spec rule).
            h1 = k1.get("highlights") or []
            h2 = k2.get("highlights") or []
            hls = []
            for i in range(max(len(h1), len(h2))):
                if i < len(h1) and i < len(h2):
                    hls.append({"src_box": [lerp(a, b) for a, b in
                                            zip(h1[i]["src_box"], h2[i]["src_box"])],
                                "dest_box": [lerp(a, b) for a, b in
                                             zip(h1[i]["dest_box"],
                                                 h2[i]["dest_box"])]})
                else:
                    held = h1[i] if i < len(h1) else h2[i]
                    hls.append({"src_box": list(held["src_box"]),
                                "dest_box": list(held["dest_box"])})
            return {
                "game_box": [lerp(a, b) for a, b in
                             zip(k1["game_box"], k2["game_box"])],
                "cam_box": [lerp(a, b) for a, b in
                            zip(k1["cam_box"], k2["cam_box"])],
                "split_ratio": int(round(lerp(k1["split_ratio"],
                                              k2["split_ratio"]))),
                "highlights": hls,
            }
    return {"game_box": list(src["game_box"]), "cam_box": list(src["cam_box"]),
            "split_ratio": int(src["split_ratio"]),
            "highlights": [{"src_box": list(h["src_box"]),
                            "dest_box": list(h["dest_box"])}
                           for h in (src.get("highlights") or [])]}


def filter_visible(hls_geom, hls_model, frame):
    """Geometry dicts (possibly interpolated) → tuple of those visible at `frame`
    (2.8.7 pop-in/out). Visibility lives on the MODEL list by index - interpolated
    geometry carries no model keys. Geometry entries beyond the model (mismatched
    keyframe counts, the 2.5 hazard) are treated as always-visible - never raise.
    PURE and the single source of truth for all composite call sites; runs per
    frame in the render hot loop but is a tuple build over ≤3 entries."""
    out = []
    for i, g in enumerate(hls_geom):
        rng = hls_model[i].get("visible") if i < len(hls_model) else None
        if rng is None or rng[0] <= frame <= rng[1]:
            out.append(g)
    return tuple(out)
