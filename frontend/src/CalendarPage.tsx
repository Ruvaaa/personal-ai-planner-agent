import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { formatCatDateTime } from './dateTime'

const API = 'http://127.0.0.1:8787'
type Task = {
  id: number
  title: string
  description?: string
  scheduled_at?: string | null
  deadline?: string | null
  priority: 'low' | 'medium' | 'high'
  duration_minutes: number
  status: string
  completed_at?: string | null
}
type CalendarEvent = {
  id: string
  summary: string
  start: string
  end?: string
  description?: string
  location?: string
  htmlLink?: string
}
type CalendarItem =
  | { kind: 'task'; value: Task }
  | { kind: 'event'; value: CalendarEvent }
type View = 'month' | 'week' | 'day'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  const body = await response.json().catch(() => ({})) as T & { detail?: string }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`)
  return body
}

const CAT_TIME_ZONE = 'Africa/Harare'
const catParts = (date: Date) => new Intl.DateTimeFormat('en-CA', {
  timeZone: CAT_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
}).formatToParts(date).reduce<Record<string, string>>((parts, part) => {
  if (part.type !== 'literal') parts[part.type] = part.value
  return parts
}, {})
const dateKey = (date: Date) => {
  const { year, month, day } = catParts(date)
  return `${year}-${month}-${day}`
}
const dateObject = (year: number, month: number, day: number) => new Date(Date.UTC(year, month - 1, day, 12))
const valueDateKey = (value: string) => value.length === 10 ? value : dateKey(new Date(value))
const addDays = (date: Date, count: number) => {
  const [year, month, day] = dateKey(date).split('-').map(Number)
  return dateObject(year, month, day + count)
}
const beginningOfWeek = (date: Date) => addDays(date, -new Date(`${dateKey(date)}T12:00:00Z`).getUTCDay())
const atMidnight = (date: Date) => {
  const [year, month, day] = dateKey(date).split('-').map(Number)
  return dateObject(year, month, day)
}
const catDate = (date: Date, options: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat([], { ...options, timeZone: CAT_TIME_ZONE }).format(date)
const inputDateTime = (value?: string | null) => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: CAT_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date).reduce<Record<string, string>>((result, part) => {
    if (part.type !== 'literal') result[part.type] = part.value
    return result
  }, {})
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`
}
const displayDateTime = (value?: string | null) => value
  ? formatCatDateTime(value)
  : 'Not scheduled'
const displayEventTime = (value: string) => value.length === 10
  ? 'All day'
  : `${new Intl.DateTimeFormat([], {
    timeZone: CAT_TIME_ZONE,
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))} CAT`
const displayEventRange = (event: CalendarEvent) => event.start.length === 10
  ? `All day · ${catDate(new Date(`${event.start}T12:00:00Z`), { dateStyle: 'medium' })}`
  : `${displayEventTime(event.start)}${event.end && event.end.length !== 10 ? ` – ${displayEventTime(event.end)}` : ''}`

