import { openUrl } from '@tauri-apps/plugin-opener'
import { dollars } from '../api'
import type { PublikStatus } from '../types'

/**
 * The publik API pieces onboarding, Settings and the studio share. Before
 * setup the app says only what it knows (no price claims of its own); after
 * setup the cost sentence is the one POST /installs returned, verbatim.
 */

export const PUBLIK_PRE_SETUP =
  'Scoring runs on publik API: no account, no key to paste. It is paid per use, ' +
  'in dollars, from a publik balance. Setting it up shows your starting balance ' +
  'and exactly how the price works — nothing is spent until you run a video.'

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

/** "publik API · $0.25 of free starter usage left" / "publik API · $3.12 left" */
export function balanceLine(p: PublikStatus | null): string {
  const b = currentBalance(p)
  if (b == null) return 'publik API ready'
  const starter = p?.status?.starter_remaining_micros
  if (claimState(p) === 'anonymous' && (starter == null || starter > 0)) {
    return `publik API · ${dollars(b)} of free starter usage left`
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
