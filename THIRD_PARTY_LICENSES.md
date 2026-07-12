# Third-party components

HERMES is built on top of open-source software. This file lists what's bundled or depended on and
under what terms, separate from HERMES's own license (see [LICENSE](LICENSE)).

## ffmpeg (bundled binary, `assets/ffmpeg.exe`)

ffmpeg is used as a completely separate, unmodified executable, invoked as a subprocess - HERMES
never links against its code. This is the standard, well-established pattern other apps that bundle
ffmpeg use (Audacity, OBS, HandBrake, and many others do the same).

The bundled binary is confirmed as **ffmpeg 8.0.1, "full" build from gyan.dev** (version string:
`8.0.1-full_build-www.gyan.dev`). All gyan.dev builds are static 64-bit builds licensed under the
**GNU General Public License (GPL) version 3**, so that's what applies here (the "full" variant
specifically includes GPL-only codecs/filters not present in gyan.dev's more permissive
"essentials" build).

ffmpeg's own license terms apply to ffmpeg itself, in full, regardless of HERMES's own license.
Full text: [gnu.org/licenses/gpl-3.0.html](https://www.gnu.org/licenses/gpl-3.0.html). Project
homepage and source: [ffmpeg.org](https://ffmpeg.org). This exact build's source and build scripts:
[github.com/GyanD/codexffmpeg](https://github.com/GyanD/codexffmpeg).

## Python packages (see `requirements.txt`)

opencv-python, numpy, Pillow, yt-dlp, tkinterdnd2, and sounddevice are each distributed under their
own permissive open-source licenses (BSD, MIT, Apache-2.0, or similar - see each project's own
repository for its exact license text). They are used as ordinary library dependencies, unmodified.
