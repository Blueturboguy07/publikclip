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
- **Music-type suggestions** — an editable genre/mood/energy brief derived from
  what's being said and how it sounds
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
Scoring runs on publik API by default (no key to paste); your own Gemini key
and a local Ollama model are both one tap away.

Runs on macOS (Apple silicon) and Windows 10/11 x64. The Windows path is
validated on every push by the `windows` workflow: env resolve, full test
suite, NSIS build, silent install, and a launch of the installed app on a
clean VM.

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
machine has none. Scoring runs on publik API by default; your own Gemini API
key or a local Ollama model (reduced scoring quality) are one tap away in
onboarding.

### Scoring on publik API

publik API is preselected in onboarding. Tapping **Continue with publik API**
sets this computer up — no account, no key to paste — and the card that
appears then shows the free starter balance, one sentence on why the calls
cost anything, and a button to link the computer to a publik account and pick
a plan. Nothing is sent to publik before that tap, and nothing is ever charged
without it showing on the card and on
[publikhq.com/dashboard/api](https://publikhq.com/dashboard/api).

The key that provisioning returns lives beside the others, in
`~/.publikclip/secrets.json` (chmod 600), under `"publik"`. Your own Gemini key
and your Pexels key are never touched by it. `PUBLIK_API_KEY` and
`PUBLIK_API_BASE_URL` override the file for a single run.

**Building from source with a publik token.**
`app/src-tauri/publik-app-token.txt` ships a placeholder
(`pat_publikclip_REPLACE_ME`). A build carrying the placeholder never posts
anything: the publik card says "Not available in this build" and onboarding
falls through to the own-key card. To build one that can provision, put the
public `pat_publikclip_…` token in that file (or set `PUBLIK_APP_TOKEN` in the
build environment) before `npx tauri build`. The token is public by design —
it authorises minting one capped, install-bound key attributed to publikclip,
which is exactly what the app does with it.

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
