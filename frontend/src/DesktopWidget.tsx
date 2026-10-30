import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { getCurrentWindow, PhysicalPosition, PhysicalSize } from '@tauri-apps/api/window'
import { listen } from '@tauri-apps/api/event'

const API = 'http://127.0.0.1:8787'
type Task = {
  id: number
  title: string
  priority: 'low' | 'medium' | 'high'
  status: string
  deadline?: string | null
  scheduled_at?: string | null
}
type CalendarEvent = { id: string; summary: string; start: string; end?: string }
type WidgetPreferences = {
  x: number | null
  y: number | null
  width: number
  height: number
  alwaysOnTop: boolean
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  const body = await response.json().catch(() => ({})) as T & { detail?: string }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`)
  return body
}

const dateKey = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
const taskDate = (value?: string | null) => value ? dateKey(new Date(value)) : ''
const rank: Record<Task['priority'], number> = { high: 0, medium: 1, low: 2 }

export default function DesktopWidget() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [events, setEvents] = useState<CalendarEvent[]>([])
  const [calendarConnected, setCalendarConnected] = useState(false)
  const [apiAvailable, setApiAvailable] = useState(true)
  const [error, setError] = useState('')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [now, setNow] = useState(new Date())
  const [alwaysOnTop, setAlwaysOnTop] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const health = await request<{ calendar_connected: boolean }>('/api/health')
      const taskResult = await request<{ tasks: Task[] }>('/api/tasks?include_completed=true')
      setTasks(taskResult.tasks)
      setCalendarConnected(health.calendar_connected)
      setApiAvailable(true)
      setError('')
      if (health.calendar_connected) {
        const currentDate = new Date()
        const start = new Date(currentDate.getFullYear(), currentDate.getMonth(), currentDate.getDate()).toISOString()
        const end = new Date(currentDate.getFullYear(), currentDate.getMonth(), currentDate.getDate() + 1).toISOString()
        try {
          const result = await request<{ events: CalendarEvent[] }>(
            `/api/events?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
          )
          setEvents(result.events)
        } catch (cause) {
          setEvents([])
          setError(cause instanceof Error
            ? `Calendar events could not be loaded: ${cause.message}`
            : 'Calendar events could not be loaded.')
        }
      } else {
        setEvents([])
      }
    } catch (cause) {
      setApiAvailable(false)
      setEvents([])
      setError(cause instanceof Error ? cause.message : 'Local planner service is unavailable.')
    }
  }, [])

  useEffect(() => {
    void refresh()
    const clock = window.setInterval(() => setNow(new Date()), 1000)
    const poll = window.setInterval(() => void refresh(), 30000)
    if (!isTauri()) return () => {
      window.clearInterval(clock)
      window.clearInterval(poll)
    }
    let unlistenData: (() => void) | undefined
    let unlistenMoved: (() => void) | undefined
    let unlistenResized: (() => void) | undefined
    let saveTimer: number | undefined
    let preferences: WidgetPreferences = { x: null, y: null, width: 360, height: 430, alwaysOnTop: false }
    const savePosition = () => {
      window.clearTimeout(saveTimer)
      saveTimer = window.setTimeout(async () => {
        const current = getCurrentWindow()
        try {
          const [position, size] = await Promise.all([current.outerPosition(), current.outerSize()])
          preferences = { ...preferences, x: position.x, y: position.y, width: size.width, height: size.height }
          await invoke('save_widget_preferences', { preferences })
        } catch {
          setError('Widget position could not be saved.')
        }
      }, 250)
    }
    void invoke<WidgetPreferences>('get_widget_preferences').then(async stored => {
      preferences = stored
      setAlwaysOnTop(stored.alwaysOnTop)
      const current = getCurrentWindow()
      if (stored.x !== null && stored.y !== null) {
        await current.setPosition(new PhysicalPosition(stored.x, stored.y))
      }
      await current.setSize(new PhysicalSize(stored.width, stored.height))
      await current.setAlwaysOnTop(stored.alwaysOnTop)
    }).catch(() => setError('Widget preferences could not be loaded.'))
    void getCurrentWindow().onMoved(({ payload }) => {
      preferences = { ...preferences, x: payload.x, y: payload.y }
      savePosition()
    }).then(unlisten => { unlistenMoved = unlisten })
    void getCurrentWindow().onResized(({ payload }) => {
      preferences = { ...preferences, width: payload.width, height: payload.height }
      savePosition()
    }).then(unlisten => { unlistenResized = unlisten })
    void listen('planner-data-changed', () => { void refresh() })
      .then(unlisten => { unlistenData = unlisten })
    return () => {
      window.clearInterval(clock)
      window.clearInterval(poll)
      window.clearTimeout(saveTimer)
      unlistenData?.()
      unlistenMoved?.()
      unlistenResized?.()
    }
  }, [refresh])

  const todayTasks = useMemo(() => tasks.filter(task => taskDate(task.scheduled_at) === dateKey(now)), [tasks, now])
  const completedToday = todayTasks.filter(task => task.status === 'completed').length
  const outstanding = tasks.filter(task => task.status !== 'completed').length
  const completed = tasks.filter(task => task.status === 'completed').length
  const progress = todayTasks.length ? Math.round(completedToday / todayTasks.length * 100) : 0
  const focusTask = tasks
    .filter(task => task.status !== 'completed'
      && (taskDate(task.scheduled_at) === dateKey(now) || task.deadline === dateKey(now)))
    .sort((a, b) => rank[a.priority] - rank[b.priority]
      || new Date(a.scheduled_at || 0).getTime() - new Date(b.scheduled_at || 0).getTime())[0]
  const nextEvent = events
    .filter(event => event.start.length === 10 || new Date(event.start).getTime() >= now.getTime())
    .sort((a, b) => new Date(a.start).getTime() - new Date(b.start).getTime())[0]

  async function captureTask(event: FormEvent) {
    event.preventDefault()
    if (!title.trim() || busy) return
    setBusy(true)
    try {
      await request('/api/tasks', { method: 'POST', body: JSON.stringify({ title: title.trim() }) })
      setTitle('')
      await refresh()
      if (isTauri()) await invoke('notify_planner_data_changed')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Task could not be captured.')
    } finally {
      setBusy(false)
    }
  }

  async function toggleAlwaysOnTop() {
    if (!isTauri()) return
    const next = !alwaysOnTop
    setAlwaysOnTop(next)
    try {
      await getCurrentWindow().setAlwaysOnTop(next)
      const current = getCurrentWindow()
      const [position, size] = await Promise.all([current.outerPosition(), current.outerSize()])
      await invoke('save_widget_preferences', {
        preferences: { x: position.x, y: position.y, width: size.width, height: size.height, alwaysOnTop: next },
      })
    } catch (cause) {
      setAlwaysOnTop(!next)
      setError(cause instanceof Error ? cause.message : 'Always-on-top could not be updated.')
    }
  }

  async function openMainApp() {
    if (!isTauri()) return
    await invoke('open_main_window')
    await getCurrentWindow().close()
  }

  return <main className="desktop-widget">
    <header className="widget-header" data-tauri-drag-region>
      <div data-tauri-drag-region><span className="widget-mark">P</span><div><span className="widget-eyebrow">PERSONAL AI PLANNER</span><strong>Today</strong></div></div>
      <div className="widget-window-actions">
        <button type="button" onClick={() => void toggleAlwaysOnTop()} aria-pressed={alwaysOnTop} title="Toggle always on top">{alwaysOnTop ? 'Pinned' : 'Pin'}</button>
        <button type="button" onClick={() => void refresh()} aria-label="Refresh widget">↻</button>
      </div>
    </header>
    <section className="widget-clock">
      <time>{now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>
      <span>{now.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' })}</span>
    </section>
    {!apiAvailable && <p className="widget-notice" role="alert">Planner service unavailable. Reconnecting…</p>}
    {error && apiAvailable && <p className="widget-notice" role="status">{error}</p>}
    <section className="widget-card widget-focus">
      <span className="widget-label">TODAY’S TOP PRIORITY</span>
      <strong>{focusTask?.title || 'No task scheduled for today'}</strong>
      {focusTask && <span className={`widget-priority ${focusTask.priority}`}>
        {focusTask.priority} priority{!focusTask.scheduled_at && focusTask.deadline === dateKey(now) ? ' · due today, not scheduled' : ''}
      </span>}
    </section>
    <section className="widget-card">
      <span className="widget-label">NEXT CALENDAR EVENT</span>
      {nextEvent
        ? <><strong>{nextEvent.summary}</strong><span>{nextEvent.start.length === 10 ? 'All day' : new Date(nextEvent.start).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</span></>
        : <span>{calendarConnected ? 'No upcoming event today' : 'Google Calendar disconnected'}</span>}
    </section>
    <section className="widget-progress">
      <div><span className="widget-label">TODAY’S SCHEDULED TASKS</span><strong>{completedToday} / {todayTasks.length} complete</strong></div>
      <div className="widget-progress-track" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${progress}%` }} /></div>
      <p>{completed} completed · {outstanding} outstanding overall</p>
    </section>
    <form className="widget-capture" onSubmit={captureTask}>
      <input aria-label="Quick task capture" placeholder="Capture a task…" value={title} onChange={event => setTitle(event.target.value)} />
      <button type="submit" disabled={!title.trim() || busy}>{busy ? '…' : '＋'}</button>
    </form>
    <button type="button" className="widget-open-app" onClick={() => void openMainApp()}>Open Personal AI Planner</button>
  </main>
}
