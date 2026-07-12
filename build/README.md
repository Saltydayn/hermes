# Building HERMES

Packaging for distribution (roadmap 7.2). Branding + version come from `shared/version.py`
(the single source of truth); bump `VERSION` there per release.

## Prerequisites (Windows)

PyInstaller can't cross-build a Windows exe, so **build on Windows**.

1. **Python 3.x** with the app's runtime deps installed (`pip install -r requirements.txt`).
2. **Build deps:** `pip install -r requirements-build.txt` (PyInstaller).
3. **ffmpeg:** place a Windows `ffmpeg.exe` at **`assets/ffmpeg.exe`**. The build bundles it;
   it refuses to build without it. (Get one from https://www.gyan.dev/ffmpeg/builds/, the
   "essentials" build is smaller; any full static `ffmpeg.exe` works.)
4. *(optional)* `assets/hermes.ico` for the exe icon; the build tolerates its absence (warns
   and ships without an icon).

## Build the app (onedir)

```
python build/build.py
```

Output: **`build/dist/HERMES/HERMES.exe`** plus **`build/dist/HERMES/_internal/`** (the
PyInstaller onedir tree; `assets/` icons and bundled ffmpeg land in `_internal/assets/`).

### First-boot debugging

The release build is windowed (no console). To surface tracebacks on a failed boot, build a
console variant:

```
python build/build.py --console
```

Run that `HERMES.exe` from a terminal to see stdout/stderr, fix the issue, then rebuild
without `--console` for the release.

## Build the portable zip

```
python build/build.py --portable
```

This builds the onedir tree, then assembles **`build/HERMES-<VERSION>-portable.zip`** whose
root is the `HERMES/` folder **with a `portable.txt` marker beside `HERMES.exe`**. That marker
flips the app into portable mode: it keeps `config.json` + `data/` **beside the exe** instead
of in `%LOCALAPPDATA%\HERMES`. Unzip anywhere and run, zero install.

## Build the installer

Produces a per-user Windows installer (no admin) that wraps the onedir tree.

**Prerequisite:** install **Inno Setup 6 (or newer)** (https://jrsoftware.org/isdl.php). The
driver auto-detects any installed `Inno Setup */ISCC.exe` under Program Files (newest first);
override with `--iscc <path>` or the `HERMES_ISCC` env var. The `.iss` uses only
version-agnostic syntax, so any current Inno Setup compiles it.

```
python build/build.py --installer
```

This builds a fresh onedir bundle first, then compiles `build/hermes.iss` into
**`build/Output/HERMES-<VERSION>-setup.exe`**. The installer:

- installs per-user to `%LOCALAPPDATA%\Programs\HERMES\` (no UAC),
- always creates a Start Menu **HERMES** entry,
- offers two optional tasks: **Create a desktop shortcut** (default on) and **Launch HERMES
  when Windows starts** (default off),
- does **not** ship `portable.txt`, so the installed app uses `%LOCALAPPDATA%\HERMES` for data.

**Fixed AppId GUID:** `{0141F00E-DB9E-4583-9850-D92A866524FC}`. Never change it; it is the
installer's upgrade/uninstall identity, and changing it would orphan installed copies.

### Build both in one shot

`build/build_release.bat` runs `python build/build.py --portable --installer`, producing the
portable zip and the installer together.

### Uninstall behavior

A normal uninstall removes the program files, shortcuts, and the startup Run key, but **keeps
your clips/Shorts/config** in `%LOCALAPPDATA%\HERMES`. The uninstaller asks once whether to
also delete that data; the default is **No (keep)**. Answer **Yes** only to wipe everything.

## Where user data lives (frozen)

- **Installed** (no `portable.txt`): `%LOCALAPPDATA%\HERMES\` holds `config.json`, `data/`,
  `models/`, `shorts_export/`. Nothing is written inside the install dir.
- **Portable** (`portable.txt` present): the same data sits beside `HERMES.exe`.

(Run-from-source is unchanged: data stays beside `main.py`.)

## SmartScreen / antivirus (unsigned v1)

The exe is **unsigned**. On first run Windows SmartScreen may show *"Windows protected your
PC"*, click **More info -> Run anyway**. A fresh unsigned PyInstaller exe can also draw a
false-positive AV quarantine. Code signing (an Authenticode cert) is the real fix and is a
future step (out of scope for v1).

## Outputs (all git-ignored)

| Path | What |
|---|---|
| `build/dist/HERMES/` | the onedir app tree (exe + `_internal/`) |
| `build/work/` | PyInstaller scratch (workpath) |
| `build/version_info.txt` | generated exe version resource |
| `build/HERMES-<VERSION>-portable.zip` | portable build |
| `build/Output/HERMES-<VERSION>-setup.exe` | installer (`--installer`) |
