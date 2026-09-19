import { openUrl } from '@tauri-apps/plugin-opener'
import { dollars } from '../api'
import type { PublikStatus } from '../types'

/**
 * The publik API card, in one place because it has to say the same thing in
 * onboarding and in Settings.
 *
 * publik's contract, section 12, says what a card that has just provisioned
 * must show, in this order: the balance line, then one sentence saying WHY it
 * costs anything at all, then one primary button that opens the claim link.
 * Money nobody explained is money people assume was taken from them.
 *
 * THE JUSTIFICATION SENTENCE is publik's own (lib/publik-api/why-it-costs.ts),
 * with one clause changed on purpose. The site's sentence says "at half the
 * provider's list price", which is true of publik's chat line and NOT of this
 * one: publikclip scores through publik's Gemini line, which the contract
 * prices at list with no spread and tells us to describe as "at cost", never
 * as a discount. Saying "half" here would be a discount nobody is getting.
 */
export const WHY_IT_COSTS =
  'The AI model behind publikclip is run by a provider that charges per use; publik passes that ' +
  "on at the provider's list price with no markup, nothing is charged behind your back, and you " +
  'can see every call on your dashboard.'

export function balanceLine(status: PublikStatus): string {
  const s = status.status
  const balance = s.balance_micros
  const starter = s.starter_remaining_micros
  if (balance == null) return 'Ready'
  if (s.claim_state === 'claimed') {
    const week =
      s.week_budget_micros != null && s.week_budget_micros !== 'none'
        ? ` · this week ${dollars(s.week_used_micros)} of ${dollars(s.week_budget_micros as number)}`
        : ''
    return `${dollars(balance)} left${week}`
  }
  if (starter != null && starter > 0) return `${dollars(balance)} left of ${dollars(starter)} free`
  return `${dollars(balance)} left`
}

interface Props {
  status: PublikStatus | null
  busy?: boolean
  note?: string | null
  /** Shown before there is anything to link: the consent tap that mints. */
  onConnect?: () => void
  onDisconnect?: () => void
  /** Onboarding renders the long version; Settings renders the compact row. */
  variant: 'onboarding' | 'settings'
}

/**
 * One button, and it is the same button everywhere: "Link this computer & pick
 * a plan" while the install is anonymous, "Add a plan or pack" once it is
 * linked. Two links is how a person ends up on the wrong one.
 */
export function PublikActions({ status }: { status: PublikStatus }) {
  const s = status.status
  const claimed = s.claim_state === 'claimed'
  const link = claimed
    ? s.add_credit_url ?? 'https://publikhq.com/dashboard/api/add'
    : s.claim_url ?? status.claim_url ?? 'https://publikhq.com/dashboard/api'
  return (
    <button className="btn-primary" onClick={() => void openUrl(link)}>
      {claimed ? 'Add a plan or pack' : 'Link this computer & pick a plan'}
    </button>
  )
}

export default function PublikCard({ status, busy, note, onConnect, onDisconnect, variant }: Props) {
  const provisioned = status?.provisioned ?? false
  const s = status?.status ?? {}
  const unavailable = status != null && !status.available

  const state = !provisioned
    ? 'Not set up'
    : s.disconnected
      ? 'Disconnected'
      : s.needs_credit
        ? 'Needs credit'
        : 'Ready'

  return (
    <div className={`ob-card publik-card ${provisioned ? 'done' : ''}`}>
      <h3>
        publik API{' '}
        {variant === 'onboarding' && !provisioned && (
          <span className="chip chip-amber">preselected</span>
        )}
      </h3>

      {/* (a) the balance line — the real number, not a promise about one */}
      <p className="ig-message mono">
        {provisioned ? `${state} · ${balanceLine(status!)}` : unavailable ? 'Not available in this build' : state}
      </p>

      {/* (b) the one sentence that says why any of this costs money */}
      <p>{WHY_IT_COSTS}</p>

      {variant === 'onboarding' && (
        <p>
          Your transcript slices and a few low-res frames go through publik&apos;s servers to a
          shared model account. publik never trains on them and does not store them. Everything
          else stays on this machine, and you can switch to your own key at any time.
        </p>
      )}

      {/* (c) the primary button */}
      {provisioned ? (
        <div className="ob-key-row">
          <PublikActions status={status!} />
          {onDisconnect && (
            <button className="btn-ghost" onClick={onDisconnect}>
              Disconnect publik API
            </button>
          )}
        </div>
      ) : (
        onConnect && (
          <div className="ob-key-row">
            <button className="btn-secondary" onClick={onConnect} disabled={busy || unavailable}>
              {busy ? 'Setting up…' : 'Continue with publik API'}
            </button>
          </div>
        )
      )}

      {variant === 'onboarding' && !provisioned && (
        <p className="ob-fine">
          Tapping that is what sets this computer up — nothing is sent to publik before you do.
        </p>
      )}
      {note && <p className="ig-message mono">{note}</p>}
    </div>
  )
}
