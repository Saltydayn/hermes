
# Changelog

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