export default function CalendarPage({ onChanged }: { onChanged: () => void }) {
  const [selectedDate, setSelectedDate] = useState(() => atMidnight(new Date()))
  const [view, setView] = useState<View>('month')
  const [tasks, setTasks] = useState<Task[]>([])
  const [events, setEvents] = useState<CalendarEvent[]>([])
  const [calendarConnected, setCalendarConnected] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selectedItem, setSelectedItem] = useState<CalendarItem | null>(null)
  const [draft, setDraft] = useState({ title: '', description: '', scheduled_at: '', deadline: '', priority: 'medium', duration_minutes: 60 })
  const [busy, setBusy] = useState(false)

  const visibleDates = useMemo(() => {
    if (view === 'day') return [atMidnight(selectedDate)]
    if (view === 'week') {
      const start = beginningOfWeek(selectedDate)
      return Array.from({ length: 7 }, (_, index) => addDays(start, index))
    }
    const [year, month] = dateKey(selectedDate).split('-').map(Number)
    const first = dateObject(year, month, 1)
    const start = beginningOfWeek(first)
    return Array.from({ length: 42 }, (_, index) => addDays(start, index))
  }, [selectedDate, view])

  const range = useMemo(() => {
    const first = visibleDates[0]
    const exclusiveEnd = addDays(visibleDates[visibleDates.length - 1], 1)
    return {
      start: `${dateKey(first)}T00:00:00+02:00`,
      end: `${dateKey(exclusiveEnd)}T00:00:00+02:00`,
    }
  }, [visibleDates])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [taskResult, health] = await Promise.all([
        request<{ tasks: Task[] }>('/api/tasks?include_completed=true'),
        request<{ calendar_connected: boolean; calendar?: { error?: string | null } }>('/api/health'),
      ])
      setTasks(taskResult.tasks)
      setCalendarConnected(health.calendar_connected)
      if (!health.calendar_connected) {
        setEvents([])
        setError(health.calendar?.error || 'Google Calendar is disconnected. Reconnect in Settings. Scheduled tasks remain available.')
      } else {
        try {
          const result = await request<{ events: CalendarEvent[] }>(
            `/api/events?start=${encodeURIComponent(range.start)}&end=${encodeURIComponent(range.end)}`,
          )
          setEvents(result.events)
        } catch (cause) {
          setEvents([])
          setError(cause instanceof Error
            ? `Calendar events could not be loaded: ${cause.message}`
            : 'Calendar events could not be loaded.')
        }
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Calendar data could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [range])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    const interval = window.setInterval(() => { void load() }, 30000)
    return () => window.clearInterval(interval)
  }, [load])

  const tasksOn = (date: Date) => tasks.filter(task =>
    task.scheduled_at && valueDateKey(task.scheduled_at) === dateKey(date),
  )
  const eventsOn = (date: Date) => events.filter(event => valueDateKey(event.start) === dateKey(date))
  const selectedTasks = tasksOn(selectedDate)
  const selectedEvents = eventsOn(selectedDate)
  const calendarTitle = view === 'month'
    ? catDate(selectedDate, { month: 'long', year: 'numeric' })
    : view === 'day'
      ? catDate(selectedDate, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
      : `${catDate(visibleDates[0], { month: 'short', day: 'numeric' })} – ${catDate(visibleDates[6], { month: 'short', day: 'numeric', year: 'numeric' })}`

  function navigate(direction: number) {
    if (view === 'month') {
      const [year, month, day] = dateKey(selectedDate).split('-').map(Number)
      const targetMonth = new Date(Date.UTC(year, month - 1 + direction, 1, 12))
      const lastDay = new Date(Date.UTC(targetMonth.getUTCFullYear(), targetMonth.getUTCMonth() + 1, 0)).getUTCDate()
      setSelectedDate(dateObject(targetMonth.getUTCFullYear(), targetMonth.getUTCMonth() + 1, Math.min(day, lastDay)))
    } else {
      setSelectedDate(addDays(selectedDate, direction * (view === 'week' ? 7 : 1)))
    }
  }

  function openTask(task: Task) {
    setSelectedItem({ kind: 'task', value: task })
    setDraft({
      title: task.title,
      description: task.description || '',
      scheduled_at: inputDateTime(task.scheduled_at),
      deadline: task.deadline || '',
      priority: task.priority || 'medium',
      duration_minutes: task.duration_minutes || 60,
    })
  }

  async function saveTask(event: FormEvent) {
    event.preventDefault()
    if (selectedItem?.kind !== 'task') return
    setBusy(true)
    setError('')
    try {
      const updated = await request<Task>(`/api/tasks/${selectedItem.value.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          ...draft,
          scheduled_at: draft.scheduled_at || null,
          deadline: draft.deadline || null,
          duration_minutes: Number(draft.duration_minutes),
        }),
      })
      setSelectedItem({ kind: 'task', value: updated })
      await load()
      onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Task could not be updated.')
    } finally {
      setBusy(false)
    }
  }

  async function completeTask(task: Task) {
    setBusy(true)
    try {
      const updated = await request<Task>(`/api/tasks/${task.id}/complete`, { method: 'PATCH' })
      setSelectedItem({ kind: 'task', value: updated })
      await load()
      onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Task could not be completed.')
    } finally {
      setBusy(false)
    }
  }

  async function deleteTask(task: Task) {
    if (!window.confirm(`Delete "${task.title}"?`)) return
    setBusy(true)
    try {
      await request(`/api/tasks/${task.id}`, { method: 'DELETE' })
      setSelectedItem(null)
      await load()
      onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Task could not be deleted.')
    } finally {
      setBusy(false)
    }
  }

  function selectDate(date: Date) {
    setSelectedDate(atMidnight(date))
    setSelectedItem(null)
  }

  function renderDate(date: Date, compact: boolean) {
    const key = dateKey(date)
    const dayTasks = tasksOn(date)
    const dayEvents = eventsOn(date)
    const indicators = dayTasks.length > 0 && dayEvents.length > 0 ? 'both' : dayTasks.length > 0 ? 'task' : dayEvents.length > 0 ? 'event' : 'none'
    return <button
      type="button"
      key={key}
      className={`calendar-day ${compact ? 'compact' : ''} ${key.slice(0, 7) !== dateKey(selectedDate).slice(0, 7) && view === 'month' ? 'muted' : ''} ${key === dateKey(new Date()) ? 'today' : ''} ${key === dateKey(selectedDate) ? 'selected' : ''}`}
      onClick={() => selectDate(date)}
      aria-label={`${catDate(date, { dateStyle: 'full' })} CAT; ${dayTasks.length} scheduled tasks; ${dayEvents.length} events`}
      aria-pressed={key === dateKey(selectedDate)}
    >
      <span className="calendar-day-number">{Number(key.slice(-2))}</span>
      {view !== 'month' && <span className="calendar-column-label">{catDate(date, { weekday: 'short' })}</span>}
      <span className={`calendar-indicators ${indicators}`} aria-hidden="true">
        {dayTasks.map(task => <i className="task-indicator" key={`task-${task.id}`} />)}
        {dayEvents.map(event => <i className="event-indicator" key={`event-${event.id}`} />)}
      </span>
      {view !== 'month' && <span className="calendar-cell-count">{dayTasks.length + dayEvents.length || '—'} items</span>}
    </button>
  }

  function renderItems(date: Date) {
    const rows: CalendarItem[] = [
      ...tasksOn(date).map(value => ({ kind: 'task' as const, value })),
      ...eventsOn(date).map(value => ({ kind: 'event' as const, value })),
    ].sort((a, b) => {
      const aStart = a.kind === 'task' ? a.value.scheduled_at || '' : a.value.start
      const bStart = b.kind === 'task' ? b.value.scheduled_at || '' : b.value.start
      return aStart.localeCompare(bStart)
    })
    return rows.map(item => item.kind === 'task'
      ? <button className="calendar-agenda-item task-agenda-item" key={`task-${item.value.id}`} onClick={() => openTask(item.value)}>
          <span className="agenda-item-type">TASK</span>
          <strong>{item.value.title}</strong>
          <span>{displayDateTime(item.value.scheduled_at)} · {item.value.priority} · {item.value.duration_minutes} min · {item.value.status}</span>
        </button>
      : <button className="calendar-agenda-item event-agenda-item" key={`event-${item.value.id}`} onClick={() => setSelectedItem(item)}>
          <span className="agenda-item-type">EVENT</span>
          <strong>{item.value.summary}</strong>
          <span>{displayEventRange(item.value)}</span>
        </button>)
  }

  return <section className="panel calendar-page interactive-calendar">
    <div className="calendar-toolbar">
      <div className="calendar-nav-group">
        <button type="button" aria-label="Previous date range" onClick={() => navigate(-1)}>‹</button>
        <button type="button" aria-label="Next date range" onClick={() => navigate(1)}>›</button>
        <button type="button" onClick={() => setSelectedDate(atMidnight(new Date()))}>Today</button>
        <h2>{calendarTitle}</h2>
      </div>
      <div className="calendar-view-switch" role="group" aria-label="Calendar view">
        {(['month', 'week', 'day'] as const).map(item =>
          <button type="button" className={view === item ? 'active' : ''} key={item} onClick={() => setView(item)}>{item}</button>,
        )}
        <button type="button" className="secondary" onClick={() => void load()} disabled={loading}>{loading ? 'Loading…' : 'Refresh'}</button>
      </div>
    </div>
    {error && <div className="notice" role="status">{error}</div>}
    <div className="calendar-legend" aria-label="Calendar indicators">
      <span><i className="task-indicator" /> Scheduled task</span>
      <span><i className="event-indicator" /> Google Calendar event</span>
      <span><i className="both-indicator"><i /><i /></i> Both</span>
      {!calendarConnected && <span className="calendar-legend-status">Tasks remain visible while Calendar is disconnected.</span>}
    </div>
    {view === 'month' && <div className="month-grid" role="grid">
      {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map(weekday => <div className="weekday" key={weekday}>{weekday}</div>)}
      {visibleDates.map(date => renderDate(date, false))}
    </div>}
    {view !== 'month' && <div className={`calendar-range-grid ${view}`} role="grid">
      {visibleDates.map(date => <div className="calendar-range-column" key={dateKey(date)}>
        {renderDate(date, true)}
        <div className="calendar-range-items">{renderItems(date)}</div>
      </div>)}
    </div>}
    <section className="day-agenda">
      <div className="day-agenda-header">
        <div><span className="card-label">SELECTED DATE</span><h2>{catDate(selectedDate, { weekday: 'long', month: 'long', day: 'numeric' })}</h2></div>
        <span>{selectedTasks.length} tasks · {selectedEvents.length} events</span>
      </div>
      {loading && <div className="empty">Refreshing calendar data…</div>}
      {!loading && !selectedTasks.length && !selectedEvents.length && <div className="empty">Nothing scheduled for this date. Task deadlines are separate and do not schedule tasks.</div>}
      {!loading && <div className="calendar-agenda-list">{renderItems(selectedDate)}</div>}
    </section>
    {selectedItem && <div className="calendar-modal-backdrop" role="presentation" onClick={event => {
      if (event.target === event.currentTarget) setSelectedItem(null)
    }}>
      <section className="calendar-item-dialog" role="dialog" aria-modal="true" aria-labelledby="calendar-item-title">
        <div className="calendar-dialog-heading">
          <div><span className="card-label">{selectedItem.kind === 'task' ? 'TASK DETAILS' : 'GOOGLE CALENDAR EVENT'}</span><h2 id="calendar-item-title">{selectedItem.kind === 'task' ? selectedItem.value.title : selectedItem.value.summary}</h2></div>
          <button type="button" className="icon-button" aria-label="Close details" onClick={() => setSelectedItem(null)}>×</button>
        </div>
        {selectedItem.kind === 'task' ? <form className="calendar-task-edit" onSubmit={saveTask}>
          <label>Title<input required maxLength={300} value={draft.title} onChange={event => setDraft({ ...draft, title: event.target.value })} /></label>
          <label>Description<textarea maxLength={4000} value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} /></label>
          <div className="calendar-edit-grid">
            <label>Scheduled date and time<input type="datetime-local" value={draft.scheduled_at} onChange={event => setDraft({ ...draft, scheduled_at: event.target.value })} /></label>
            <label>Deadline<input type="date" value={draft.deadline} onChange={event => setDraft({ ...draft, deadline: event.target.value })} /></label>
            <label>Priority<select value={draft.priority} onChange={event => setDraft({ ...draft, priority: event.target.value })}><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select></label>
            <label>Estimated duration (minutes)<input type="number" min={5} max={1440} value={draft.duration_minutes} onChange={event => setDraft({ ...draft, duration_minutes: Number(event.target.value) })} /></label>
          </div>
          <p className="calendar-task-status">Status: <strong>{selectedItem.value.status}</strong>{selectedItem.value.completed_at ? ` · completed ${displayDateTime(selectedItem.value.completed_at)}` : ''}</p>
          <div className="calendar-dialog-actions">
            <button type="submit" className="primary-button" disabled={busy}>Save task</button>
            {selectedItem.value.status !== 'completed' && <button type="button" className="secondary" disabled={busy} onClick={() => void completeTask(selectedItem.value)}>Mark complete</button>}
            <button type="button" className="danger-button" disabled={busy} onClick={() => void deleteTask(selectedItem.value)}>Delete</button>
          </div>
        </form> : <div className="calendar-event-details">
          <p><strong>Date and time</strong><span>{selectedItem.value.start.length === 10
            ? displayEventRange(selectedItem.value)
            : `${displayDateTime(selectedItem.value.start)}${selectedItem.value.end ? ` – ${displayEventTime(selectedItem.value.end)}` : ''}`}</span></p>
          {!!selectedItem.value.description && <p><strong>Description</strong><span>{selectedItem.value.description}</span></p>}
          {!!selectedItem.value.location && <p><strong>Location</strong><span>{selectedItem.value.location}</span></p>}
          {!!selectedItem.value.htmlLink && <a href={selectedItem.value.htmlLink} target="_blank" rel="noreferrer">Open in Google Calendar</a>}
        </div>}
      </section>
    </div>}
  </section>
}
