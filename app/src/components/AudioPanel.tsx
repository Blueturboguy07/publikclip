import { useEffect, useState } from 'react'
import { open } from '@tauri-apps/plugin-dialog'
import { listen } from '@tauri-apps/api/event'
import { api } from '../api'
import type { AudioKeysStatus, LibraryItem, OnlineAudioResult } from '../types'

/**
 * Music & SFX library — a side panel over the clip editor with two tabs:
 * "Library" (local files, search/filter, drag onto the audio track) and
 * "Online" (CC-licensed Freesound/Jamendo search, "add to library").
 * Never places anything on the track itself — that's drag-and-drop (Library)
 * or a separate "suggest audio" pass in ClipEditor.
 */

interface Props {
  onClose: () => void
  onLibraryChanged: () => void
}

type Tab = 'library' | 'online'

function fmtDur(s: number): string {
  const m = Math.floor(s / 60)
  const sec = Math.round(s % 60)
  return m > 0 ? `${m}:${String(sec).padStart(2, '0')}` : `${sec}s`
}

function licenceClass(licence: string | null): string {
  if (!licence) return ''
  if (licence === 'CC0-1.0') return 'lib-licence-cc0'
  if (licence.startsWith('CC-BY')) return 'lib-licence-ccby'
  return ''
}

