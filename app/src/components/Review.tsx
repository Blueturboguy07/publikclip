import { useMemo, useState } from 'react'
import { api } from '../api'
import type { Clip, JobResults, RenderOutput } from '../types'
import ClipEditor from './ClipEditor'

/**
 * The review bay: filmstrip of rendered clips, a 9:16 monitor, and THE
 * AUDIT — the score's full provenance. This panel is the product thesis:
 * never a bare number.
 */

const RESTYLE_PRESETS = ['classic', 'beast', 'hormozi', 'minimal', 'karaoke-pop']
const CAMERA_MODES: [string, string][] = [
  ['cut', 'hard cut on speaker change'],
  ['pan', 'eased pan between speakers'],
  ['locked', 'static crop, no switching']
]

interface Props {
  results: JobResults
  onBack: () => void
  onRestyle: (captions: string, camera: string) => void
}

const RULE_LABELS: Record<string, string> = {
  funny_no_laugh: 'FUNNY, NO LAUGHTER',
  funny_corroborated: 'LAUGHTER CONFIRMED ×2',
  shock_no_arousal: 'SHOCK, FLAT DELIVERY',
  bait_penalty: 'ENGAGEMENT BAIT',
  heatmap_boost: 'HUMANS REPLAYED THIS'
}

const SIGNAL_LABELS: Record<string, string> = {
  laughter: 'laughter',
  audio_events: 'audio events',
  arousal: 'vocal arousal',
  replay_heatmap: 'replay heatmap',
  visual: 'visual pass'
}

/** Always name the brain that scored the job: a job scored on the user's own
 *  key must never read like a publik API job. */
function scoredBy(score: JobResults['score']): string {
  const model = score?.model ?? '—'
  switch (score?.llm_mode) {
    case 'publik':
      return `scored by ${model} via publik API`
    case 'gemini':
      return `scored by ${model} via my Gemini key`
    case 'ollama':
      return `scored by ${model} locally (Ollama) · LOCAL ESTIMATE`
    default:
      return `scored by ${model}`
  }
}

