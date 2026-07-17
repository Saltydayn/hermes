"""HERMES build driver (roadmap 7.2 / SPEC_dist.2).

Turns main.py into a PyInstaller onedir bundle (dist/HERMES/HERMES.exe + _internal/), and
optionally compiles the installer. Single source of truth for version/branding is
shared/version.py. PyInstaller is a BUILD-TIME dependency (requirements-build.txt), never
imported by app code, never in requirements.txt.

Usage (from anywhere; the driver cd's to the repo root):
    python build/build.py                 # build the onedir bundle (release: no console)
    python build/build.py --console       # build with a console window (first-boot debug)
    python build/build.py --installer     # build, then compile the Inno Setup installer
    python build/build.py --installer --release --publish-blobs ../hermes-public-push
        # full release: build, compile the installer, then push this release's
        # blobs + manifest to the release-blobs branch of that git working copy
        # (9.3f) - the GitHub Release itself only ever gets the installer exe
        # attached; self-update clients fetch blobs from that branch instead.

Outputs:
    build/dist/HERMES/HERMES.exe               # the app
    build/Output/HERMES-<VERSION>-setup.exe    # installer (with --installer)

Run on Windows: PyInstaller cannot cross-build a Windows exe. ffmpeg is NOT bundled (9.3b) -
it downloads on demand at runtime the first time a module that needs it is enabled; a build
succeeds without assets/ffmpeg.exe present.
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))     # build/
REPO = os.path.dirname(HERE)                            # repo root
sys.path.insert(0, REPO)
from shared import version  # noqa: E402

SPEC = os.path.join(HERE, "hermes.spec")
ISS = os.path.join(HERE, "hermes.iss")
DIST = os.path.join(HERE, "dist")
WORK = os.path.join(HERE, "work")
VERSION_INFO = os.path.join(HERE, "version_info.txt")

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


# Files a self-update (9.3c) must never touch: manifest.json is the manifest
# itself, portable.txt is the marker that decides portable vs installed data
# paths - the updater flipping that would silently change where the app reads
# its config and data.
_MANIFEST_EXCLUDE = {"manifest.json", "portable.txt"}

# Directories (POSIX relpaths from tree_root) excluded from the manifest entirely:
# the static Tcl/Tk data family every Tkinter app on Windows bundles - encoding
# tables, locale message catalogs, theme scripts. Measured (REPORT_9.3e.md,
# real --release build): 992 of 1106 files (89.7%) for 5.7 MB, none of it ever
# touched by HERMES's own code or changed by a HERMES release. diff_manifests()
# only ever flags a relpath as "download" or "remove" by comparing what's present
# in the local vs remote manifest - a file consistently absent from BOTH is simply
# never mentioned either way, so excluding this family here means the self-updater
# never downloads or deletes it; the installer still ships every one of these files
# in full, this only removes them from update-delta TRACKING.
_MANIFEST_EXCLUDE_DIRS = (
    "_internal/_tcl_data",
    "_internal/_tk_data",
    "_internal/tcl8",
    "_internal/tkinterdnd2",
)


def _sha256_of(path, chunk=1048576):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def build_manifest(tree_root, version_str):
    """Hash every file under tree_root (POSIX relpaths, sha256 + size), excluding
    manifest.json/portable.txt and the static Tcl/Tk data family
    (_MANIFEST_EXCLUDE_DIRS, 9.3e). Returns the manifest dict; does not write it."""
    files = {}
    for dirpath, dirnames, filenames in os.walk(tree_root):
        rel_dir = os.path.relpath(dirpath, tree_root).replace(os.sep, "/")
        if rel_dir in _MANIFEST_EXCLUDE_DIRS:
            dirnames[:] = []   # do not descend into an excluded family
            continue
        for name in filenames:
            if name in _MANIFEST_EXCLUDE:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, tree_root).replace(os.sep, "/")
            files[rel] = {"sha256": _sha256_of(full), "size": os.path.getsize(full)}
    return {"version": version_str, "files": files}


def write_manifest(tree_root, version_str):
    """Compute and write manifest.json into tree_root - part of every onedir build
    (9.3c), so the shipped tree always carries its own installed record. Returns
    the manifest dict."""
    manifest = build_manifest(tree_root, version_str)
    out = os.path.join(tree_root, "manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"[build] manifest -> {os.path.relpath(out, REPO)} "
          f"({len(manifest['files'])} files)")
    return manifest


def build_release_blobs(tree_root, manifest):
    """Emit build/release/: one sha256-named blob per unique file in the manifest
    (content-addressed, natural dedup) plus a copy of manifest.json - the local
    staging area publish_release_blobs() pushes from. (Earlier design uploaded
    these as GitHub Release assets directly; as of 9.3f they instead go to the
    release-blobs branch so the Releases page carries only the installer.)"""
    release_dir = os.path.join(HERE, "release")
    if os.path.isdir(release_dir):
        shutil.rmtree(release_dir)
    os.makedirs(release_dir, exist_ok=True)
    seen = set()
    total_size = 0
    for rel, info in manifest["files"].items():
        sha = info["sha256"]
        if sha in seen:
            continue
        seen.add(sha)
        src = os.path.join(tree_root, rel.replace("/", os.sep))
        shutil.copy2(src, os.path.join(release_dir, sha))
        total_size += info["size"]
    shutil.copy2(os.path.join(tree_root, "manifest.json"),
                os.path.join(release_dir, "manifest.json"))
    print(f"[build] release blobs -> {os.path.relpath(release_dir, REPO)} "
          f"({len(seen)} unique files, {total_size / 1_000_000:.1f} MB total)")


# Branch (of the SAME public source repo, e.g. the hermes-public-push sibling clone)
# that holds the flat, content-addressed blob store - deliberately not a second repo
# and not GitHub Release assets, so the main branch (source browsing) and the
# Releases page (installer only) both stay untouched by blob content (9.3f).
BLOBS_BRANCH = "release-blobs"


def _run_git(args, cwd):
    """Run a git command in cwd. Raises RuntimeError with stderr on failure -
    publish_release_blobs() lets this propagate; a partial/broken publish must
    stop loudly, never silently continue on the wrong branch."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}:\n{result.stderr}")
    return result.stdout


