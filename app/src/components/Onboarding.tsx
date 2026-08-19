import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * Three beats: what this is → pick the brain (OpenRouter key or local Ollama) →
 * go. The optional Instagram feedback module gets its own guided flow later
 * (Settings → Connect Instagram), so first-run stays under a minute.
 */

interface Props {
  onDone: () => void
}

export default function Onboarding({ onDone }: Props) {
  const [step, setStep] = useState(0)
  const [key, setKey] = useState('')
  const [saved, setSaved] = useState(false)
  const [ollama, setOllama] = useState<{ running: boolean; models: string[] } | null>(null)

  useEffect(() => {
    api.checkOllama().then(setOllama).catch(() => setOllama({ running: false, models: [] }))
  }, [])

  async function saveKey() {
    if (!key.trim()) return
    await api.saveLlmKey('openrouter', key)
    setSaved(true)
  }

  return (
    <div className="onboarding">
      <div className="grain" />
      {step === 0 && (
        <section className="ob-step" key="s0">
          <p className="ob-kicker">publikclip</p>
          <h1 className="ob-title">
            THE CLIPPER
            <br />
            THAT SHOWS
            <br />
            ITS WORK<span className="amber">.</span>
          </h1>
          <p className="ob-body">
            Long video in, vertical clips out. Speech, laughter, speakers, and camera
            moves are all computed <em>on this machine</em>. The only thing that ever
            leaves it is two or three small text calls to score your moments — and
            every score comes with the full audit trail of how it was made.
          </p>
          <button className="btn-primary" onClick={() => setStep(1)}>
            Set it up
          </button>
        </section>
      )}
      {step === 1 && (
        <section className="ob-step" key="s1">
          <p className="ob-kicker">01 / the scoring brain</p>
          <h2 className="ob-h2">Pick how moments get judged</h2>
          <div className="ob-cards">
            <div className={`ob-card ${saved ? 'done' : ''}`}>
              <h3>OpenRouter key <span className="chip chip-amber">recommended</span></h3>
              <p>
                Bring your own key (openrouter.ai/keys). Pick any model — the
                default is GLM-4.5V. Best humor and shock judgment.
              </p>
              <div className="ob-key-row">
                <input
                  type="password"
                  placeholder="sk-or-v1-…"
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && saveKey()}
                />
                <button className="btn-secondary" onClick={saveKey} disabled={!key.trim()}>
                  {saved ? 'Saved ✓' : 'Save'}
                </button>
              </div>
            </div>
            <div className={`ob-card ${ollama?.running ? '' : 'dim'}`}>
              <h3>
                Ollama <span className={`led ${ollama?.running ? 'led-on' : 'led-off'}`} />
              </h3>
              <p>
                {ollama === null
                  ? 'Checking…'
                  : ollama.running
                    ? `Running locally (${ollama.models.filter((m) => !m.includes('embed')).slice(0, 2).join(', ') || 'no chat models'}). Zero cost, fully offline — scores are labeled "local estimate" because small models judge humor less reliably.`
                    : 'Not detected. Install ollama.com and pull a model (e.g. llama3.1:8b) to run fully offline.'}
              </p>
            </div>
          </div>
          <p className="ob-fine">
            You can switch per-run. Everything else — transcription, laughter
            detection, speaker tracking, rendering — is local either way.
          </p>
          <button
            className="btn-primary"
            onClick={() => setStep(2)}
            disabled={!saved && !ollama?.running}
          >
            Continue
          </button>
        </section>
      )}
      {step === 2 && (
        <section className="ob-step" key="s2">
          <p className="ob-kicker">02 / one honest warning</p>
          <h2 className="ob-h2">First run downloads the models</h2>
          <p className="ob-body">
            About <span className="mono">2.5 GB</span> of open speech and audio models,
            fetched once into <span className="mono">~/.publikclip</span>. An hour-long
            podcast then takes a while on-device — the progress bar never lies to you,
            and every stage checkpoints, so you can quit and resume anytime.
          </p>
          <button className="btn-primary" onClick={onDone}>
            Open the studio
          </button>
        </section>
      )}
    </div>
  )
}