function fmtTime(t: number): string {
  const m = Math.floor(t / 60)
  const s = Math.floor(t % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

export default function Review({ results, onBack, onRestyle }: Props) {
  const outputs = results.render?.outputs ?? []
  const clips = results.score?.clips ?? []
  const [selected, setSelected] = useState(0)
  const [exported, setExported] = useState<Record<number, string>>({})
  const currentPreset = results.render?.caption_preset ?? 'classic'
  const [restylePreset, setRestylePreset] = useState(currentPreset)
  const [restyleCamera, setRestyleCamera] = useState('cut')
  const [editing, setEditing] = useState<number | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const styleChanged = restylePreset !== currentPreset || restyleCamera !== 'cut'

  const pair = useMemo(() => {
    const out = outputs[selected]
    const clip = out ? clips[out.clip] : undefined
    return { out, clip }
  }, [outputs, clips, selected])

  async function doExport(out: RenderOutput, clip: Clip) {
    const dest = await api.exportClip(
      out.path,
      `${results.ingest?.title ?? 'clip'} ${fmtTime(clip.start)}`
    )
    setExported((prev) => ({ ...prev, [out.clip]: dest }))
  }

  if (editing !== null) {
    return (
      <div className="review">
        <div className="grain" />
        <ClipEditor
          key={`${editing}-${reloadKey}`}
          jobId={results.job_id}
          clipIndex={editing}
          onClose={() => setEditing(null)}
          onRendered={() => setReloadKey((k) => k + 1)}
        />
      </div>
    )
  }

  return (
    <div className="review">
      <div className="grain" />
      <header className="review-head">
        <button className="btn-ghost" onClick={onBack}>
          ← studio
        </button>
        <div className="review-title-block">
          <h1 className="review-title">{results.ingest?.title ?? results.job_id}</h1>
          <p className="review-sub mono">
            {outputs.length} clips · {scoredBy(results.score)} ·{' '}
            {results.candidates?.heatmap_present ? 'replay heatmap in play' : 'no public heatmap'}
          </p>
        </div>
      </header>

      <div className="restyle-bar">
        <span className="opt-label">captions</span>
        {RESTYLE_PRESETS.map((preset) => (
          <button
            key={preset}
            className={`opt ${restylePreset === preset ? 'opt-on' : ''}`}
            onClick={() => setRestylePreset(preset)}
          >
            {preset}
          </button>
        ))}
        <span className="opt-label" style={{ marginLeft: 18 }}>
          camera
        </span>
        {CAMERA_MODES.map(([mode, hint]) => (
          <button
            key={mode}
            className={`opt ${restyleCamera === mode ? 'opt-on' : ''}`}
            onClick={() => setRestyleCamera(mode)}
            title={hint}
          >
            {mode}
          </button>
        ))}
        <button
          className="btn-primary restyle-go"
          disabled={!styleChanged}
          onClick={() => onRestyle(restylePreset, restyleCamera)}
          title="re-renders only the changed stages — scores and cuts stay"
        >
          RESTYLE + RE-RENDER
        </button>
      </div>

      <div className="filmstrip">
        {outputs.map((out, i) => {
          const clip = clips[out.clip]
          return (
            <button
              key={out.clip}
              className={`film-card ${i === selected ? 'film-on' : ''}`}
              onClick={() => setSelected(i)}
              style={{ animationDelay: `${i * 50}ms` }}
            >
              <span className="film-score mono">{Math.round(clip?.score ?? out.score)}</span>
              <span className="film-time mono">{clip ? fmtTime(clip.start) : ''}</span>
              <span className="film-platform">{out.best_platform}</span>
            </button>
          )
        })}
      </div>

      {pair.out && pair.clip && (
        <div className="bay">
          <div className="monitor-wrap">
            <video
              key={pair.out.path}
              className="monitor"
              src={api.fileUrl(pair.out.path)}
              controls
              playsInline
            />
            <div className="monitor-actions">
              <button className="btn-secondary" onClick={() => setEditing(pair.out!.clip)}>
                ✎ EDIT CLIP (bounds · cuts · visuals)
              </button>
              <button className="btn-primary" onClick={() => doExport(pair.out!, pair.clip!)}>
                {exported[pair.out.clip] ? 'EXPORTED ✓' : 'EXPORT MP4'}
              </button>
              {exported[pair.out.clip] && (
                <span className="mono export-path">{exported[pair.out.clip]}</span>
              )}
            </div>
          </div>

          <aside className="audit">
            <p className="audit-kicker">THE AUDIT</p>
            <div className="audit-score-row">
              <span className="audit-big mono">{Math.round(pair.clip.score)}</span>
              <div className="audit-platforms">
                {Object.entries(pair.clip.platform_scores).map(([platform, value]) => (
                  <div className="platform-row" key={platform}>
                    <span className="platform-name">{platform}</span>
                    <div className="platform-bar">
                      <div className="platform-fill" style={{ width: `${value}%` }} />
                    </div>
                    <span className="mono platform-val">{Math.round(value)}</span>
                  </div>
                ))}
              </div>
            </div>
            <p className="audit-summary">{pair.clip.summary}</p>

            <p className="audit-label">SUBSCORES</p>
            <div className="subs">
              {Object.entries(pair.clip.subscores).map(([name, value]) => (
                <div className="sub-row" key={name}>
                  <span className="sub-name">{name.replace('_', ' ')}</span>
                  <div className="sub-bar">
                    <div className="sub-fill" style={{ width: `${value * 10}%` }} />
                  </div>
                  <span className="mono sub-val">{value.toFixed(1)}</span>
                </div>
              ))}
            </div>

            {pair.clip.adjustments.length > 0 && (
              <>
                <p className="audit-label">ADJUSTMENTS</p>
                <div className="ledger">
                  {pair.clip.adjustments.map((adj, i) => (
                    <div className="ledger-row" key={i}>
                      <span className={`ledger-factor mono ${adj.factor >= 1 ? 'up' : 'down'}`}>
                        ×{adj.factor}
                      </span>
                      <div>
                        <span className="ledger-rule">{RULE_LABELS[adj.rule] ?? adj.rule}</span>
                        <span className="ledger-reason">{adj.reason}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}

            <p className="audit-label">SIGNALS</p>
            <div className="signals">
              {pair.clip.signals_fired.map((signal) => (
                <span className="sig sig-on" key={signal}>
                  <span className="led led-on" />
                  {SIGNAL_LABELS[signal] ?? signal}
                </span>
              ))}
              {pair.clip.signals_missing.map((signal) => (
                <span className="sig sig-off" key={signal}>
                  <span className="led led-off" />
                  {SIGNAL_LABELS[signal] ?? signal}
                </span>
              ))}
            </div>

            {pair.clip.music && (
              <>
                <p className="audit-label">MUSIC BRIEF</p>
                <div className="music-card">
                  <p className="music-main">
                    <span className="amber">{pair.clip.music.genre}</span> ·{' '}
                    {pair.clip.music.mood} · <span className="mono">{pair.clip.music.bpm_range} bpm</span>
                  </p>
                  <p className="music-theme">{pair.clip.music.theme}</p>
                  <p className="music-alt">
                    also try:{' '}
                    {pair.clip.music.alternatives
                      .map((alt) => `${alt.genre} (${alt.bpm_range})`)
                      .join(' / ')}
                  </p>
                </div>
              </>
            )}

            <p className="audit-fine mono">
              confidence: {pair.clip.confidence} · captions: {results.render?.caption_preset} ·{' '}
              {pair.out.words} words · {pair.out.event_tags} event tags
            </p>
          </aside>
        </div>
      )}
    </div>
  )
}
