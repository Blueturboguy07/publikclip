import { invoke, convertFileSrc } from '@tauri-apps/api/core'
import type {
  AudioItem,
  AudioKeysStatus,
  JobResults,
  JobSummary,
  LibraryItem,
  LoopOverview,
  OnlineAudioResult,
  PublikStatus,
  SetupState,
  SyncSummary
} from './types'

export const api = {
  runJob: (source: string, llm: string, captions: string) =>
    invoke<void>('run_job', { source, llm, captions }),
  resumeJob: (jobId: string, llm?: string, captions?: string, camera?: string) =>
    invoke<void>('resume_job', { jobId, llm, captions, camera }),
  jobResults: (jobId: string) => invoke<JobResults>('job_results', { jobId }),
  listJobs: () => invoke<JobSummary[]>('list_job_dirs'),
  saveGeminiKey: (key: string) => invoke<boolean>('save_gemini_key', { key }),
  setupState: () => invoke<SetupState>('get_setup_state'),
  markOnboarded: () => invoke<void>('mark_onboarded'),
  checkOllama: () => invoke<{ running: boolean; models: string[] }>('check_ollama'),
  exportClip: (path: string, title?: string) =>
    invoke<string>('export_clip', { path, title }),
  igStatus: () => invoke<{ connected: boolean; username?: string }>('ig_status'),
  igSync: () => invoke<SyncSummary>('ig_tool', { args: ['sync'] }),
  igOverview: () => invoke<LoopOverview>('ig_tool', { args: ['overview'] }),
  igLink: (jobId: string, clip: number, mediaId: string, source: 'manual' | 'match_confirmed') =>
    invoke<{ ok: boolean }>('ig_tool', {
      args: ['link', jobId, String(clip), mediaId, '--source', source]
    }),
  igUnlink: (mediaId: string) =>
    invoke<{ ok: boolean }>('ig_tool', { args: ['unlink', mediaId] }),
  igReject: (mediaId: string, jobId: string, clip: number) =>
    invoke<{ ok: boolean }>('ig_tool', { args: ['reject', mediaId, jobId, String(clip)] }),
  publikStatus: () => invoke<PublikStatus>('publik_status'),
  publikProvision: () => invoke<PublikStatus>('publik_provision'),
  publikDisconnect: () => invoke<PublikStatus>('publik_disconnect'),
  fileUrl: (path: string) => convertFileSrc(path),

  /* ---------- music / sfx library ---------- */
  audioImport: (paths: string[], kind: 'auto' | 'music' | 'sfx') =>
    invoke<{ ok: boolean; items: LibraryItem[]; error?: string }>('audio_import', { paths, kind }),
  audioList: (kind?: 'music' | 'sfx', query?: string) =>
    invoke<{ ok: boolean; items: LibraryItem[] }>('audio_list', { kind, query }),
  audioRemove: (id: string) => invoke<{ ok: boolean }>('audio_remove', { id }),
  audioSearch: (
    query: string,
    source: 'freesound' | 'jamendo' | 'all',
    kind: 'music' | 'sfx',
    maxDuration: number | undefined,
    allowAttribution: boolean
  ) =>
    invoke<{ ok: boolean; results: OnlineAudioResult[]; error?: string }>('audio_search', {
      query, source, kind, maxDuration, allowAttribution
    }),
  audioFetch: (source: 'freesound' | 'jamendo', sourceId: string) =>
    invoke<{ ok: boolean; item?: LibraryItem; error?: string }>('audio_fetch', { source, sourceId }),
  audioSuggest: (jobId: string, clip: number) =>
    invoke<{ ok: boolean; audio: AudioItem[]; error?: string }>('audio_suggest', { jobId, clip }),
  saveFreesoundKey: (key: string) => invoke<boolean>('save_freesound_key', { key }),
  saveJamendoKey: (key: string) => invoke<boolean>('save_jamendo_key', { key }),
  audioKeysStatus: () => invoke<AudioKeysStatus>('audio_keys_status'),
  runAudioBootstrap: () => invoke<void>('run_audio_bootstrap')
}

/** micros → "$0.18" (publik API balances are integer micros of a dollar). */
export const dollars = (micros?: number | null) =>
  micros == null ? '' : '$' + (micros / 1_000_000).toFixed(2)
