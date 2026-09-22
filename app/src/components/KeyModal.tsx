import { useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { openUrl } from '@tauri-apps/plugin-opener'
import { api } from '../api'
import type { PublikStatus } from '../types'
import { PUBLIK_DATA_PATH, PUBLIK_PRE_SETUP, PublikReady } from './PublikCard'

/** Post-onboarding brain + key management: publik API first, then your own
 * Gemini key, then Pexels. */

function PublikRow() {
  const [publik, setPublik] = useState<PublikStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)

  useEffect(() => {
    api.publikStatus().then(setPublik).catch(() => setPublik(null))
  }, [])

  async function act(fn: () => Promise<PublikStatus>) {
    setBusy(true)
    setNote(null)
    try {
      setPublik(await fn())
    } catch (err) {
      setNote(String(err))
    } finally {
      setBusy(false)
    }
  }

  const ready = publik?.provisioned && !publik.status?.disconnected
  const state = publik === null
    ? 'checking…'
    : publik.status?.disconnected
      ? 'Disconnected'
      : publik.provisioned
        ? 'Ready'
        : 'Not set up'

  return (
    <div className="publik-row">
      <p className="audit-label">PUBLIK API · {state.toUpperCase()}</p>
      {ready && publik ? (
        <PublikReady publik={publik} />
      ) : (
        <>
          <p className="ig-intro">{PUBLIK_PRE_SETUP}</p>
          <p className="ig-intro">{PUBLIK_DATA_PATH}</p>
        </>
      )}
      <div className="publik-actions">
        {ready ? (
          <button className="btn-ghost" disabled={busy} onClick={() => act(api.publikDisconnect)}>
            Disconnect publik API
          </button>
        ) : (
          <button className="btn-secondary" disabled={busy || publik === null} onClick={() => act(api.publikProvision)}>
            {busy ? 'Setting up…' : publik?.status?.disconnected ? 'Reconnect publik API' : 'Set up publik API'}
          </button>
        )}
        <button className="btn-ghost" onClick={() => openUrl('https://publikhq.com/developers#why')}>
          How pricing works ↗
        </button>
      </div>
      {note && <p className="ig-message mono">{note}</p>}
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
        <p className="ig-message mono">
          Applies to new runs; a job mid-flight keeps the brain it started with.
        </p>
      </div>
    </div>
  )
}
