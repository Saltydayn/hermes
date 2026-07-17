"""Shared HTTP download primitive - Phase 9.3a.

Headless, stdlib-only. Streams a URL to a local path with progress reporting,
optional sha256/size verification, and cooperative cancellation. Never raises;
every failure mode comes back as a DownloadResult with ok=False and a short
error string.

Resume/Range requests are intentionally not implemented. A failed or canceled
download is simply re-run from scratch; for the file sizes this app deals with
(an update delta of tens of MB, an ffmpeg zip under 100MB) that is far simpler
than correct range plus re-verify and the cost is acceptable.

This module never resolves or creates paths - callers pass in a full dest
path and own directory creation. Keeps it a leaf module with zero dependency
on shared.paths or shared.config.
"""

import hashlib
import os
import urllib.error
import urllib.request


class DownloadResult:
    def __init__(self, ok=False, canceled=False, error="", path="", sha256="", bytes=0):
        self.ok = ok
        self.canceled = canceled
        self.error = error
        self.path = path
        self.sha256 = sha256
        self.bytes = bytes


def _parse_content_length(headers):
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def download_file(url, dest, *, expected_sha256=None, expected_size=None,
                   on_progress=None, cancel_event=None, timeout=15, chunk=65536):
    """Download url to dest, streaming and hashing as it goes.

    Writes to dest + ".part" and atomically replaces dest only after every
    requested check passes. On failure or cancel the .part file is removed
    and dest is left untouched. total passed to on_progress is the HTTP
    Content-Length when the server sends it, else expected_size, else None.

    on_progress runs on the calling thread and must be cheap - it must not
    touch tkinter directly. Callers that need UI updates push to a queue and
    drain it from the UI thread.
    """
    part = dest + ".part"
    resp = None
    fh = None
    succeeded = False
    try:
        try:
            resp = urllib.request.urlopen(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            return DownloadResult(ok=False, error=f"connection failed: {exc}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return DownloadResult(ok=False, error=f"connection failed: {exc}")

        total = _parse_content_length(resp.headers)
        if total is None:
            total = expected_size

        try:
            fh = open(part, "wb")
        except OSError as exc:
            return DownloadResult(ok=False, error=f"cannot write to {part}: {exc}")

        hasher = hashlib.sha256()
        downloaded = 0

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return DownloadResult(ok=False, canceled=True)

            try:
                data = resp.read(chunk)
            except (urllib.error.URLError, OSError) as exc:
                return DownloadResult(ok=False, error=f"read failed: {exc}")

            if not data:
                break

            hasher.update(data)
            fh.write(data)
            downloaded += len(data)

            if on_progress is not None:
                on_progress(downloaded, total)

        if expected_size is not None and downloaded != expected_size:
            return DownloadResult(
                ok=False,
                error=f"size mismatch: got {downloaded}, expected {expected_size}")

        digest = hasher.hexdigest()
        if expected_sha256 is not None and digest != expected_sha256.lower():
            return DownloadResult(ok=False, error="checksum mismatch")

        fh.close()
        fh = None
        try:
            os.replace(part, dest)
        except OSError as exc:
            return DownloadResult(ok=False, error=f"cannot finalize {dest}: {exc}")

        succeeded = True
        return DownloadResult(ok=True, path=dest, sha256=digest, bytes=downloaded)

    except Exception as exc:  # noqa: BLE001 - this primitive must never raise
        return DownloadResult(ok=False, error=f"unexpected error: {exc}")

    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        if resp is not None:
            try:
                resp.close()
            except OSError:
                pass
        if not succeeded:
            _remove_quiet(part)


def sha256_file(path, chunk=1048576):
    """Streamed lowercase-hex sha256 of an existing file. Raises OSError if unreadable."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            hasher.update(data)
    return hasher.hexdigest()
