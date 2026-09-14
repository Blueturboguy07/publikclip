# publikclip

**Long video in. Scored vertical clips out. Everything runs on your machine.**

publikclip is an open-source (AGPL-3.0) desktop app that takes a YouTube URL or a
horizontal video file and produces vertical 9:16 clips with:

- **Smart camera** — active-speaker-tracked crop paths, smoothed motion, hard cuts
  on speaker change, punch-ins fired by actual laughter and vocal energy
- **Word-accurate captions** — multiple styles, karaoke highlighting, prosodic
  emphasis (loud words get loud styling), `[laughs]` tags from real laughter detection
- **A virality score you can audit** — never a bare number: every clip ships with
  its subscores, which detectors fired, and every adjustment applied. LLM humor
  scores get discounted when no actual laughter corroborates them.
- **Music & SFX** — a local, CC-licensed library (import your own or pull from
  Freesound/Jamendo), auto-suggested from a genre/mood/energy brief derived
  from what's being said and how it sounds, with manual placement always
  available and music ducked under speech automatically
- **Optional real-outcomes loop** — connect your own Instagram (via your own Meta
  app, no middleman) and the scorer calibrates against how your clips actually perform

Every model — speech recognition, forced alignment, diarization, laughter
detection, audio tagging, face detection, active-speaker detection — runs
locally. The only network calls are the video download and 2–3 small LLM calls
(bring your own Gemini key, or run fully local via Ollama at reduced scoring
quality).

## Status

Working end to end: hour-long podcast in, rendered/captioned/scored 9:16 clips
out, validated on real footage. The Instagram feedback loop ships in-app
(sync, clip↔Reel matching, snapshot history, automatic score calibration).
Builds are currently unsigned — install from source below, or follow the
guided install at [publikhq.com/publikclip](https://publikhq.com/publikclip).

Runs on macOS (Apple silicon) and Windows 10/11 x64. The Windows path is
validated on every push by the `windows` workflow: env resolve, full test
suite, NSIS build, silent install, and a launch of the installed app on a
clean VM.

## Music & SFX

Every clip gets its own music/sfx track, backed by a local library at
`~/.publikclip/audio/` (files copied in, plus an index of tags/bpm/licence).
The library starts empty — fill it from your own files, or pull
Creative-Commons tracks from Freesound/Jamendo straight from the app.

- **Local import** — Library tab → **files**/**folder** opens a native
  picker. Kind (music vs. sfx) and tags are derived automatically; bpm is
  detected for music.
- **Online CC sources** — the Online tab searches Freesound (sound effects)
  and Jamendo (music), CC0-only by default. Both need a free API key, saved
  once in the app: [freesound.org/apiv2/apply](https://freesound.org/apiv2/apply/)
  and [devportal.jamendo.com](https://devportal.jamendo.com/) — the panel
  shows the signup link inline when a key is missing.
- **Suggestions** — **suggest audio** scores your library against the
  clip's music brief (genre/mood/bpm/energy) and the event timeline (cut
  boundaries, laughs), and proposes one music bed plus a few sfx stings.
  Suggested items show dashed on the track; accept one with its ✓, or
  delete it — nothing is ever auto-accepted.
- **Manual placement** — drag any library item onto the audio track,
  drag its edges to trim, drag its body to move it. Select an item for
  gain, fade in/out, loop, and duck controls.
- **Ducking** — music tagged to duck sidechains under speech automatically
  at render time (light/medium/heavy, from the clip's brief) so dialogue
  always stays legible; sfx never ducks.
- **Licence policy** — CC0 by default; a CC-BY item requires opting in
  ("allow attribution" in the Online tab) and is never silent about it — a
  `credits.txt` is written next to every rendered clip that uses one,
  listing its attribution. NC and ND licences are never fetched, full stop.

## Layout

```
pipeline/   Python package — the entire processing pipeline + CLI
app/        Tauri v2 desktop shell (React UI, Python sidecar)
```

## Install from source (macOS)

You need four tools: git, [Node](https://nodejs.org), [Rust](https://rustup.rs),
and [uv](https://docs.astral.sh/uv/). Then:

```sh
git clone https://github.com/Blueturboguy07/publikclip.git
cd publikclip/app
npm install
npx tauri build --bundles app
ditto src-tauri/target/release/bundle/macos/publikclip.app /Applications/publikclip.app
open /Applications/publikclip.app
```

The app downloads its speech/audio models (~4–5 GB) on first run with a
progress UI, and fetches a caption-capable static ffmpeg automatically if the
machine has none. Scoring uses your own Gemini API key, or a local Ollama
model at reduced scoring quality — onboarding walks through both.

## Install from source (Windows)

You need [Rust](https://rustup.rs), the Visual Studio **Desktop development
with C++** build tools, [Node](https://nodejs.org), git, and
[uv](https://docs.astral.sh/uv/) (`winget install --id astral-sh.uv -e`).
Then, in PowerShell:

```powershell
git clone https://github.com/Blueturboguy07/publikclip.git
cd publikclip\app
npm.cmd install
node_modules\.bin\tauri.cmd build --bundles nsis
# run the installer it produces:
Start-Process (Get-ChildItem src-tauri\target\release\bundle\nsis -Filter *-setup.exe).FullName
```

First run behaves the same as on macOS: models download behind a progress
bar, and a caption-capable static ffmpeg is fetched automatically.

## Development

```sh
# pipeline
cd pipeline && uv sync && uv run pytest
uv run publikclip run "https://www.youtube.com/watch?v=..."

# app
cd app && npm install && npm run tauri dev
```

## License

AGPL-3.0-or-later. Portions adapted from other open-source projects — see
`VENDORED-LICENSES.md` for the full provenance list.
