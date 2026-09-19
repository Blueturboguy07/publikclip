import { useCallback, useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { api } from '../api'
import type { PublikStatus } from '../types'
import PublikCard from './PublikCard'

/** Post-onboarding key management — the onboarding-only input was a gap. */

interface Props {
  onClose: () => void
  onPublikChange?: () => void
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

export default function KeyModal({ onClose, onPublikChange }: Props) {
  const [key, setKey] = useState('')
  const [hasKey, setHasKey] = useState<boolean | null>(null)
  const [saved, setSaved] = useState(false)
  const [publik, setPublik] = useState<PublikStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)

  const refreshPublik = useCallback(() => {
    api
      .publikStatus()
      .then((s) => {
        setPublik(s)
        onPublikChange?.()
      })
      .catch(() => setPublik(null))
  }, [onPublikChange])

  useEffect(() => {
    invoke<{ has_gemini_key: boolean }>('get_setup_state').then((s) => setHasKey(s.has_gemini_key))
    refreshPublik()
  }, [refreshPublik])

  async function save() {
    if (!key.trim()) return
    await invoke('save_gemini_key', { key })
    setSaved(true)
    setHasKey(true)
  }

  async function connect() {
    setBusy(true)
    setNote(null)
    try {
      setPublik(await api.publikProvision())
      onPublikChange?.()
    } catch (err) {
      setNote(String(err))
    } finally {
      setBusy(false)
    }
  }

  async function disconnect() {
    setBusy(true)
    setNote(null)
    try {
      await api.publikDisconnect()
      refreshPublik()
    } catch (err) {
      setNote(String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <p className="audit-kicker">THE BRAIN</p>
          <button className="btn-ghost" onClick={onClose}>close ✕</button>
        </header>

        {/* publik API first: it is the default, and the plan button has to be
            reachable from inside the app for as long as this computer is
            unlinked (publik's contract, section 12.2). */}
        <PublikCard
          status={publik}
          busy={busy}
          note={note}
          onConnect={connect}
          onDisconnect={publik?.provisioned ? disconnect : undefined}
          variant="settings"
        />

        <p className="audit-label" style={{ marginTop: 22 }}>MY OWN GEMINI KEY</p>
        <p className="ig-intro">
          Prefer your own Google key? Gemini scores at the same quality; the key lives in{' '}
          <span className="mono">~/.publikclip/secrets.json</span>, chmod 600, and never goes
          anywhere but Google.{' '}
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
