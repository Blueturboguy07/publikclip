import { useState } from 'react'
import { openUrl } from '@tauri-apps/plugin-opener'
import { api, dollars } from '../api'
import type { PublikStatus } from '../types'

/**
 * The publik API pieces onboarding, Settings and the studio share. Before
 * setup the app says only what it knows (no price claims of its own); after
 * setup the cost sentence is the one POST /installs returned, verbatim.
 */

export const PUBLIK_PRE_SETUP =
  'Scoring runs on publik API: no key to paste. It is paid per use, in dollars, ' +
  'from a publik balance. Your balance starts at $0.00. Linking your publik ' +
  'account gives $0.05 of free use, once; a plan, a pack, or your own key takes ' +
  'it from there. Nothing is spent until you run a video.'

export const PUBLIK_DATA_PATH =
  "Your transcript slices and a few low-res frames go through publik's servers " +
  'to score your moments. Everything else stays on this machine, and you can ' +
  'switch to your own key at any time.'

export function currentBalance(p: PublikStatus | null): number | null {
  const b = p?.status?.balance_micros
  return typeof b === 'number' ? b : null
}

export function claimState(p: PublikStatus | null): 'anonymous' | 'claimed' {
  return p?.status?.claim_state ?? p?.claim_state ?? 'anonymous'
}

/** "publik API · $0.00" (nothing linked yet) / "publik API · $0.05 of free use left"
 *  (only when the whole balance is the starter) / "publik API · $3.12 left".
 *  balance_micros is plan + pack + starter, so a linked account with a plan and
 *  an unspent starter must not have its whole balance called free. */
export function balanceLine(p: PublikStatus | null): string {
  const b = currentBalance(p)
  if (b == null) return 'publik API ready'
  if (b === 0) return `publik API · ${dollars(b)}`
  const starter = p?.status?.starter_remaining_micros
  if (starter != null && starter > 0 && starter >= b) {
    return `publik API · ${dollars(b)} of free use left`
  }
  return `publik API · ${dollars(b)} left`
}

/** The one account link: link this computer while anonymous, add a plan once claimed. */
export function accountLink(p: PublikStatus | null): { label: string; url: string } | null {
  if (!p) return null
  if (claimState(p) === 'anonymous') {
    const url = p.claim_url ?? p.status?.top_up_url ?? null
    return url ? { label: 'Link this computer & pick a plan', url } : null
  }
  const url = p.add_credit_url ?? p.status?.top_up_url ?? null
  return url ? { label: 'Add a plan or pack', url } : null
}

/** After setup: balance line, the server's cost sentence, the account link. */
export function PublikReady({ publik }: { publik: PublikStatus }) {
  const link = accountLink(publik)
  return (
    <div className="publik-ready">
      <p className="publik-balance mono">{balanceLine(publik)}</p>
      {publik.disclosure?.cost && <p>{publik.disclosure.cost}</p>}
      {publik.disclosure?.data_path && <p className="publik-fine">{publik.disclosure.data_path}</p>}
      {link && (
        <button className="btn-secondary publik-link" onClick={() => openUrl(link.url)}>
          {link.label}
        </button>
      )}
    </div>
  )
}

/**
 * Founder rule (2026-09-28): no job starts until this computer's publik API
 * install is linked to a publik account, whichever brain scores (publik API,
 * my Gemini key, or Ollama). Mirrors publik::is_linked in the Rust shell,
 * which refuses run_job/resume_job on the same condition.
 */
export type LinkGate = 'checking' | 'off' | 'disconnected' | 'unlinked' | 'linked'

export function linkGate(p: PublikStatus | null): LinkGate {
  if (!p) return 'checking'
  if (!p.provisioned) return 'off'
  if (p.status?.disconnected) return 'disconnected'
  return claimState(p) === 'claimed' ? 'linked' : 'unlinked'
}

export const LINK_GATE_TITLE = 'Link your publik account to start'
export const LINK_GATE_BODY =
  'publikclip runs on a linked publik account. Linking gives $0.05 of free use, once.'
export const LINK_STILL_REQUIRED = 'A linked publik account is still required.'

/**
 * The one card that stands in for the run control until the computer is
 * linked: turn publik API on (or reconnect it), open the claim page in the
 * browser, then "I've linked it" asks the server (GET /wallet) and re-renders.
 */
export function LinkGateCard({
  publik,
  checking = false,
  inline = false,
  onChange
}: {
  publik: PublikStatus | null
  /** the server has not answered yet: say so instead of asking for a link */
  checking?: boolean
  /** inside another card (onboarding): no outer frame */
  inline?: boolean
  onChange: (p: PublikStatus) => void
}) {
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const gate = checking ? 'checking' : linkGate(publik)
  if (gate === 'linked') return null

  async function turnOn() {
    setBusy(true)
    setNote(null)
    try {
      onChange(await api.publikProvision())
    } catch (err) {
      setNote(String(err))
    } finally {
      setBusy(false)
    }
  }

  async function recheck() {
    setBusy(true)
    setNote(null)
    try {
      const p = await api.publikRefresh()
      onChange(p)
      if (linkGate(p) === 'unlinked') {
        setNote(
          p.refreshed
            ? "publik doesn't show this computer as linked yet. Finish linking in your browser, then try again."
            : "Couldn't reach publik API. Check your connection, then try again."
        )
      }
    } catch (err) {
      setNote(String(err))
    } finally {
      setBusy(false)
    }
  }

  const claim = gate === 'unlinked' ? accountLink(publik)?.url ?? null : null

  return (
    <section className={`publik-banner publik-gate ${inline ? 'publik-gate-inline' : ''}`}>
      <span className="led led-half" />
      <div>
        <strong>{LINK_GATE_TITLE}</strong>
        <p>{LINK_GATE_BODY}</p>
        {gate === 'checking' && <p className="publik-fine mono">Checking this computer's publik account…</p>}
        {gate === 'off' && <p className="publik-fine">{PUBLIK_DATA_PATH}</p>}
        {gate === 'disconnected' && (
          <p className="publik-fine">publik API is disconnected on this computer. Reconnect it first.</p>
        )}
        {gate !== 'checking' && (
          <div className="publik-actions">
            {gate === 'off' && (
              <button className="btn-primary" onClick={turnOn} disabled={busy}>
                {busy ? 'Setting up…' : 'Turn on publik API'}
              </button>
            )}
            {gate === 'disconnected' && (
              <button className="btn-primary" onClick={turnOn} disabled={busy}>
                {busy ? 'Reconnecting…' : 'Reconnect publik API'}
              </button>
            )}
            {gate === 'unlinked' && (
              <>
                <button className="btn-primary" onClick={() => claim && openUrl(claim)} disabled={!claim}>
                  Link this computer
                </button>
                <button className="btn-secondary" onClick={recheck} disabled={busy}>
                  {busy ? 'Checking…' : "I've linked it"}
                </button>
              </>
            )}
          </div>
        )}
        {note && <p className="ig-message mono">{note}</p>}
      </div>
    </section>
  )
}
