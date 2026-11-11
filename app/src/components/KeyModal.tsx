import { useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { openUrl } from '@tauri-apps/plugin-opener'
import { api } from '../api'
import type { PublikStatus } from '../types'
import { PUBLIK_DATA_PATH, PayChoice, PublikReady } from './PublikCard'

/** Post-onboarding brain + key management: publik first (signed in by the
 * gate; the pay choice lives here), then your own Gemini key, then Pexels. */

function PublikRow() {
  const [publik, setPublik] = useState<PublikStatus | null>(null)

  useEffect(() => {
    api
      .publikRefresh()
      .catch(() => api.publikStatus())
      .then(setPublik)
      .catch(() => setPublik(null))
  }, [])

  return (
    <div className="publik-row">
      <p className="audit-label">
        PUBLIK · {publik === null ? 'CHECKING…' : publik.handle ? `@${publik.handle.toUpperCase()}` : 'SIGNED IN'}
      </p>
      {publik ? (
        <>
          <PublikReady publik={publik} />
          <PayChoice publik={publik} onChange={setPublik} />
        </>
      ) : (
        <p className="ig-intro">{PUBLIK_DATA_PATH}</p>
      )}
      <div className="publik-actions">
        <button className="btn-ghost" onClick={() => openUrl('https://publikhq.com/developers#why')}>
          How pricing works ↗
        </button>
      </div>
    </div>
  )
}

interface Props {
  onClose: () => void
}

function PexelsField() {
  const [key, setKey] = useState('')
  const [saved, setSaved] = useState(false)
  return (
    <div className="ig-form">
      <input
        placeholder="Pexels API key (free — pexels.com/api)"
        type="password"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        className="mono"
      />
      <button
        className="btn-secondary"
        disabled={!key.trim()}
        onClick={async () => {
          await invoke('save_pexels_key', { key })
          setSaved(true)
        }}
      >
        {saved ? 'saved ✓' : 'save'}
      </button>
    </div>
  )
}

function FreesoundField() {
  const [key, setKey] = useState('')
  const [saved, setSaved] = useState(false)
  return (
    <div className="ig-form">
      <input
        placeholder="Freesound API key (free — freesound.org/apiv2/apply)"
        type="password"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        className="mono"
      />
      <button
        className="btn-secondary"
        disabled={!key.trim()}
        onClick={async () => {
          await invoke('save_freesound_key', { key })
          setSaved(true)
        }}
      >
        {saved ? 'saved ✓' : 'save'}
      </button>
    </div>
  )
}

function JamendoField() {
  const [key, setKey] = useState('')
  const [saved, setSaved] = useState(false)
  return (
    <div className="ig-form">
      <input
        placeholder="Jamendo client id (free — devportal.jamendo.com)"
        type="password"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        className="mono"
      />
      <button
        className="btn-secondary"
        disabled={!key.trim()}
        onClick={async () => {
          await invoke('save_jamendo_key', { key })
          setSaved(true)
        }}
      >
        {saved ? 'saved ✓' : 'save'}
      </button>
    </div>
  )
}

export default function KeyModal({ onClose }: Props) {
  const [key, setKey] = useState('')
  const [hasKey, setHasKey] = useState<boolean | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    invoke<{ has_gemini_key: boolean }>('get_setup_state').then((s) =>
      setHasKey(s.has_gemini_key)
    )
  }, [])

  async function save() {
    if (!key.trim()) return
    await invoke('save_gemini_key', { key })
    setSaved(true)
    setHasKey(true)
  }

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <p className="audit-kicker">THE BRAIN</p>
          <button className="btn-ghost" onClick={onClose}>close ✕</button>
        </header>
        <PublikRow />
        <p className="audit-label" style={{ marginTop: 22 }}>YOUR OWN GEMINI KEY</p>
        <p className="ig-intro">
          Prefer your own Google key? Gemini scores at the same quality; your key lives
          in <span className="mono">~/.publikclip/secrets.json</span>, chmod 600, and
          never goes anywhere but Google.{' '}
          {hasKey && <strong>A key is currently saved{saved ? ' — updated ✓' : ''}.</strong>}
        </p>
        <div className="ig-form">
          <input
            placeholder="AIza… (aistudio.google.com → Get API key)"
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && save()}
            className="mono"
          />
          <button className="btn-primary" onClick={save} disabled={!key.trim()}>
            {saved ? 'SAVED ✓' : 'SAVE KEY'}
          </button>
        </div>
        <p className="audit-label" style={{ marginTop: 22 }}>PEXELS (STOCK VISUALS)</p>
        <PexelsField />
        <p className="audit-label" style={{ marginTop: 22 }}>FREESOUND (SOUND EFFECTS)</p>
        <FreesoundField />
        <p className="audit-label" style={{ marginTop: 22 }}>JAMENDO (MUSIC)</p>
        <JamendoField />
        <p className="ig-message mono">
          Applies to new runs; a job mid-flight keeps the brain it started with.
        </p>
      </div>
    </div>
  )
}