export default function AudioPanel({ onClose, onLibraryChanged }: Props) {
  const [tab, setTab] = useState<Tab>('library')
  const [playingKey, setPlayingKey] = useState<string | null>(null)

  // ---- Library tab --------------------------------------------------------
  const [items, setItems] = useState<LibraryItem[]>([])
  const [kindFilter, setKindFilter] = useState<'' | 'music' | 'sfx'>('')
  const [query, setQuery] = useState('')
  const [importing, setImporting] = useState(false)
  const [libraryEmpty, setLibraryEmpty] = useState<boolean | null>(null) // unfiltered — for the starter-pack prompt
  const [bootstrapping, setBootstrapping] = useState(false)
  const [bootstrapMsg, setBootstrapMsg] = useState('')

  function reloadLibrary() {
    api.audioList(kindFilter || undefined, query || undefined).then((r) => setItems(r.items))
    api.audioList().then((r) => setLibraryEmpty(r.items.length === 0))
  }
  useEffect(reloadLibrary, [kindFilter, query])

  useEffect(() => {
    let un: (() => void) | null = null
    listen<{ event: string; stage?: string; fraction?: number; message?: string; ok?: boolean; error?: string }>(
      'pipeline-event',
      ({ payload }) => {
        if (payload.stage !== 'bootstrap') return
        if (payload.event === 'progress') setBootstrapMsg(payload.message ?? '')
        if (payload.event === 'result') {
          setBootstrapping(false)
          setBootstrapMsg(payload.ok ? 'done ✓' : (payload.error ?? 'failed'))
          reloadLibrary()
          onLibraryChanged()
        }
      }
    ).then((u) => (un = u))
    return () => un?.()
  }, [])

  async function doBootstrap() {
    setBootstrapping(true)
    setBootstrapMsg('starting…')
    await api.runAudioBootstrap()
  }

  async function doImportFiles() {
    const picked = await open({ multiple: true, directory: false })
    if (!picked) return
    setImporting(true)
    try {
      await api.audioImport(Array.isArray(picked) ? picked : [picked], 'auto')
      reloadLibrary()
      onLibraryChanged()
    } finally {
      setImporting(false)
    }
  }

  async function doImportFolder() {
    const picked = await open({ multiple: false, directory: true })
    if (!picked) return
    setImporting(true)
    try {
      await api.audioImport([picked as string], 'auto')
      reloadLibrary()
      onLibraryChanged()
    } finally {
      setImporting(false)
    }
  }

  function removeItem(id: string) {
    api.audioRemove(id).then(() => {
      reloadLibrary()
      onLibraryChanged()
    })
  }

  // ---- Online tab -----------------------------------------------------------
  const [source, setSource] = useState<'all' | 'freesound' | 'jamendo'>('all')
  const [onlineKind, setOnlineKind] = useState<'music' | 'sfx'>('music')
  const [onlineQuery, setOnlineQuery] = useState('')
  const [allowAttribution, setAllowAttribution] = useState(false)
  const [results, setResults] = useState<OnlineAudioResult[]>([])
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [fetchingKey, setFetchingKey] = useState<string | null>(null)
  const [keys, setKeys] = useState<AudioKeysStatus | null>(null)
  const [freesoundKeyInput, setFreesoundKeyInput] = useState('')
  const [jamendoKeyInput, setJamendoKeyInput] = useState('')

  useEffect(() => {
    api.audioKeysStatus().then(setKeys)
  }, [])

  async function doSearch() {
    if (!onlineQuery.trim()) return
    setSearching(true)
    setSearchError(null)
    try {
      const res = await api.audioSearch(onlineQuery, source, onlineKind, undefined, allowAttribution)
      if (res.ok) setResults(res.results)
      else {
        setResults([])
        setSearchError(res.error ?? 'search failed')
      }
    } catch (e) {
      setResults([])
      setSearchError(String(e))
    } finally {
      setSearching(false)
    }
  }

  async function addToLibrary(r: OnlineAudioResult) {
    const key = `${r.source}:${r.source_id}`
    setFetchingKey(key)
    try {
      const res = await api.audioFetch(r.source, r.source_id)
      if (res.ok) {
        reloadLibrary()
        onLibraryChanged()
      } else setSearchError(res.error ?? 'fetch failed')
    } finally {
      setFetchingKey(null)
    }
  }

  async function saveKey(which: 'freesound' | 'jamendo') {
    if (which === 'freesound') {
      await api.saveFreesoundKey(freesoundKeyInput)
      setFreesoundKeyInput('')
    } else {
      await api.saveJamendoKey(jamendoKeyInput)
      setJamendoKeyInput('')
    }
    api.audioKeysStatus().then(setKeys)
  }

  const signupUrl = source === 'jamendo' ? 'https://devportal.jamendo.com/' : 'https://freesound.org/apiv2/apply/'
  const missingKey =
    keys !== null &&
    ((source === 'freesound' && !keys.has_freesound_key) ||
      (source === 'jamendo' && !keys.has_jamendo_key) ||
      (source === 'all' && !keys.has_freesound_key && !keys.has_jamendo_key))

  return (
    <div className="lib-panel" onClick={(e) => e.stopPropagation()}>
      <div className="lib-head">
        <span className="lib-title">MUSIC &amp; SFX</span>
        <button className="btn-ghost" onClick={onClose}>close ✕</button>
      </div>
      <div className="lib-tabs">
        <button className={`lib-tab ${tab === 'library' ? 'lib-tab-on' : ''}`} onClick={() => setTab('library')}>
          LIBRARY
        </button>
        <button className={`lib-tab ${tab === 'online' ? 'lib-tab-on' : ''}`} onClick={() => setTab('online')}>
          ONLINE (CC)
        </button>
      </div>
      <div className="lib-body">
        {tab === 'library' && (
          <>
            <div className="lib-row">
              <input
                type="search"
                placeholder="search tags / name…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <div className="lib-row">
              <button className={`opt ${kindFilter === '' ? 'opt-on' : ''}`} onClick={() => setKindFilter('')}>all</button>
              <button className={`opt ${kindFilter === 'music' ? 'opt-on' : ''}`} onClick={() => setKindFilter('music')}>music</button>
              <button className={`opt ${kindFilter === 'sfx' ? 'opt-on' : ''}`} onClick={() => setKindFilter('sfx')}>sfx</button>
            </div>
            <div className="lib-row">
              <button className="btn-secondary" onClick={doImportFiles} disabled={importing}>
                {importing ? 'importing…' : '＋ files'}
              </button>
              <button className="btn-secondary" onClick={doImportFolder} disabled={importing}>
                ＋ folder
              </button>
            </div>
            {libraryEmpty === true && (
              <div className="lib-key-notice">
                <span>Your library is empty — get a curated CC0 starter pack (music + sfx) to try suggestions right away.</span>
                <button className="btn-secondary" onClick={doBootstrap} disabled={bootstrapping}>
                  {bootstrapping ? bootstrapMsg || 'fetching…' : '★ get starter pack'}
                </button>
              </div>
            )}
            {libraryEmpty === false && items.length === 0 && (
              <p className="lib-empty">no items match this filter</p>
            )}
            {items.map((it) => {
              const key = `lib:${it.id}`
              return (
                <div
                  key={it.id}
                  className="lib-item"
                  draggable
                  title="drag onto the audio track"
                  onDragStart={(e) => {
                    e.dataTransfer.setData('application/x-publikclip-audio', JSON.stringify(it))
                    e.dataTransfer.effectAllowed = 'copy'
                  }}
                >
                  <button
                    className={`lib-play ${playingKey === key ? 'lib-play-on' : ''}`}
                    onClick={() => setPlayingKey(playingKey === key ? null : key)}
                  >
                    {playingKey === key ? '❚❚' : '▶'}
                  </button>
                  {playingKey === key && (
                    <audio
                      autoPlay
                      src={api.fileUrl(it.path)}
                      onEnded={() => setPlayingKey(null)}
                      style={{ display: 'none' }}
                    />
                  )}
                  <div className="lib-item-body">
                    <span className="lib-item-name">{it.name}</span>
                    <span className="lib-item-meta">
                      {it.kind} · {fmtDur(it.duration)}{it.bpm ? ` · ${Math.round(it.bpm)}bpm` : ''}
                    </span>
                    {it.tags.length > 0 && <span className="lib-item-tags">{it.tags.slice(0, 4).join(', ')}</span>}
                  </div>
                  {it.licence && <span className={`lib-licence ${licenceClass(it.licence)}`}>{it.licence}</span>}
                  <button className="lib-remove" onClick={() => removeItem(it.id)} title="remove from library">✕</button>
                </div>
              )
            })}
          </>
        )}

        {tab === 'online' && (
          <>
            <div className="lib-row">
              <button className={`opt ${source === 'all' ? 'opt-on' : ''}`} onClick={() => setSource('all')}>all sources</button>
              <button className={`opt ${source === 'freesound' ? 'opt-on' : ''}`} onClick={() => setSource('freesound')}>freesound</button>
              <button className={`opt ${source === 'jamendo' ? 'opt-on' : ''}`} onClick={() => setSource('jamendo')}>jamendo</button>
            </div>
            <div className="lib-row">
              <button className={`opt ${onlineKind === 'music' ? 'opt-on' : ''}`} onClick={() => setOnlineKind('music')}>music</button>
              <button className={`opt ${onlineKind === 'sfx' ? 'opt-on' : ''}`} onClick={() => setOnlineKind('sfx')}>sfx</button>
            </div>
            <div className="lib-row">
              <input
                type="text"
                placeholder="search e.g. lo-fi hip hop, whoosh…"
                value={onlineQuery}
                onChange={(e) => setOnlineQuery(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && doSearch()}
              />
              <button className="btn-secondary" onClick={doSearch} disabled={searching || !onlineQuery.trim()}>
                {searching ? '…' : 'search'}
              </button>
            </div>
            <label className="lib-toggle">
              <input
                type="checkbox"
                checked={allowAttribution}
                onChange={(e) => setAllowAttribution(e.target.checked)}
              />
              allow attribution (CC-BY) — attribution recorded to credits.txt
            </label>

            {missingKey && (
              <div className="lib-key-notice">
                <span>
                  {source === 'jamendo' ? 'Jamendo' : 'Freesound'} needs a free API key —{' '}
                  <a href={signupUrl} target="_blank" rel="noreferrer">sign up</a>, then paste it here.
                </span>
                <div className="lib-row">
                  <input
                    type="password"
                    placeholder={source === 'jamendo' ? 'jamendo client id' : 'freesound API key'}
                    value={source === 'jamendo' ? jamendoKeyInput : freesoundKeyInput}
                    onChange={(e) =>
                      source === 'jamendo' ? setJamendoKeyInput(e.target.value) : setFreesoundKeyInput(e.target.value)
                    }
                  />
                  <button
                    className="btn-secondary"
                    onClick={() => saveKey(source === 'jamendo' ? 'jamendo' : 'freesound')}
                  >
                    save
                  </button>
                </div>
              </div>
            )}

            {searchError && <p className="editor-err">{searchError}</p>}
            {!searching && results.length === 0 && !searchError && (
              <p className="lib-empty">
                search {source === 'all' ? 'Freesound + Jamendo' : source} for CC-licensed {onlineKind}
              </p>
            )}
            {results.map((r) => {
              const key = `${r.source}:${r.source_id}`
              return (
                <div key={key} className="lib-item">
                  <button
                    className={`lib-play ${playingKey === key ? 'lib-play-on' : ''}`}
                    onClick={() => setPlayingKey(playingKey === key ? null : key)}
                  >
                    {playingKey === key ? '❚❚' : '▶'}
                  </button>
                  {playingKey === key && (
                    <audio autoPlay src={r.download_url} onEnded={() => setPlayingKey(null)} style={{ display: 'none' }} />
                  )}
                  <div className="lib-item-body">
                    <span className="lib-item-name">{r.name}</span>
                    <span className="lib-item-meta">{r.source} · {fmtDur(r.duration)}</span>
                    <span className="lib-item-tags">{r.attribution}</span>
                  </div>
                  <span className={`lib-licence ${licenceClass(r.licence)}`}>{r.licence}</span>
                  <button
                    className="btn-secondary lib-add-btn"
                    onClick={() => addToLibrary(r)}
                    disabled={fetchingKey === key}
                  >
                    {fetchingKey === key ? '…' : '＋ add'}
                  </button>
                </div>
              )
            })}
          </>
        )}
      </div>
    </div>
  )
}
