import { useCallback, useEffect, useRef, useState } from 'react'
import { openUrl } from '@tauri-apps/plugin-opener'
import { api } from '../api'
import type { JobSummary, PublikStatus } from '../types'
import KeyModal from './KeyModal'
import { accountLink, balanceLine, claimState } from './PublikCard'

const BRAINS: [string, string][] = [
  ['publik', 'publik API'],
  ['gemini', 'my Gemini key'],
  ['ollama', 'ollama']
]

const STAGE_ORDER = [
  'ingest', 'asr', 'diarize', 'events', 'candidates', 'score', 'camera', 'render'
]

const STAGE_LABELS: Record<string, string> = {
  ingest: 'INGEST',
  asr: 'TRANSCRIBE',
  diarize: 'SPEAKERS',
  events: 'LISTEN',
  candidates: 'SCAN',
  score: 'JUDGE',
  camera: 'DIRECT',
  render: 'RENDER'
}

const CAPTION_PRESETS = ['classic', 'beast', 'hormozi', 'minimal', 'karaoke-pop']

interface Props {
  jobs: JobSummary[]
  running: boolean
  stages: Record<string, { fraction: number; message: string }>
  error: string | null
  onRun: (source: string, llm: string, captions: string) => void
  onOpenLoop: () => void
  onOpenJob: (id: string) => void
  onResume: (id: string, llm?: string) => void
}

