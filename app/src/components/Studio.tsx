import { useCallback, useEffect, useRef, useState } from 'react'
import { getCurrentWebview } from '@tauri-apps/api/webview'
import { open as openFileDialog } from '@tauri-apps/plugin-dialog'
import { openUrl } from '@tauri-apps/plugin-opener'
import { api } from '../api'
import { VIDEO_EXTENSIONS, pickVideo } from '../files'
import type { JobSummary, PublikStatus, UpdateNotice } from '../types'
import KeyModal from './KeyModal'
import { LINK_GATE_TITLE, LinkGateCard, accountLink, balanceLine, claimState, linkGate } from './PublikCard'

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
  const [dragging, setDragging] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  // true once GET /wallet has answered (or failed) this session: until then
  // an unlinked answer from the files on disk may just be stale
  const [walletChecked, setWalletChecked] = useState(false)
  // A newer build exists (one GET on launch; offline this stays null).
  const [update, setUpdate] = useState<UpdateNotice | null>(null)
  useEffect(() => {
    api
      .checkUpdate()
      .then((u) => setUpdate(u.update_available ? u : null))
      .catch(() => {})
  }, [])
  const pickedBrain = useRef(false)
  // The drag-drop listener is registered once; it reads this instead of
  // re-subscribing (and missing a drop) every time a run starts or ends.
  const runningRef = useRef(running)
  runningRef.current = running

  const chooseFile = useCallback(async () => {
    try {
      const picked = await openFileDialog({
        multiple: false,
        directory: false,
        filters: [{ name: 'Video', extensions: VIDEO_EXTENSIONS }]
      })
      if (typeof picked === 'string') {
        setSource(picked)
        setNotice(null)
      }
    } catch (err) {
      setNotice(`Couldn't open the file picker: ${String(err)}`)
    }
  }, [])

  // Tauri takes over file drops on the window, so the DOM never sees a
  // `drop` with file paths — the webview's own drag-drop event is the only
  // way to learn where the file is.
  useEffect(() => {
    let unlisten: (() => void) | undefined
    let disposed = false
    getCurrentWebview()
      .onDragDropEvent((event) => {
        const drag = event.payload
        if (drag.type === 'enter' || drag.type === 'over') {
          if (!runningRef.current) setDragging(true)
        } else if (drag.type === 'leave') {
          setDragging(false)
        } else if (drag.type === 'drop') {
          setDragging(false)
          if (runningRef.current) return
          const video = pickVideo(drag.paths)
          if (video) {
            setSource(video)
            setNotice(null)
          } else {
            setNotice("That isn't a video file. Drop an MP4, MOV, MKV or WEBM.")
          }
        }
      })
      .then((stop) => {
        if (disposed) stop()
        else unlisten = stop
      })
      .catch(() => {})
    return () => {
      disposed = true
      unlisten?.()
    }
  }, [])

  // Ask the server (GET /wallet): the files on disk only change at mint or
  // after a publik API call, so a computer linked since then still read
  // "anonymous". Offline, the Rust side returns the files unchanged.
  const refreshPublik = useCallback(() => {
    api
      .publikRefresh()
      .catch(() => api.publikStatus())
      .then(setPublik)
      .catch(() => {})
      .finally(() => setWalletChecked(true))
  }, [])

  // First paint from the files (no network); the refresh below replaces it.
  useEffect(() => {
    api
      .publikStatus()
      .then((p) => setPublik((cur) => cur ?? p))
      .catch(() => {})
  }, [])

  // On mount and after every run (maybe ended on a 402): ask the server.
  useEffect(() => {
    if (!running) refreshPublik()
  }, [running, refreshPublik])

  // Default brain is publik API once this computer is linked — never the
  // user's own Gemini key just because one is saved (a job once started 18 s
  // before the key finished minting and billed that key directly). Once the
  // user picks a brain this session, the picker is theirs.
  const gate = linkGate(publik)
  const linked = gate === 'linked'
  useEffect(() => {
    if (linked && !pickedBrain.current) setLlm('publik')
  }, [linked])

  const canRun = linked && !running && source.trim() !== ''
  const st = publik?.status
  const link = accountLink(publik)
  const topUp = st?.top_up_url ?? link?.url ?? null

  return (
    <div className="studio">
      <div className="grain" />
      {update && (
        <section className="publik-banner update-banner">
          <span className="led led-half" />
          <div>
            <strong>publikclip {update.latest} is out.</strong> You have {update.current}.
            <div className="publik-actions">
              <button className="btn-secondary" onClick={() => openUrl(update.url)}>
                Download
              </button>
              <button className="btn-ghost" onClick={() => setUpdate(null)}>
                Later
              </button>
            </div>
          </div>
        </section>
      )}
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
              disabled={running || (!job.rendered && !linked)}
              title={job.rendered ? 'open results' : linked ? 'resume from checkpoint' : LINK_GATE_TITLE}
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
        {dragging && (
          <div className="drop-overlay" aria-hidden="true">
            <span className="drop-overlay-text">DROP A VIDEO TO LOAD IT</span>
          </div>
        )}
        <section className="input-block">
          <h1 className="input-heading">
            FEED IT<span className="amber"> AN HOUR.</span>
          </h1>
          <div className="input-row">
            <input
              value={source}
              onChange={(e) => {
                setSource(e.target.value)
                setNotice(null)
              }}
              onKeyDown={(e) => e.key === 'Enter' && canRun && onRun(source.trim(), llm, captions)}
              placeholder="Paste a YouTube URL, or drop a video here"
              disabled={running}
            />
            <button className="btn-secondary" onClick={chooseFile} disabled={running}>
              CHOOSE FILE
            </button>
            <button
              className="btn-primary"
              onClick={() => onRun(source.trim(), llm, captions)}
              disabled={!canRun}
              title={linked ? undefined : LINK_GATE_TITLE}
            >
              {running ? 'WORKING' : 'CUT IT'}
            </button>
          </div>
          {notice && <p className="input-notice mono">{notice}</p>}
          {!linked && !running && (
            <LinkGateCard
              publik={publik}
              checking={!walletChecked && gate !== 'off'}
              onChange={setPublik}
            />
          )}
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

        {linked && st?.needs_credit && (
          <section className="publik-banner">
            <span className="led led-half" />
            <div>
              <strong>publik API needs more balance.</strong>{' '}
              {st.message ?? "This computer's publik balance is used up."}
              <div className="publik-actions">
                {topUp && (
                  <button className="btn-secondary publik-link" onClick={() => openUrl(topUp)}>
                    Add a plan or pack
                  </button>
                )}
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
