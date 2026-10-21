# Changelog

## v1.1.0 - "Athena"

The big one: subtitles. HERMES can now listen to your clip and burn karaoke-style
subtitles right into the video.

**Subtitle Studio (new tab)**
- Transcribes your clip's finished cut, so the words line up with the edit you
  actually made. Runs fully on your PC, nothing gets uploaded.
- Works on any PC. With an NVIDIA card it can use the GPU and gets a lot faster.
- Language auto-detect, plus hints for game terms and a second language, so names and
  callouts get spelled right more often.
- Fix the transcript like a text file: click a word and type. Space splits a word,
  Backspace joins two, Enter starts a new row, Ctrl+Z and Ctrl+Y undo and redo.
- Words the model wasn't sure about are marked, and "Review flagged" walks you
  through them one by one.
- A timeline with the audio waveform: drag words to retime them, right-click to add a
  word or a label like [laughs].
- A live 9:16 preview that shows exactly what gets burned in. Drag the subtitles to
  move them.
- Styles: presets, fonts (four bundled, your system fonts, or your own), colors,
  outline, an active-word highlight, one word at a time / build-up / full line,
  pop and fade animations, and Shake or Pop on single words for emphasis.
- Save your own looks as slots. Each clip remembers its style, and new clips start
  with the last one you used.
- Optional .srt file next to the video for platforms with their own captions.
- "Keep in sync" re-transcribes on its own when you change the cuts, but asks first if
  you fixed words by hand. If the cuts changed and the subtitles weren't redone, the
  render asks instead of burning in subtitles that no longer line up.

**Downloads tab (new)**
- Speech models and the optional GPU add-on download here when you want them,
  so the app itself stays small. You see the size before anything downloads.

**Clip Editor**
- The editor preview and your renders now include the subtitles you set up in
  Subtitle Studio.

**Home & About**
- One "All of Saltydayn's Links" button with every social icon (now including GitHub),
  plus buttons to create your own free Linktree and to more free tools.
- Icons and module cards are drawn smoothly now instead of looking jagged, and every
  module card has its own icon and a short description.
- "by Saltydayn" next to the name on Home and in the window title.
- After every update, a short "what's new" window tells you what changed and where to
  find it.
- Import and export folders now default to `Videos\HERMES\Import` and
  `Videos\HERMES\Export` instead of a hidden AppData folder. If you were using the old
  default folders, HERMES switches you over; clips you already have stay where they were.

**Fixes and safety**
- The name plate now shows umlauts and accents properly (it used to print "??"), and it
  uses the same font as your subtitles.
- Branding images in folders with umlauts or accents load correctly.
- Your settings are saved safely: a crash or power cut mid-save can't wipe them anymore,
  and HERMES keeps a backup it restores from automatically.
- Clips with a variable frame rate (common from phones and some recorders) get a warning
  on import, since their sound can drift out of sync.
- Self-updates work for Windows user names with umlauts or accents (from this version on),
  and keeping a downloaded update for later no longer throws it away.
- "Remove GPU mode" tells you when it needs a restart to finish.
- HERMES now writes a small log file (`hermes.log` in its data folder), so bug reports can
  say what actually happened. If a tab fails to load, you get told instead of it just
  missing.
- "Report a bug" has an "Open log folder" button, and both feedback windows let you add
  your name or contact, or tick "Stay anonymous".
- HERMES now remembers edits and subtitles for your last 200 clips (was 25 for edits and
  50 for subtitles). You can change it on Home under Settings, from 50 up to 1000.
- Saving a file as `con`, `nul` or similar Windows-reserved names no longer fails.
- Smaller download and install: about 430 MB of unused files are gone from the app.

## v1.0.2

Small fix release. A few app icons (Discord, Ko-fi, X, and the window's own icon)
and the About tab's supporter data weren't actually making it into the installer -
they're bundled correctly now.

## v1.0.1

A smaller release focused on Home and distribution, plus the update mechanism itself
going live for the first time.

**Home**
- The app opens at a bigger default size now, so the settings below the module cards
  aren't hiding below the fold on a smaller window.
- Settings moved into their own collapsible panel that stays put no matter how the
  window is resized, instead of getting squeezed off screen.
- You can now remove HERMES's own downloaded ffmpeg copy from Home, not just download it.

**Distribution**
- Installer only from here on. No more portable zip, one clean download link.
- HERMES can now check for updates and apply them itself in the background, without
  needing to run the installer again by hand.

## v1.0.0

The first real release. Since the last beta round, here's what changed:

**Editor, easier to actually find things in**
- A persistent toolbar for the actions you use constantly - cut, add keyframe, set in/out, render.
- The inspector is now split into clear, collapsible categories instead of one long scroll.
- Obvious resize handles on every box, in both the source and preview views.
- Render status is impossible to miss now - a red "not rendered" warning shows up before you
  accidentally export an unedited clip.
- The keyboard cheat sheet (`?`) is easier to find and stays up to date with your actual keybinds.

**Branding and watermarks**
- What used to be a single avatar image is now a full list of image overlays - add as many as you
  want, drag to place, drag the handles to resize, adjust opacity per image.

**Undo/redo and safety**
- Multi-step undo/redo across the whole edit - not just one step back.
- Your edits autosave per clip automatically, with a one-time notice the first time you use the
  Editor so you know your work isn't at risk.
- A guardrail on very long source clips (10+ minutes) so you don't accidentally start editing
  something that's going to be miserable to work with.

**Audio**
- Per-section volume control on the multi-cut timeline, on top of the whole-clip volume.
- Mark any region of the audio independent of your cuts and adjust its volume on its own.

**Import**
- A recent-files list and a configurable default import folder.
- An optional "detect new clips" checkbox that spots freshly added files in your folder so you can
  import with one click instead of hunting for the file.
- A confirmation prompt before swapping out a clip you're already working on.

**Performance**
- Fixed the preview lag when dragging boxes, the seam, or highlights around - it was a debounce
  bug letting real work fire on every single mouse-move instead of being capped.
- Fixed a much bigger one: jumping to a new part of a longer clip could take several seconds. Now
  it's immediate.

**Onboarding**
- A first-launch prompt and short guided tour on your first visit to every tab, so new features
  aren't a mystery. Fully optional and can be turned off (or replayed) any time from Home.

**Feedback**
- "Send feedback" and "Report a bug" buttons right in the About tab - no more digging for an email
  address.

**Everything else**
- Dozens of smaller bug fixes and polish passes caught along the way, mostly things testers ran
  into during the beta.
