"""Process-priority nicety for the render (extracted in 2.9.2). Verbatim move."""


def _set_process_priority(above_normal):
    """Windows process priority for the render (2.8.4, Performance mode only):
    ABOVE_NORMAL while feeding, NORMAL restored on every exit. No-op off-Windows;
    never raises - priority is a nicety, not worth failing a render over. Module-
    local on purpose: promote to shared/ only when a second consumer exists."""
    try:
        import ctypes
        # The GetCurrentProcess() pseudo-handle is (HANDLE)-1; passing its return
        # through default-ctypes int args truncates it to 32 bits → invalid handle →
        # SetPriorityClass silently fails. c_void_p(-1) IS the pseudo-handle, intact.
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.c_void_p(-1),
            0x00008000 if above_normal else 0x00000020)  # ABOVE_NORMAL / NORMAL
    except Exception:  # noqa: BLE001 - includes non-Windows (no windll)
        pass