def publish_release_blobs(release_dir, version_str, repo_dir, branch=BLOBS_BRANCH,
                          push=True):
    """Publish this release's blobs + a version-named manifest onto `branch` (an
    orphan branch, created on first use) of the git working copy at repo_dir -
    NOT the repo's default branch, so main stays exactly the curated source
    allowlist with no blob content ever appearing in it. Content-addressed: a
    blob whose bytes already exist on the branch produces no git diff, so a
    re-run (or a release that changed nothing new) is a safe no-op, and a file
    unchanged across many releases is only ever pushed once. Always restores
    repo_dir to whatever branch it was on before returning, success or failure,
    so a shared sibling clone is never left mid-operation for a later task.
    Returns (ok, message); never raises."""
    try:
        original_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"],
                                   repo_dir).strip()
    except RuntimeError as exc:
        return False, str(exc)
    try:
        _run_git(["fetch", "origin"], repo_dir)
        local_branches = _run_git(["branch", "--list", branch], repo_dir)
        remote_branches = _run_git(["branch", "-r", "--list", f"origin/{branch}"],
                                   repo_dir)
        if remote_branches.strip():
            _run_git(["checkout", branch], repo_dir)
            _run_git(["reset", "--hard", f"origin/{branch}"], repo_dir)
        elif local_branches.strip():
            _run_git(["checkout", branch], repo_dir)
        else:
            _run_git(["checkout", "--orphan", branch], repo_dir)
            _run_git(["rm", "-rf", "."], repo_dir)

        blobs_dir = os.path.join(repo_dir, "blobs")
        os.makedirs(blobs_dir, exist_ok=True)
        published = 0
        for name in os.listdir(release_dir):
            if name == "manifest.json":
                continue
            shutil.copy2(os.path.join(release_dir, name), os.path.join(blobs_dir, name))
            published += 1
        shutil.copy2(os.path.join(release_dir, "manifest.json"),
                    os.path.join(blobs_dir, f"manifest-{version_str}.json"))

        _run_git(["add", "-A"], repo_dir)
        status = _run_git(["status", "--porcelain"], repo_dir)
        if not status.strip():
            return True, "nothing new to publish - all blobs already on the branch"
        _run_git(["commit", "-m", f"Publish release blobs for v{version_str}"], repo_dir)
        if push:
            _run_git(["push", "origin", branch], repo_dir)
        return True, (f"published {published} blob(s) + manifest-{version_str}.json "
                     f"to {branch}" + (" and pushed" if push else " (not pushed)"))
    except (RuntimeError, OSError) as exc:
        return False, str(exc)
    finally:
        try:
            _run_git(["checkout", original_branch], repo_dir)
        except RuntimeError:
            pass


def build_onedir(console=False):
    """Run PyInstaller against the spec, producing build/dist/HERMES/. ffmpeg is not
    required at build time (9.3b) - it downloads on demand at runtime. Always ends
    by writing a manifest.json into the tree (9.3c) - cheap, and the shipped build
    needs one whether or not this release is ever pushed through the self-updater.
    Returns the manifest dict."""
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
    return write_manifest(os.path.join(DIST, "HERMES"), version.VERSION)


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
    ap.add_argument("--installer", action="store_true",
                    help="after building, compile the Inno Setup installer (needs Inno Setup 6)")
    ap.add_argument("--iscc", default=None,
                    help="path to ISCC.exe (else the default install path / HERMES_ISCC / PATH)")
    ap.add_argument("--console", action="store_true",
                    help="build with a console window (first-boot traceback debugging)")
    ap.add_argument("--release", action="store_true",
                    help="after building, emit build/release/ (sha256-named blobs + "
                         "manifest.json) - the self-updater's (9.3c) staging area")
    ap.add_argument("--publish-blobs", metavar="REPO_DIR", default=None,
                    help="push build/release/'s blobs + a version-named manifest to "
                         f"the '{BLOBS_BRANCH}' branch of the git working copy at "
                         "REPO_DIR (e.g. ../hermes-public-push) - implies --release. "
                         "The Releases page and main branch are never touched by this.")
    args = ap.parse_args()

    os.chdir(REPO)
    manifest = build_onedir(console=args.console)
    if args.installer:
        build_installer(iscc_override=args.iscc)
    if args.release or args.publish_blobs:
        build_release_blobs(os.path.join(DIST, "HERMES"), manifest)
    if args.publish_blobs:
        release_dir = os.path.join(HERE, "release")
        ok, message = publish_release_blobs(release_dir, version.VERSION,
                                            args.publish_blobs)
        print(f"[build] publish-blobs -> {message}")
        if not ok:
            sys.exit("[build] ERROR: publishing release blobs failed.")
    print("[build] done.")


if __name__ == "__main__":
    main()
