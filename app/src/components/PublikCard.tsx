import { useState } from 'react'
import { openUrl } from '@tauri-apps/plugin-opener'
import { api, dollars } from '../api'
import type { PublikStatus } from '../types'

/**
 * The publik pieces onboarding, Settings and the studio share. The app opens
 * with Sign in with publik (the publik module's gate, src-tauri/src/publik.rs),
 * so by the time these render the person is signed in. What is left to show:
 * who is signed in, the plan or balance, and the one setting the module asks
 * for: how the app pays for AI (Connect your plan, or Use my own key).
 */

export const PUBLIK_DATA_PATH =
  "Your transcript slices and a few low-res frames go through publik's servers " +
  'to score your moments. Everything else stays on this machine, and you can ' +
  'switch to your own key at any time.'

export function hasPlan(p: PublikStatus | null): boolean {
  if (!p) return false
  return p.plan.kind !== 'none' || (p.balance_micros ?? 0) > 0
}

/** "publik API · Plan: Basic" / "publik API · $3.12" / "publik API · $0.00". */
export function balanceLine(p: PublikStatus | null): string {
  if (!p) return 'publik API'
  if (p.plan.kind === 'plan') return `publik API · Plan: ${p.plan.label ?? 'publik'}`
  const b = p.balance_micros
  return b == null ? 'publik API' : `publik API · ${dollars(b)}`
}

/** "Paying for AI with your publik plan" / "with your own key" / "not chosen yet". */
export function choiceLine(p: PublikStatus | null): string {
  if (p?.choice === 'publik') return 'Paying for AI with your publik plan.'
  if (p?.choice === 'own_key') return 'Paying for AI with your own key.'
  return 'How this app pays for AI is not chosen yet.'
}

/** Signed in as @handle, the balance line, the wallet link. */
export function PublikReady({ publik }: { publik: PublikStatus }) {
  return (
    <div className="publik-ready">
      <p className="publik-balance mono">
        {publik.handle ? `Signed in as @${publik.handle}` : 'Signed in to publik'} · {balanceLine(publik)}
      </p>
      <p className="publik-fine">{PUBLIK_DATA_PATH}</p>
      <button className="btn-secondary publik-link" onClick={() => openUrl(publik.top_up_url)}>
        {hasPlan(publik) ? 'Open your publik wallet' : 'Get a plan'}
      </button>
    </div>
  )
}

/**
 * The setting the publik module asks for: set_choice("publik") after Connect
 * your plan, set_choice("own_key") after Use my own key, set_choice(null) to
 * see the gate's question again at the next start.
 */
export function PayChoice({
  publik,
  onChange
}: {
  publik: PublikStatus | null
  onChange: (p: PublikStatus) => void
}) {
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)

  async function choose(choice: 'publik' | 'own_key' | null) {
    setBusy(true)
    setNote(null)
    try {
      onChange(await api.publikSetChoice(choice))
    } catch (err) {
      setNote(String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="publik-choice">
      <p className="publik-fine">{choiceLine(publik)}</p>
      <div className="publik-actions">
        <button
          className={publik?.choice === 'publik' ? 'btn-secondary' : 'btn-primary'}
          onClick={() => choose('publik')}
          disabled={busy || publik?.choice === 'publik'}
        >
          Connect your plan
        </button>
        <button
          className={publik?.choice === 'own_key' ? 'btn-secondary' : 'btn-primary'}
          onClick={() => choose('own_key')}
          disabled={busy || publik?.choice === 'own_key'}
        >
          Use my own key
        </button>
        <button className="btn-ghost" onClick={() => choose(null)} disabled={busy || !publik?.choice}>
          Ask me again at the next start
        </button>
      </div>
      {note && <p className="ig-message mono">{note}</p>}
    </div>
  )
}
