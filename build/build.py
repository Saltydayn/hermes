"""HERMES build driver (roadmap 7.2 / SPEC_dist.2).

Turns main.py into a PyInstaller onedir bundle (dist/HERMES/HERMES.exe + _internal/), and
optionally assembles the portable zip. Single source of truth for version/branding is
shared/version.py. PyInstaller is a BUILD-TIME dependency (requirements-build.txt), never
imported by app code, never in requirements.txt.

Usage (from anywhere; the driver cd's to the repo root):
    python build/build.py                 # build the onedir bundle (release: no console)
    python build/build.py --console       # build with a console window (first-boot debug)
    python build/build.py --portable      # build, then zip the portable build (+ marker)

Outputs:
    build/dist/HERMES/HERMES.exe          # the app
    build/HERMES-<VERSION>-portable.zip   # portable build (with --portable)

Run on Windows: PyInstaller cannot cross-build a Windows exe, and ffmpeg.exe is required at
assets/ffmpeg.exe (the build refuses without it).
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))     # build/
REPO = os.path.dirname(HERE)                            # repo root
sys.path.insert(0, REPO)
from shared import version  # noqa: E402

SPEC = os.path.join(HERE, "hermes.spec")
ISS = os.path.join(HERE, "hermes.iss")
DIST = os.path.join(HERE, "dist")
WORK = os.path.join(HERE, "work")
VERSION_INFO = os.path.join(HERE, "version_info.txt")
FFMPEG = os.path.join(REPO, "assets", "ffmpeg.exe")

# Typical install location of Inno Setup's command-line compiler (shown in the error hint).
# Any installed "Inno Setup N" folder is auto-detected; override via --iscc / HERMES_ISCC.
_ISCC_DEFAULT = r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"


def _version_tuple():
    """'0.1.0' -> (0, 1, 0, 0). Windows version resources want four ints."""
    parts = [int(p) for p in version.VERSION.split(".") if p.isdigit()]
    parts = (parts + [0, 0, 0, 0])[:4]
    return tuple(parts)


def _file_version():
    """Version string for artifact filenames + the installer's AppVersion, e.g.
    '0.9.0-beta'. The exe version RESOURCE stays numeric (_version_tuple)."""
    suffix = getattr(version, "VERSION_SUFFIX", "")
    return version.VERSION + (f"-{suffix.lower()}" if suffix else "")


def write_version_info():
    """Generate the PyInstaller VSVersionInfo resource from shared/version.py."""
    v = _version_tuple()
    vstr = ".".join(str(x) for x in v)
    # ASCII-only strings (the .exe resource is happiest that way; the em-dash/(c) glyphs the
    # spec shows are swapped for ASCII equivalents, purely cosmetic in Properties->Details).
    desc = f"{version.APP_NAME} - {version.EXPANSION}"
    body = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={v},
    prodvers={v},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', {version.PUBLISHER!r}),
         StringStruct('FileDescription', {desc!r}),
         StringStruct('FileVersion', {vstr!r}),
         StringStruct('InternalName', {version.APP_NAME!r}),
         StringStruct('LegalCopyright', {('Copyright ' + version.PUBLISHER)!r}),
         StringStruct('OriginalFilename', 'HERMES.exe'),
         StringStruct('ProductName', {version.APP_NAME!r}),
         StringStruct('ProductVersion', {vstr!r})])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    with open(VERSION_INFO, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[build] wrote {os.path.relpath(VERSION_INFO, REPO)} (version {vstr})")


def build_onedir(console=False):
    """Run PyInstaller against the spec, producing build/dist/HERMES/."""
    if not os.path.exists(FFMPEG):
        sys.exit(
            f"[build] ERROR: {os.path.relpath(FFMPEG, REPO)} is missing.\n"
            "        The bundle is useless without ffmpeg. Place a Windows ffmpeg.exe there\n"
            "        (e.g. from https://www.gyan.dev/ffmpeg/builds/) and rebuild.")
    write_version_info()
    os.environ["HERMES_CONSOLE"] = "1" if console else "0"

    import PyInstaller.__main__ as pyi
    args = [SPEC, "--noconfirm", "--distpath", DIST, "--workpath", WORK]
    print(f"[build] PyInstaller {args}")
    pyi.run(args)

    exe = os.path.join(DIST, "HERMES", "HERMES.exe")
    if not os.path.exists(exe):
        sys.exit("[build] ERROR: PyInstaller finished but HERMES.exe is missing.")
    print(f"[build] OK -> {os.path.relpath(exe, REPO)}"
          + ("  (console build)" if console else ""))


def build_portable():
    """Stage a copy of the onedir tree, drop portable.txt beside the exe, zip the HERMES/
    folder to build/HERMES-<VERSION>-portable.zip."""
    src = os.path.join(DIST, "HERMES")
    if not os.path.isdir(src):
        sys.exit("[build] ERROR: build the onedir bundle before --portable.")
    zip_path = os.path.join(HERE, f"HERMES-{_file_version()}-portable")
    with tempfile.TemporaryDirectory() as staging:
        dest = os.path.join(staging, "HERMES")
        shutil.copytree(src, dest)
        # The marker dist.1's portable branch looks for, beside the exe.
        open(os.path.join(dest, "portable.txt"), "w").close()
        readme = os.path.join(REPO, "README.md")
        if os.path.exists(readme):
            shutil.copy2(readme, os.path.join(dest, "README.md"))
        if os.path.exists(zip_path + ".zip"):
            os.remove(zip_path + ".zip")
        shutil.make_archive(zip_path, "zip", root_dir=staging, base_dir="HERMES")
    print(f"[build] OK -> {os.path.relpath(zip_path + '.zip', REPO)}  (portable, with marker)")


def _find_iscc(override=None):
    """Locate Inno Setup's ISCC.exe: --iscc, then HERMES_ISCC env, then ANY installed
    Inno Setup (6, 7, …) under Program Files, then PATH. None if not found. The .iss uses
    only version-agnostic syntax, so any current Inno Setup compiles it."""
    for c in (override, os.environ.get("HERMES_ISCC")):
        if c and os.path.exists(c):
            return c
    bases = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.environ.get("ProgramFiles", r"C:\Program Files")]
    for base in bases:
        # "Inno Setup 6", "Inno Setup 7", etc: newest version folder first.
        for path in sorted(glob.glob(os.path.join(base, "Inno Setup *", "ISCC.exe")),
                           reverse=True):
            if os.path.exists(path):
                return path
    return shutil.which("ISCC.exe") or shutil.which("ISCC")


def build_installer(iscc_override=None):
    """Compile build/hermes.iss with ISCC into build/Output/HERMES-<VERSION>-setup.exe.
    Requires the onedir tree to exist (build first)."""
    if not os.path.isdir(os.path.join(DIST, "HERMES")):
        sys.exit("[build] ERROR: build the onedir bundle before --installer.")
    iscc = _find_iscc(iscc_override)
    if not iscc:
        sys.exit(
            "[build] ERROR: Inno Setup (ISCC.exe) not found.\n"
            f"        Looked under Program Files for 'Inno Setup */ISCC.exe' (e.g. {_ISCC_DEFAULT}).\n"
            "        Install Inno Setup 6+ from https://jrsoftware.org/isdl.php , or pass\n"
            "        --iscc <path-to-ISCC.exe> (or set HERMES_ISCC).")
    cmd = [iscc, f"/DMyAppVersion={_file_version()}",
           f"/DMyAppPublisher={version.PUBLISHER}", ISS]
    print(f"[build] ISCC {cmd}")
    r = subprocess.run(cmd, cwd=HERE)        # cwd=build/ so the .iss's relative Source resolves
    if r.returncode != 0:
        sys.exit(f"[build] ERROR: ISCC failed (exit {r.returncode}).")
    out = os.path.join(HERE, "Output", f"HERMES-{_file_version()}-setup.exe")
    if not os.path.exists(out):
        sys.exit("[build] ERROR: ISCC finished but the setup exe is missing.")
    print(f"[build] OK -> {os.path.relpath(out, REPO)}")


def main():
    ap = argparse.ArgumentParser(description="Build HERMES (PyInstaller onedir).")
    ap.add_argument("--portable", action="store_true",
                    help="after building, assemble the portable zip (with portable.txt)")
    ap.add_argument("--installer", action="store_true",
                    help="after building, compile the Inno Setup installer (needs Inno Setup 6)")
    ap.add_argument("--iscc", default=None,
                    help="path to ISCC.exe (else the default install path / HERMES_ISCC / PATH)")
    ap.add_argument("--console", action="store_true",
                    help="build with a console window (first-boot traceback debugging)")
    args = ap.parse_args()

    os.chdir(REPO)
    build_onedir(console=args.console)
    if args.portable:
        build_portable()
    if args.installer:
        build_installer(iscc_override=args.iscc)
    print("[build] done.")


if __name__ == "__main__":
    main()
