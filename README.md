# H.E.R.M.E.S.

**Highlight Editor & Rapid Media Export Suite**

A free Windows desktop app for turning raw stream clips into finished 9:19 vertical videos ready for YouTube Shorts, Instagram Reels, Tiktok, you name it. Load a
clip, crop and split it into a 9:16 layout, add highlight boxes and a watermark, cut out the
boring parts, adjust the audio, render, and export. Built by a streamer, for streamers - the
whole point is making that after-stream editing loop fast instead of painful.

> **Beta.** Things may still change and bugs are expected. If something breaks or feels off,
> use the feedback/bug-report buttons in the app (About tab), or find us on Discord below -
> that's exactly what the beta is for.

**[Discord](https://discord.gg/XhraUHSjSf)** · **[Twitch](https://www.twitch.tv/saltydayn)** ·
**[X / Twitter](https://x.com/saltydayn)** · **[Ko-fi](https://ko-fi.com/saltydayn)** ·
**[Wiki (full guide + screenshots)](../../wiki)**

## What it actually does

- Import a clip from a file or a URL (Twitch, YouTube, and more).
- Crop and lay it out for vertical Shorts - split game/cam view or a single focused panel.
- Add highlight boxes (picture-in-picture), a watermark/branding, keyframed movement over time.
- Cut out dead air with a frame-accurate multi-cut timeline, adjust audio per-section.
- Render in-app, then export and get a one-click hand-off to YouTube's upload page.

Full walkthrough with screenshots is on the [Wiki](../../wiki) - this README stays short on
purpose.

## Getting it

**Portable zip** (no install):

1. Unzip the `HERMES` folder anywhere - a USB stick, your Desktop, wherever.
2. Run `HERMES.exe`.

Your config, clips, and exports live inside that `HERMES` folder. Delete the folder and it's
gone, no trace left on your system.

**Installer** (`HERMES-...-setup.exe`):

1. Run the setup. It installs per-user (no admin prompt needed), adds a Start Menu entry, and
   offers an optional desktop shortcut + launch-on-startup.
2. Your clips, exports, and settings live in `%LOCALAPPDATA%\HERMES`, separate from the program
   files. Uninstalling asks whether to keep or wipe them (keeps by default).

Both are on the [Releases page](../../releases). Windows will probably show a **"Windows
protected your PC"** SmartScreen warning on first run, since the app isn't code-signed yet -
click **More info -> Run anyway**. That's normal for small unsigned apps and will get sorted out
down the line.

## Requirements

- Windows 10 or newer, 64-bit.
- That's it. ffmpeg is bundled, no Python install needed.

## Running from source

```
pip install -r requirements.txt
python main.py
```

You'll also need `ffmpeg.exe` on your PATH or placed at `assets/ffmpeg.exe` (not included in
this repo - grab a build from ffmpeg.org).

## License

Source-available, not open-source - you're welcome to read the code and run the official
builds, but redistributing, reselling, or shipping a modified/rebranded version isn't allowed
without asking first. See [LICENSE](LICENSE) for the actual terms, and
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for the open-source pieces HERMES is built on
(ffmpeg and a handful of Python packages).

## Support

Everything above is free, no ads, nothing paywalled. If you want to support development, the
[Ko-fi](https://ko-fi.com/saltydayn) has supporter tiers with perks like a vote in what gets
built next - never anything that locks features behind a paywall. Bug reports, feature ideas,
and just showing up on Discord all help just as much.
