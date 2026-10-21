# Third-party components

HERMES is built on top of open-source software. This file lists what's bundled or depended on and
under what terms, separate from HERMES's own license (see [LICENSE](LICENSE)).

## ffmpeg (downloaded on first use)

ffmpeg is used as a completely separate, unmodified executable, invoked as a subprocess - HERMES
never links against its code. This is the standard, well-established pattern other apps that ship
ffmpeg use (Audacity, OBS, HandBrake, and many others do the same).

HERMES doesn't ship ffmpeg inside the installer. The first time it's needed, HERMES offers to
download a pinned build straight from gyan.dev, or the same file from gyan.dev's GitHub
archive if that link is gone (**ffmpeg 8.1.2, "essentials" build**). gyan.dev's
builds are static 64-bit builds licensed under the **GNU General Public License (GPL) version 3**.
If you run from source, you can also place your own `ffmpeg.exe` on your PATH or at
`assets/ffmpeg.exe` instead.

ffmpeg's own license terms apply to ffmpeg itself, in full, regardless of HERMES's own license.
Full text: [gnu.org/licenses/gpl-3.0.html](https://www.gnu.org/licenses/gpl-3.0.html). Project
homepage and source: [ffmpeg.org](https://ffmpeg.org). gyan.dev's build source and scripts:
[github.com/GyanD/codexffmpeg](https://github.com/GyanD/codexffmpeg).

## Python packages (see `requirements.txt`)

The installer bundles these libraries, unmodified, as ordinary dependencies. Each keeps its own
license; the license text ships with each project (see its repository).

- **opencv-python** (Apache-2.0), **numpy** (BSD-3-Clause), **Pillow** (MIT-CMU / HPND),
  **yt-dlp** (Unlicense), **tkinterdnd2** (MIT, bundles tkdnd under a BSD-style license),
  **sounddevice** (MIT, bundles PortAudio under the MIT license), **cffi** (MIT).
- **faster-whisper** (MIT) and what it brings along: **CTranslate2** (MIT), **onnxruntime**
  (MIT), **tokenizers**, **huggingface_hub** and **hf-xet** (Apache-2.0), **tqdm** (MPL-2.0 / MIT), **PyYAML** (MIT), **requests** (Apache-2.0),
  **urllib3** (MIT), **idna** (BSD-3-Clause), **certifi** (MPL-2.0), **charset-normalizer**
  (MIT), **httpx**, **httpcore** and **colorama** (BSD-3-Clause), **h11** and **anyio** (MIT),
  **fsspec** (BSD-3-Clause), **filelock** (Unlicense), **packaging** (Apache-2.0 / BSD),
  **typing_extensions** (PSF), **click** (BSD-3-Clause), **psutil** (BSD-3-Clause),
  **pywin32** (PSF), **pycparser** (BSD-3-Clause).
- Python itself (PSF License) and the Tcl/Tk runtime (BSD-style) come with the app via
  PyInstaller (GPL with a bootloader exception that allows this).

## Bundled fonts (`assets/fonts/`)

Four fonts ship with HERMES for Subtitle Studio's karaoke burn-in, each under the **SIL Open Font
License, Version 1.1** (full text bundled alongside each font as `<Family>-OFL.txt`; also available
at [scripts.sil.org/OFL](http://scripts.sil.org/OFL)). The OFL explicitly permits bundling,
redistributing, and selling fonts with software, unmodified or modified, as long as they are not
sold by themselves.

- **Anton** - Copyright 2020 The Anton Project Authors
  ([AntonFont](https://github.com/googlefonts/AntonFont.git)).
- **Bebas Neue** - Copyright 2010 by Dharma Type.
- **Montserrat** (ExtraBold weight) - Copyright 2024 The Montserrat.Git Project Authors
  ([Montserrat](https://github.com/JulietaUla/Montserrat.git)). Shipped as a static instance of the
  upstream variable font at weight 800, produced with fontTools (a build-time tool only, not a
  runtime dependency).
- **Poppins** (SemiBold weight) - Copyright 2020 The Poppins Project Authors
  ([Poppins](https://github.com/itfoundry/Poppins)).

## Speech models (downloaded on demand, Downloads tab)

Subtitle Studio's speech models are OpenAI's Whisper models (released under the **MIT License**),
converted for faster-whisper. HERMES downloads them only when you pick one in the Downloads tab,
from these Hugging Face repositories, each pinned to a fixed version:

- [Systran/faster-whisper-large-v3](https://huggingface.co/Systran/faster-whisper-large-v3)
- [deepdml/faster-whisper-large-v3-turbo-ct2](https://huggingface.co/deepdml/faster-whisper-large-v3-turbo-ct2)
- [Systran/faster-whisper-medium](https://huggingface.co/Systran/faster-whisper-medium)
- [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small)

## NVIDIA GPU add-on (optional, downloaded on demand)

The optional GPU mode needs NVIDIA's CUDA runtime libraries (cuBLAS, cuDNN, NVRTC). HERMES doesn't
ship them. If you turn GPU mode on in the Downloads tab, HERMES fetches NVIDIA's own official
packages from PyPI (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, `nvidia-cuda-nvrtc-cu12`) and
unpacks the libraries locally. They remain under NVIDIA's license terms, which come with those
packages.