export default function Studio({ jobs, running, stages, error, onRun, onOpenLoop, onOpenJob, onResume }: Props) {
  const [source, setSource] = useState('')
  const [llm, setLlm] = useState('publik')
  const [captions, setCaptions] = useState('classic')
  const [showKey, setShowKey] = useState(false)
  const [publik, setPublik] = useState<PublikStatus | null>(null)
  const pickedBrain = useRef(false)

  const refreshPublik = useCallback(() => {
    api.publikStatus().then(setPublik).catch(() => setPublik(null))
  }, [])

  // Initial brain from what this computer actually has: publik API if set
  // up, else the user's own Gemini key, else Ollama. Only once — after that
  // the picker is the user's.
  useEffect(() => {
    Promise.all([api.publikStatus().catch(() => null), api.setupState().catch(() => null)]).then(
      ([p, setup]) => {
        setPublik(p)
        if (pickedBrain.current) return
        setLlm(p?.provisioned ? 'publik' : setup?.has_gemini_key ? 'gemini' : 'ollama')
      }
    )
  }, [])

  // A run just ended (maybe on a 402): re-read the balance line / banner.
  useEffect(() => {
    if (!running) refreshPublik()
  }, [running, refreshPublik])

  const st = publik?.status
  const link = accountLink(publik)
  const topUp = st?.top_up_url ?? link?.url ?? null

  return (
    <div className="studio">
      <div className="grain" />
      {showKey && (
        <KeyModal
          onClose={() => {
            setShowKey(false)
            refreshPublik()
          }}
        />
      )}
      <aside className="rail">
        <header className="rail-brand">
          <span className="rail-logo">publikclip</span>
          <span className="rail-sub">the clipper that shows its work</span>
        </header>
        <div className="rail-jobs">
          <p className="rail-label">SESSIONS</p>
          {jobs.length === 0 && <p className="rail-empty">nothing yet</p>}
          {jobs.map((job) => (
            <button
              key={job.id}
              className={`rail-job ${job.rendered ? '' : 'partial'}`}
              onClick={() => (job.rendered ? onOpenJob(job.id) : onResume(job.id))}
              disabled={running}
              title={job.rendered ? 'open results' : 'resume from checkpoint'}
            >
              <span className={`led ${job.rendered ? 'led-on' : 'led-half'}`} />
              <span className="rail-job-title">{job.title ?? job.id}</span>
              <span className="rail-job-hint">{job.rendered ? 'open' : 'resume'}</span>
            </button>
          ))}
        </div>
        <footer className="rail-foot">
          {publik?.provisioned && !st?.needs_credit && !st?.disconnected && (
            <p className="rail-balance mono">
              {balanceLine(publik)}
              {claimState(publik) === 'anonymous' && publik.claim_url && (
                <>
                  {' · '}
                  <button className="btn-link" onClick={() => openUrl(publik.claim_url!)}>
                    Link this computer →
                  </button>
                </>
              )}
            </p>
          )}
          <button className="btn-ghost" onClick={() => setShowKey(true)}>
            ◈ brain &amp; keys
          </button>
          <button className="btn-ghost" onClick={onOpenLoop}>
            ⟳ instagram loop
          </button>
        </footer>
      </aside>

      <main className="stage-area">
        <section className="input-block">
          <h1 className="input-heading">
            FEED IT<span className="amber"> AN HOUR.</span>
          </h1>
          <div className="input-row">
            <input
              value={source}
              onChange={(e) => setSource(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && source.trim() && !running && onRun(source.trim(), llm, captions)}
              placeholder="YouTube URL or a path to a video file"
              disabled={running}
            />
            <button
              className="btn-primary"
              onClick={() => onRun(source.trim(), llm, captions)}
              disabled={running || !source.trim()}
            >
              {running ? 'WORKING' : 'CUT IT'}
            </button>
          </div>
          <div className="run-options">
            <div className="opt-group">
              <span className="opt-label">brain</span>
              {BRAINS.map(([mode, label]) => (
                <button
                  key={mode}
                  className={`opt ${llm === mode ? 'opt-on' : ''}`}
                  onClick={() => {
                    pickedBrain.current = true
                    setLlm(mode)
                  }}
                  disabled={running}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="opt-group">
              <span className="opt-label">captions</span>
              {CAPTION_PRESETS.map((preset) => (
                <button
                  key={preset}
                  className={`opt ${captions === preset ? 'opt-on' : ''}`}
                  onClick={() => setCaptions(preset)}
                  disabled={running}
                >
                  {preset}
                </button>
              ))}
            </div>
          </div>
        </section>

        {(running || Object.keys(stages).length > 0) && (
          <section className="deck">
            {STAGE_ORDER.filter((s) => stages[s] || running).map((name, i) => {
              const st = stages[name]
              const state = !st ? 'idle' : st.fraction >= 1 ? 'done' : 'live'
              return (
                <div className={`deck-row ${state}`} key={name} style={{ animationDelay: `${i * 40}ms` }}>
                  <span className="deck-name mono">{STAGE_LABELS[name] ?? name.toUpperCase()}</span>
                  <div className="deck-bar">
                    <div
                      className={`deck-fill ${st && st.fraction < 0 ? 'indeterminate' : ''}`}
                      style={st && st.fraction >= 0 ? { width: `${Math.min(100, st.fraction * 100)}%` } : undefined}
                    />
                  </div>
                  <span className="deck-msg">{st?.message ?? ''}</span>
                </div>
              )
            })}
          </section>
        )}

        {publik?.provisioned && st?.needs_credit && (
          <section className="publik-banner">
            <span className="led led-half" />
            <div>
              <strong>publik API needs more balance.</strong>{' '}
              {st.message ?? (claimState(publik) === 'anonymous'
                ? 'The free starter usage on this computer is used up.'
                : "This computer's publik balance is used up.")}
              <div className="publik-actions">
                {topUp && (
                  <button className="btn-secondary publik-link" onClick={() => openUrl(topUp)}>
                    {claimState(publik) === 'anonymous' ? 'Link this computer & pick a plan' : 'Add a plan or pack'}
                  </button>
                )}
                <button className="btn-ghost" onClick={() => setShowKey(true)}>
                  Use my own key instead
                </button>
              </div>
            </div>
          </section>
        )}

        {st?.disconnected && (
          <section className="publik-banner">
            <span className="led led-half" />
            <div>
              <strong>publik API is disconnected on this computer.</strong> Its key no
              longer works — reconnect to get a working one, or use your own key.
              <div className="publik-actions">
                <button
                  className="btn-secondary"
                  onClick={() => api.publikProvision().then(setPublik).catch(() => setShowKey(true))}
                >
                  Reconnect
                </button>
                <button className="btn-ghost" onClick={() => setShowKey(true)}>
                  Use my own key instead
                </button>
              </div>
            </div>
          </section>
        )}

        {error && (
          <section className="error-block">
            <span className="led led-err" />
            {error}
          </section>
        )}
      </main>
    </div>
  )
}
