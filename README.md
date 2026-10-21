# H.E.R.M.E.S.

**Highlight Editor & Rapid Media Export Suite**

A free Windows desktop app for turning raw stream clips into finished vertical 9:16 videos ready
for YouTube Shorts, Instagram Reels, TikTok, you name it. Load a clip, crop and split it into a
9:16 layout, add highlight boxes and a watermark, cut out the boring parts, add burned-in
subtitles, render, and export. Built by a streamer, for streamers - the whole point is making that
after-stream editing loop fast instead of painful.

**[Discord](https://discord.gg/XhraUHSjSf)** · **[Twitch](https://www.twitch.tv/saltydayn)** ·
**[X / Twitter](https://x.com/saltydayn)** · **[Ko-fi](https://ko-fi.com/saltydayn)** ·
**[Wiki (full guide + screenshots)](../../wiki)** ·
**[Free web tools](https://saltydayn.github.io/web-tools/)**

## What it actually does

- Import a clip from a file or a URL (Twitch, YouTube, and more).
- Crop and lay it out for vertical Shorts - split game/cam view or a single focused panel.
- Add highlight boxes (picture-in-picture), a watermark/branding, keyframed movement over time.
- Cut out dead air with a frame-accurate multi-cut timeline, adjust audio per-section.
- **New in 1.1:** Subtitle Studio. HERMES listens to your cut and writes the subtitles for you.
  Fix any word like you'd fix a typo in a text file, pick a style, and they get burned right
  into the video, karaoke style. It all runs on your own PC, nothing gets uploaded.
- Render in-app, then export and get a one-click hand-off to YouTube's upload page.

Full walkthrough with screenshots is on the [Wiki](../../wiki) - this README stays short on
purpose.

## Getting it

Grab `HERMES-...-setup.exe` from the [Releases page](../../releases) and run it.

1. It installs per-user (no admin prompt needed), adds a Start Menu entry, and offers an
   optional desktop shortcut + launch-on-startup.
2. Your import and export folders are in `Videos\HERMES`, and settings, speech models, and
   subtitles live in `%LOCALAPPDATA%\HERMES`, separate from the program files. Uninstalling
   asks whether to keep or wipe the settings folder (keeps by default).
3. From then on HERMES updates itself - you'll get a banner when a new version is out.

Windows will probably show a **"Windows protected your PC"** SmartScreen warning on first run,
since the app isn't code-signed yet - click **More info -> Run anyway**. That's normal for small
unsigned apps and will get sorted out down the line.

## Requirements

- Windows 10 or newer, 64-bit. No Python install needed.
- ffmpeg isn't bundled to keep the download small. HERMES offers to grab it for you the first
  time something needs it.
- Subtitles need a speech model (roughly 0.5 to 3 GB depending on which one you pick), downloaded
  from the Downloads tab when you want it. Any PC works; an NVIDIA card makes it a lot faster
  with the optional GPU add-on, also in the Downloads tab.

## What goes online

Your clips, edits, and subtitles never leave your PC. HERMES only talks to the internet for
these:

- **Update check:** a tiny version file from GitHub when the app starts (you can turn this off
  on Home) or when you click Check now.
- **Supporters list:** the About tab fetches the current supporter names when the app starts.
- **Feedback:** only when you hit Send in the feedback or bug-report window. It sends what you
  typed there (plus your name or contact if you add one) and the HERMES version number.
- **Downloads you start:** ffmpeg, speech models (from Hugging Face), the GPU add-on, app
  updates, and any clip URL you paste into Import.

No accounts, no tracking, no analytics.

## Running from source

```
pip install -r requirements.txt
python main.py
```

You'll also need `ffmpeg.exe` on your PATH or placed at `assets/ffmpeg.exe` (not included in
this repo - grab a build from ffmpeg.org), unless you let the app download it for you.

## License

Source-available, not open-source - you're welcome to read the code and run the official
builds, but redistributing, reselling, or shipping a modified/rebranded version isn't allowed
without asking first. See [LICENSE](LICENSE) for the actual terms, and
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for the open-source pieces HERMES is built on.

## Support

Everything above is free, no ads, nothing paywalled. If you want to support development, the
[Ko-fi](https://ko-fi.com/saltydayn) has supporter tiers with perks like a vote in what gets
built next - never anything that locks features behind a paywall. Bug reports, feature ideas,
and just showing up on Discord all help just as much. The feedback and bug-report buttons are
in the About tab.
