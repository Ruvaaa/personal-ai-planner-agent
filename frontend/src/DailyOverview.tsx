import { useEffect, useState } from 'react'

type Overview = Record<string, any>
const API = 'http://127.0.0.1:8787'
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(API + path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  const result = await response.json().catch(() => ({})) as T & { detail?: string }
  if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`)
  return result
}
const time = (value: string) => value?.includes('T') ? new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : value

export default function DailyOverview({ version, onPlan }: { version: number; onPlan: () => void }) {
  const [data, setData] = useState<Overview | null>(null)
  const [briefing, setBriefing] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let mounted = true
    void request<Overview>('/api/overview').then(result => { if (mounted) setData(result) })
      .catch(cause => { if (mounted) setError(cause instanceof Error ? cause.message : 'Overview unavailable.') })
    return () => { mounted = false }
  }, [version])

  async function createBriefing() {
    setBusy(true)
    setError('')
    try {
      const result = await request<{ briefing: string }>('/api/overview/briefing', {
        method: 'POST', body: JSON.stringify({}),
      })
      setBriefing(result.briefing)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Briefing could not be generated.')
    } finally {
      setBusy(false)
    }
  }

  if (error && !data) return <div className="notice">{error}</div>
  if (!data) return <div className="empty">Loading today’s planner data…</div>

  return <section className="daily-overview">
    {error && <div className="notice">{error}</div>}
    <div className="overview-date-row"><p>{new Date(`${data.date}T12:00:00`).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })}</p><button className="secondary" onClick={onPlan}>Plan my day</button></div>
    <div className="planner-stats overview-stats">
      <article className="metric"><span className="card-label">DUE TODAY</span><strong>{data.due_today_tasks.length}</strong><p>outstanding tasks</p></article>
      <article className="metric"><span className="card-label">OVERDUE</span><strong>{data.overdue_tasks.length}</strong><p>need a deadline review</p></article>
      <article className="metric"><span className="card-label">HIGH PRIORITY</span><strong>{data.high_priority_tasks.length}</strong><p>still outstanding</p></article>
      <article className="metric"><span className="card-label">AVAILABLE</span><strong>{data.available_minutes ?? '—'}</strong><p>{data.available_minutes === null ? 'calendar unavailable' : 'working minutes'}</p></article>
    </div>
    {!!data.reminders.length && <section className="reminder-strip" aria-label="Planner reminders"><span className="card-label">REMINDERS</span>{data.reminders.slice(0, 5).map((item: Overview) => <p key={`${item.kind}-${item.task_id}`}>{item.kind === 'overdue' ? 'Overdue' : 'Due today'}: {item.title}</p>)}</section>}
    <div className="daily-overview-grid">
      <section className="panel">
        <div className="panel-heading"><div><span className="card-label">TODAY</span><h2>Tasks and priorities</h2></div></div>
        <div className="daily-task-group"><strong>Due today</strong>{data.due_today_tasks.length ? data.due_today_tasks.map((task: Overview) => <p key={task.id}><span className={`priority-dot ${task.priority}`} />{task.title} · {task.duration_minutes || 60} min</p>) : <p className="empty">No tasks due today.</p>}</div>
        {!!data.overdue_tasks.length && <div className="daily-task-group"><strong>Overdue</strong>{data.overdue_tasks.slice(0, 5).map((task: Overview) => <p key={task.id}><span className="priority-dot high" />{task.title} · due {task.deadline}</p>)}</div>}
        {!!data.high_priority_tasks.length && <div className="daily-task-group"><strong>High priority</strong>{data.high_priority_tasks.slice(0, 5).map((task: Overview) => <p key={task.id}>{task.title}{task.deadline ? ` · due ${task.deadline}` : ''}</p>)}</div>}
        <div className="recommended-action"><span className="card-label">NEXT RECOMMENDED ACTION</span><strong>{data.next_recommended_action}</strong></div>
      </section>
      <section className="panel">
        <div className="panel-heading"><div><span className="card-label">CONFIRMED</span><h2>Calendar today</h2></div><span>{data.calendar_events.length} events</span></div>
        {!data.calendar_available && <p className="notice">Calendar is unavailable; free time cannot be estimated.</p>}
        {data.calendar_events.length ? data.calendar_events.map((event: Overview) => <article className="event" key={event.id}><time>{time(event.start)}{event.end && event.end.includes('T') ? ` – ${time(event.end)}` : ''}</time><h3>{event.summary}</h3></article>) : <div className="empty">No calendar events recorded for today.</div>}
        {data.daily_proposal && <div className="proposal-summary"><span className="card-label">PROPOSED TASK BLOCKS</span><p>{data.daily_proposal.scheduled_minutes} minutes suggested · {data.daily_proposal.status}. These are not confirmed calendar events.</p><button className="secondary" onClick={onPlan}>Review proposal</button></div>}
      </section>
    </div>
    <section className="panel daily-email-summary"><div><span className="card-label">EMAIL SNAPSHOT</span><p>{data.email?.connected ? `${data.email.unread_count ?? '—'} unread in the locally synced index · last sync ${data.email.last_sync ? new Date(data.email.last_sync).toLocaleString() : 'not synced'}` : 'Gmail is not connected.'}</p>{!!data.email?.important?.length && <p>Important: {data.email.important.map((email: Overview) => email.subject).join(' · ')}</p>}{!!data.email?.reply_needed?.length && <p>Reply needed: {data.email.reply_needed.map((email: Overview) => email.subject).join(' · ')}</p>}</div><button className="secondary" onClick={() => void createBriefing()} disabled={busy}>{busy ? 'Preparing…' : 'Generate daily briefing'}</button></section>
    {briefing && <section className="panel briefing-panel"><span className="card-label">GEMINI BRIEFING · VERIFIED LOCAL RECORDS</span><p>{briefing}</p></section>}
  </section>
}
