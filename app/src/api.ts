import { invoke, convertFileSrc } from '@tauri-apps/api/core'
import type { JobResults, JobSummary, LoopOverview, PublikStatus, SetupState, SyncSummary } from './types'

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
  fileUrl: (path: string) => convertFileSrc(path)
}

/** micros → "$0.18" (publik API balances are integer micros of a dollar). */
export const dollars = (micros?: number | null) =>
  micros == null ? '' : '$' + (micros / 1_000_000).toFixed(2)
