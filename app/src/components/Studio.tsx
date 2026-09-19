import { useCallback, useEffect, useState } from 'react'
import { openUrl } from '@tauri-apps/plugin-opener'
import { api } from '../api'
import type { JobSummary, PublikStatus } from '../types'
import KeyModal from './KeyModal'
import { PublikActions, balanceLine } from './PublikCard'

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

/** The value here IS the string run_job forwards as --llm. */
const BRAINS: Array<[string, string]> = [
  ['publik', 'publik API'],
  ['gemini', 'my Gemini key'],
  ['ollama', 'ollama']
]

interface Props {
  jobs: JobSummary[]
  running: boolean
  stages: Record<string, { fraction: number; message: string }>
  error: string | null
  /** Bumped by App whenever a run finishes, so the balance line is never stale. */
  publikTick?: number
  onRun: (source: string, llm: string, captions: string) => void
  onOpenLoop: () => void
  onOpenJob: (id: string) => void
  onResume: (id: string, llm?: string) => void
}

export default function Studio({ jobs, running, stages, error, publikTick, onRun, onOpenLoop, onOpenJob, onResume }: Props) {
  const [source, setSource] = useState('')
  const [llm, setLlm] = useState('publik')
  const [captions, setCaptions] = useState('classic')
  const [showKey, setShowKey] = useState(false)
  const [publik, setPublik] = useState<PublikStatus | null>(null)

  const refreshPublik = useCallback(() => {
    api.publikStatus().then(setPublik).catch(() => setPublik(null))
  }, [])

  // Re-read on mount and after every finished run: a 402 mid-run has to show
  // its link the moment the run stops, not on the next launch.
  useEffect(() => {
    refreshPublik()
  }, [refreshPublik, publikTick])

  // The picker starts on whatever this computer is actually set up for.
  useEffect(() => {
    if (!publik) return
    if (publik.provisioned && !publik.status.disconnected) setLlm('publik')
    else
      api
        .setupState()
        .then((s) => setLlm(s.has_gemini_key ? 'gemini' : 'ollama'))
        .catch(() => setLlm('gemini'))
    // Only on the first status read; after that the person owns the choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publik?.provisioned, publik?.status.disconnected])

  return (
    <div className="studio">
      <div className="grain" />
      {showKey && <KeyModal onClose={() => setShowKey(false)} onPublikChange={refreshPublik} />}
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
          {publik?.provisioned && !publik.status.disconnected && !publik.status.needs_credit && (
            <p className="rail-empty mono">publik API · {balanceLine(publik)}</p>
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
                  onClick={() => setLlm(mode)}
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

        {publik?.provisioned && publik.status.needs_credit && (
          <section className="error-block">
            <span className="led led-err" />
            <span>
              publik API needs credit. {balanceLine(publik)}.
            </span>
            {/* Exactly one link, and it is the one publik chose for this state. */}
            <PublikActions status={publik} />
            <button className="btn-ghost" onClick={() => setShowKey(true)}>
              Use my own key instead
            </button>
          </section>
        )}

        {publik?.provisioned && publik.status.disconnected && (
          <section className="error-block">
            <span className="led led-err" />
            <span>publik API is disconnected on this computer.</span>
            <button className="btn-ghost" onClick={() => setShowKey(true)}>
              Reconnect or use my own key
            </button>
          </section>
        )}

        {!publik?.provisioned && publik?.available === false && (
          <section className="error-block">
            <span className="led led-half" />
            <span>
              This build carries no publik API token. Paste your own Gemini key, or run Ollama.
            </span>
            <button className="btn-ghost" onClick={() => void openUrl('https://publikhq.com/publikclip')}>
              What is publik API?
            </button>
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
