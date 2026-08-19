import { useCallback, useEffect, useRef, useState } from 'react'
import { listen } from '@tauri-apps/api/event'
import { api } from './api'
import type { JobResults, JobSummary, PipelineEvent, SetupState } from './types'
import Onboarding from './components/Onboarding'
import Studio from './components/Studio'
import Review from './components/Review'
import Loop from './components/Loop'
import './styles.css'

type View = 'boot' | 'onboarding' | 'studio' | 'review' | 'loop'

export default function App() {
  const [view, setView] = useState<View>('boot')
  const [setup, setSetup] = useState<SetupState | null>(null)
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [activeJob, setActiveJob] = useState<string | null>(null)
  const [results, setResults] = useState<JobResults | null>(null)
  const [stages, setStages] = useState<Record<string, { fraction: number; message: string }>>({})
  const [running, setRunning] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const unlistenRef = useRef<(() => void) | null>(null)
  const activeJobRef = useRef<string | null>(null)
  activeJobRef.current = activeJob

  const refreshJobs = useCallback(() => {
    api.listJobs().then(setJobs).catch(() => setJobs([]))
  }, [])

  useEffect(() => {
    api.setupState().then((s) => {
      setSetup(s)
      setView(s.onboarded ? 'studio' : 'onboarding')
    })
    refreshJobs()
  }, [refreshJobs])

  // Instagram loop: opportunistic sync on launch + hourly while open
  // (decision #12 — no background process, the app's own uptime is the
  // schedule). Fire-and-forget; the Loop screen re-reads on entry.
  useEffect(() => {
    const kick = () => {
      api
        .igStatus()
        .then((s) => (s.connected ? api.igSync() : null))
        .catch(() => null)
    }
    kick()
    const timer = window.setInterval(kick, 60 * 60 * 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    let disposed = false
    listen<PipelineEvent>('pipeline-event', ({ payload }) => {
      if (payload.event === 'job' && payload.job_id) {
        setActiveJob(payload.job_id)
        setResults(null)
      } else if (payload.event === 'progress' && payload.stage) {
        setStages((prev) => ({
          ...prev,
          [payload.stage!]: {
            fraction: payload.fraction ?? -1,
            message: payload.message ?? ''
          }
        }))
      } else if (payload.event === 'result') {
        setRunning(false)
        refreshJobs()
        if (payload.ok && activeJobRef.current) {
          api.jobResults(activeJobRef.current).then((r) => {
            setResults(r)
            setView('review')
          })
        } else if (!payload.ok) {
          setRunError(String(payload.error ?? 'Pipeline failed'))
        }
      } else if (payload.event === 'exited') {
        setRunning(false)
        // The sidecar died without reporting a result. The shell now
        // includes the captured stderr tail (`detail`); fall back to the
        // legacy generic message when it's absent (older builds).
        const detail = payload.detail && String(payload.detail).trim()
          ? String(payload.detail)
          : null
        setRunError(
          detail
            ? 'The pipeline exited unexpectedly:\n\n' + detail + '\n\nResume the job to continue from its last checkpoint.'
            : 'The pipeline exited unexpectedly. Resume the job to continue from its last checkpoint.'
        )
      }
    }).then((un) => {
      if (disposed) un()
      else unlistenRef.current = un
    })
    return () => {
      disposed = true
      unlistenRef.current?.()
    }
  }, [refreshJobs])

  const startRun = useCallback(
    async (source: string, llm: string, captions: string) => {
      setRunning(true)
      setRunError(null)
      setStages({})
      setResults(null)
      setActiveJob(null)
      await api.runJob(source, llm, captions)
    },
    []
  )

  const openJob = useCallback(async (jobId: string) => {
    const r = await api.jobResults(jobId)
    setActiveJob(jobId)
    setResults(r)
    if (r.render?.outputs?.length) setView('review')
  }, [])

  if (view === 'boot') return <div className="boot" />

  if (view === 'onboarding' && setup) {
    return (
      <Onboarding
        onDone={() => {
          api.markOnboarded()
          setSetup({ ...setup, onboarded: true })
          setView('studio')
        }}
      />
    )
  }

  if (view === 'loop') {
    return <Loop onBack={() => setView('studio')} />
  }

  if (view === 'review' && results) {
    return (
      <Review
        results={results}
        onBack={() => {
          setView('studio')
          refreshJobs()
        }}
        onRestyle={(captions, camera) => {
          setRunning(true)
          setRunError(null)
          setStages({})
          setActiveJob(results.job_id)
          setView('studio')
          api.resumeJob(results.job_id, undefined, captions, camera)
        }}
      />
    )
  }

  return (
    <Studio
      jobs={jobs}
      running={running}
      stages={stages}
      error={runError}
      onRun={startRun}
      onOpenLoop={() => setView('loop')}
      onOpenJob={openJob}
      onResume={(id, llm) => {
        setRunning(true)
        setRunError(null)
        setStages({})
        setActiveJob(id)
        api.resumeJob(id, llm)
      }}
    />
  )
}
