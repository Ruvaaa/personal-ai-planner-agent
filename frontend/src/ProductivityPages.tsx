import { FormEvent, useEffect, useState } from 'react'

type WorkspacePage = 'planner' | 'inbox' | 'projects' | 'weekly' | 'insights' | 'notes' | 'capture'
type RecordItem = Record<string, any>

const API = 'http://127.0.0.1:8787'
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(API + path, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options?.headers },
    })
  } catch (cause) {
    if (cause instanceof TypeError) {
      throw new Error('The local planner API is unreachable. Check that the app is running and reconnect before saving.')
    }
    throw cause
  }
  const body = await response.json().catch(() => ({})) as T & { detail?: string }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`)
  return body
}
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })
const localDate = () => new Date().toISOString().slice(0, 10)
const showTime = (value: string) => new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
const showCatDateTime = (value: string) => new Date(value).toLocaleString([], {
  timeZone: 'Africa/Harare',
  weekday: 'long',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})
const formatWeekRange = (start: string, end: string) => {
  const startDate = new Date(`${start}T12:00:00`)
  const endDate = new Date(`${end}T12:00:00`)
  const startLabel = startDate.toLocaleDateString([], { month: 'short', day: 'numeric' })
  const endLabel = endDate.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })
  return `${startLabel} – ${endLabel}`
}

export default function ProductivityPages({ page, onChanged, initialDate }: { page: WorkspacePage; onChanged: () => void; initialDate?: string }) {
  const [data, setData] = useState<any>(null)
  const [items, setItems] = useState<RecordItem[]>([])
  const [projects, setProjects] = useState<RecordItem[]>([])
  const [taskOptions, setTaskOptions] = useState<RecordItem[]>([])
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [query, setQuery] = useState('')
  const [date, setDate] = useState(initialDate || localDate())
  const [capture, setCapture] = useState('')
  const [preview, setPreview] = useState<RecordItem | null>(null)
  const [projectName, setProjectName] = useState('')
  const [projectDescription, setProjectDescription] = useState('')
  const [inboxText, setInboxText] = useState('')
  const [noteTitle, setNoteTitle] = useState('')
  const [noteContent, setNoteContent] = useState('')
  const [activeNoteId, setActiveNoteId] = useState<string | null>(null)
  const [isNewNote, setIsNewNote] = useState(false)
  const [noteProject, setNoteProject] = useState('')
  const [noteTask, setNoteTask] = useState('')
  const [eventStart, setEventStart] = useState('')
  const [workStart, setWorkStart] = useState('09:00')
  const [workEnd, setWorkEnd] = useState('17:00')

  useEffect(() => {
    if (page === 'planner' && initialDate) setDate(initialDate)
  }, [page, initialDate])

  async function load() {
    setError('')
    try {
      if (page === 'planner') {
        const [proposals, settings] = await Promise.all([
          request<{ proposals: RecordItem[] }>('/api/planner/proposals'),
          request<RecordItem>('/api/planner/settings'),
        ])
        setData(proposals.proposals.find(item => item.date === date) || null)
        setWorkStart(settings.work_start)
        setWorkEnd(settings.work_end)
      } else if (page === 'inbox') {
        const result = await request<{ items: RecordItem[] }>('/api/inbox')
        setItems(result.items.filter(item => item.status === 'open'))
      } else if (page === 'projects') {
        const result = await request<{ projects: RecordItem[] }>('/api/projects')
        setItems(result.projects)
      } else if (page === 'weekly') {
        setData(await request(`/api/weekly-review?date=${encodeURIComponent(date)}`))
      } else if (page === 'insights') {
        const result = await request<{ insights: RecordItem[] }>(`/api/insights?date=${encodeURIComponent(date)}`)
        setItems(result.insights)
      } else if (page === 'notes') {
        const [noteResult, projectResult, taskResult] = await Promise.all([
          request<{ notes: RecordItem[] }>(`/api/notes${query ? `?q=${encodeURIComponent(query)}` : ''}`),
          request<{ projects: RecordItem[] }>('/api/projects'),
          request<{ tasks: RecordItem[] }>('/api/tasks?include_completed=true'),
        ])
        const sortedNotes = [...noteResult.notes].sort((a, b) =>
          String(b.created_at || '').localeCompare(String(a.created_at || '')),
        )
        setItems(sortedNotes)
        if (!isNewNote) {
          const currentNote = sortedNotes.find(note => note.id === activeNoteId)
          const nextNote = currentNote || sortedNotes[0]
          setActiveNoteId(nextNote?.id || null)
          if (nextNote) {
            setNoteTitle(nextNote.title || '')
            setNoteContent(nextNote.content || '')
          }
        }
        setProjects(projectResult.projects)
        setTaskOptions(taskResult.tasks)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load this view.')
    }
  }

  useEffect(() => { void load() }, [page, date, query])

  async function run(action: () => Promise<unknown>, success: string) {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await action()
      setMessage(success)
      await load()
      onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The request could not be completed.')
    } finally {
      setBusy(false)
    }
  }

  async function generatePlan() {
    await run(async () => {
      const result = await request('/api/planner/daily', json('POST', { date }))
      setData(result)
    }, 'Schedule proposal generated from current tasks and calendar commitments.')
  }

  function renderPlanner() {
    const blocks: RecordItem[] = data?.blocks || []
    return <div className="productivity-view">
      <section className="panel planner-controls">
        <div><span className="card-label">DETERMINISTIC PLAN</span><h2>Daily Planner</h2><p>{new Date(`${date}T12:00:00`).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })}</p></div>
        <div className="planner-actions"><label>Work hours <input type="time" value={workStart} onChange={event => setWorkStart(event.target.value)} /> to <input type="time" value={workEnd} onChange={event => setWorkEnd(event.target.value)} /></label><input aria-label="Plan date" type="date" value={date} onChange={event => setDate(event.target.value)} /><button className="primary-button" disabled={busy} onClick={() => void run(() => request('/api/planner/settings', json('PUT', { work_start: workStart, work_end: workEnd })), 'Working hours saved, then generate a fresh plan.')}>Save hours</button><button className="primary-button" disabled={busy} onClick={() => void generatePlan()}>Generate plan</button></div>
      </section>
      {!data ? <div className="empty">Generate a proposal to see available time, fixed commitments, due tasks, and suggested task blocks.</div> : <>
        <section className="planner-stats">
          <article className="metric"><span className="card-label">AVAILABLE</span><strong>{data.available_minutes}</strong><p>minutes in configured work hours</p></article>
          <article className="metric"><span className="card-label">PROPOSED WORK</span><strong>{data.scheduled_minutes}</strong><p>minutes; not calendar events</p></article>
          <article className="metric"><span className="card-label">DUE TODAY</span><strong>{data.due_today_tasks.length}</strong><p>outstanding tasks</p></article>
          <article className="metric"><span className="card-label">OVERDUE</span><strong>{data.overdue_tasks.length}</strong><p>deadline already passed</p></article>
        </section>
        {data.conflicts?.length > 0 && <div className="notice">{data.conflicts.join(' ')}</div>}
        {!!data.high_priority_tasks?.length && <section className="panel"><span className="card-label">HIGH PRIORITY</span><p>{data.high_priority_tasks.map((task: RecordItem) => task.title).join(' · ')}</p></section>}
        <section className="panel"><div className="panel-heading"><span className="card-label">TIMELINE</span><h2>Suggested blocks</h2></div>{blocks.length ? blocks.map((block: RecordItem) => <article className="productivity-row" key={block.id}><div><strong>{showTime(block.start)}–{showTime(block.end)} · {block.title}</strong><p>{block.kind === 'task' ? `${block.priority} priority · ${block.duration_minutes} min · AI suggestion` : `${block.duration_minutes} min · reserved break`}</p></div>{block.kind === 'task' && <div className="row-actions"><input aria-label={`Reschedule ${block.title}`} type="datetime-local" onChange={event => { block.newStart = event.target.value }} /><button className="secondary" disabled={busy} onClick={() => void run(() => request(`/api/planner/proposals/${data.id}/blocks/${block.id}`, json('PATCH', { start: block.newStart })), 'Task block rescheduled.')}>Move</button><button className="secondary" disabled={busy} onClick={() => void run(() => request(`/api/planner/proposals/${data.id}/blocks/${block.id}/complete`, json('POST', {})), 'Task marked complete.')}>Complete</button></div>}</article>) : <div className="empty">No tasks fit this day under the current work hours.</div>}</section>
        {!!data.fixed_events?.length && <section className="panel"><span className="card-label">CONFIRMED CALENDAR EVENTS</span>{data.fixed_events.map((event: RecordItem, index: number) => <p key={event.id || index}>{event.summary}: {event.start} to {event.end}</p>)}</section>}
        {!!data.unscheduled?.length && <section className="panel"><span className="card-label">NEEDS REVIEW</span>{data.unscheduled.map((item: RecordItem) => <p key={`${item.task_id}-${item.reason}`}>{item.title}: {item.reason}</p>)}</section>}
        <section className="panel proposal-footer"><p>Task blocks are suggestions and do not change your calendar unless you approve and commit them.</p><span className={`proposal-state ${data.status}`}>{data.status}</span><button className="secondary" disabled={busy || data.status === 'approved'} onClick={() => void run(() => request(`/api/planner/proposals/${data.id}/approve`, json('POST', {})), 'Proposal approved.')}>Approve proposal</button><button className="secondary" disabled={busy} onClick={() => void run(() => request(`/api/planner/proposals/${data.id}/reject`, json('POST', {})), 'Proposal rejected.')}>Reject</button><button className="primary-button" disabled={busy || data.status !== 'approved'} onClick={() => void run(() => request(`/api/planner/proposals/${data.id}/commit`, json('POST', {})), 'Google Calendar accepted the approved task blocks.')}>Add approved blocks to Calendar</button></section>
      </>}
    </div>
  }

  function renderInbox() {
    return <div className="productivity-view task-inbox-view">
      <section className="panel inbox-capture-panel">
        <div className="inbox-capture-heading">
          <div>
            <span className="card-label">CAPTURE</span>
            <h2>Task Inbox</h2>
            <p>Save loose ideas and commitments here, then turn them into tasks when you’re ready.</p>
          </div>
        </div>
        <form className="inbox-capture-form" onSubmit={event => {
          event.preventDefault()
          void run(async () => {
            await request('/api/inbox', json('POST', { text: inboxText, source: 'manual' }))
            setInboxText('')
          }, '')
        }}>
          <input
            value={inboxText}
            onChange={event => setInboxText(event.target.value)}
            placeholder="What do you need to remember?"
            aria-label="Capture an idea or commitment"
            required
            maxLength={4000}
          />
          <button className="primary-button" disabled={busy}>Add to inbox</button>
        </form>
      </section>
      {items.length ? <section className="inbox-items" aria-label="Open inbox items">
        {items.map(item => {
          const createdAt = item.created_at ? new Date(item.created_at) : null
          const dateLabel = createdAt && !Number.isNaN(createdAt.getTime())
            ? createdAt.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
            : 'Date unavailable'
          const sourceLabel = typeof item.source === 'string'
            ? item.source.replace(/_/g, ' ')
            : 'Captured'

          return <article className="panel inbox-item" key={item.id}>
            <header className="inbox-item-header">
              <span className="inbox-source">{sourceLabel}</span>
              <time dateTime={createdAt && !Number.isNaN(createdAt.getTime()) ? createdAt.toISOString() : undefined}>{dateLabel}</time>
            </header>
            <p className="inbox-item-text">{item.original_text || 'Untitled capture'}</p>
            <label className="inbox-notes-label" htmlFor={`inbox-notes-${item.id}`}>Notes <span>Optional</span></label>
            <textarea
              id={`inbox-notes-${item.id}`}
              aria-label={`Notes for ${item.original_text || 'inbox item'}`}
              defaultValue={item.notes || ''}
              placeholder="Add context or details to remember"
              onBlur={event => {
                if (event.target.value !== (item.notes || '')) {
                  void run(() => request(`/api/inbox/${encodeURIComponent(item.id)}`, json('PATCH', { notes: event.target.value })), '')
                }
              }}
            />
            <footer className="inbox-item-actions">
              <button
                className="primary-button"
                disabled={busy}
                onClick={() => void run(
                  () => request(`/api/inbox/${encodeURIComponent(item.id)}/convert`, json('POST', { record_type: 'task', details: { title: item.original_text } })),
                  '',
                )}
              >Convert to task</button>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void run(
                  () => request(`/api/inbox/${encodeURIComponent(item.id)}/status`, json('POST', { status: 'archived' })),
                  '',
                )}
              >Archive</button>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void run(
                  () => request(`/api/inbox/${encodeURIComponent(item.id)}/status`, json('POST', { status: 'dismissed' })),
                  '',
                )}
              >Dismiss</button>
            </footer>
          </article>
        })}
      </section> : <div className="empty inbox-empty">Your inbox is clear. Use Quick Capture, desktop chat, or this field to collect ideas.</div>}
    </div>
  }

  function renderProjects() {
    return <div className="productivity-view"><section className="panel"><span className="card-label">PROJECTS</span><h2>Project workspace</h2><form className="capture-form project-form" onSubmit={event => { event.preventDefault(); void run(async () => { await request('/api/projects', json('POST', { name: projectName, description: projectDescription })); setProjectName(''); setProjectDescription('') }, 'Project created.') }}><input value={projectName} onChange={event => setProjectName(event.target.value)} placeholder="Project name" required maxLength={200} /><input value={projectDescription} onChange={event => setProjectDescription(event.target.value)} placeholder="Description" maxLength={4000} /><button className="primary-button" disabled={busy}>Create project</button></form></section>{items.map(project => <ProjectCard key={project.id} project={project} busy={busy} run={run} />)}{!items.length && <div className="empty">No projects yet. Create one to group tasks and milestones.</div>}</div>
  }

  function renderWeekly() {
    return <div className="productivity-view weekly-review">
      <section className="panel review-controls weekly-review-controls">
        <div>
          <span className="card-label">WEEKLY SUMMARY</span>
          <p>Review your progress and upcoming work, based on saved task records.</p>
        </div>
        <label className="weekly-date-control">
          <span>Week containing</span>
          <input type="date" value={date} onChange={event => setDate(event.target.value)} />
        </label>
      </section>
      {data && <>
        <section className="planner-stats weekly-stats" aria-label="Weekly task summary">
          <article className="metric"><span className="card-label">COMPLETED</span><strong>{data.completed_tasks.length}</strong><p>this week</p></article>
          <article className="metric"><span className="card-label">CARRIED FORWARD</span><strong>{data.carried_forward_tasks.length}</strong><p>still to do</p></article>
          <article className="metric"><span className="card-label">OVERDUE</span><strong>{data.overdue_tasks.length}</strong><p>past deadline</p></article>
          <article className="metric"><span className="card-label">RECORDED EFFORT</span><strong>{data.completed_estimated_minutes}<span> min</span></strong><p>estimated task time</p></article>
        </section>

        <section className="panel weekly-details">
          <div className="weekly-range-heading">
            <div>
              <span className="card-label">WEEK AT A GLANCE</span>
              <h2>{formatWeekRange(data.week_start, data.week_end)}</h2>
            </div>
          </div>
          {data.historical_completion_dates_missing > 0 && <p className="history-note">
            {data.historical_completion_dates_missing} older completed task(s) don’t have a completion date, so historical totals may be incomplete.
          </p>}
          <div className="weekly-sections">
            <WeeklyReviewSection title="Completed" items={data.completed_tasks} empty="No tasks were recorded as completed this week." tone="completed" />
            <WeeklyReviewSection title="Carried forward" items={data.carried_forward_tasks} empty="Nothing due this week is left outstanding." tone="outstanding" />
            <WeeklyReviewSection title="Overdue" items={data.overdue_tasks} empty="No overdue tasks in your saved records." tone="overdue" />
            <WeeklyReviewSection title="Upcoming deadlines" items={data.upcoming_deadlines} empty="No upcoming deadlines recorded." tone="upcoming" />
          </div>
        </section>

        <section className="panel weekly-projects">
          <div className="weekly-range-heading">
            <div>
              <span className="card-label">LONGER-TERM WORK</span>
              <h2>Projects and milestones</h2>
            </div>
          </div>
          {data.projects.length || data.milestones_approaching.length ? <>
            <div className="weekly-project-list">
              {data.projects.map((project: RecordItem) => <p key={project.id}>
                <strong>{project.name}</strong><span>{project.progress_percent}% complete</span>
              </p>)}
              {data.milestones_approaching.map((milestone: RecordItem) => <p key={milestone.id}>
                <strong>{milestone.title}</strong><span>Milestone · {milestone.target_date}</span>
              </p>)}
            </div>
          </> : <p className="weekly-project-empty">No project or milestone updates for this week.</p>}
        </section>
      </>}
    </div>
  }

  function renderInsights() {
    return <div className="productivity-view insights-view">
      <section className="panel review-controls insights-overview">
        <div className="insights-overview-copy">
          <span className="card-label">RECORD-SUPPORTED</span>
          <h2>Workload overview</h2>
          <p>Review measurable workload signals from your tasks, deadlines, and calendar.</p>
        </div>
        <div className="insights-overview-controls">
          <label htmlFor="insight-date">Review date</label>
          <input id="insight-date" aria-label="Insight date" type="date" value={date} onChange={event => setDate(event.target.value)} />
          <div className="insights-count" aria-live="polite">
            <strong>{items.length}</strong>
            <span>{items.length === 1 ? 'signal found' : 'signals found'}</span>
          </div>
        </div>
      </section>
      {items.length ? <div className="insight-list">
        {items.map(item => {
          const records = Array.isArray(item.records)
            ? item.records
              .filter((record: RecordItem) => record && typeof record === 'object')
              .map((record: RecordItem) => record.title || record.name || record.summary)
              .filter((title: unknown): title is string => typeof title === 'string' && title.trim().length > 0)
            : []
          const kind = typeof item.kind === 'string' && item.kind.trim()
            ? item.kind.replace(/_/g, ' ')
            : 'Workload signal'

          return <article className="panel insight-card" key={item.id}>
            <div className="insight-card-header">
              <span className="insight-kind">{kind}</span>
              <span className="insight-source">Based on your records</span>
            </div>
            <h3>{item.title || 'Workload insight'}</h3>
            <p className="insight-explanation">{item.why || 'No explanation was provided for this insight.'}</p>
            <section className="insight-evidence" aria-label="Related records">
              <strong>Related records</strong>
              {records.length
                ? <ul>{records.map((title, index) => <li key={`${title}-${index}`}>{title}</li>)}</ul>
                : <p>No related records were provided.</p>}
            </section>
            <section className="insight-recommendation" aria-label="Suggested next step">
              <span className="card-label">SUGGESTED NEXT STEP</span>
              <p>{item.recommendation || 'Review the related records and choose an appropriate next step.'}</p>
            </section>
            {item.kind === 'capacity' && <section className="insight-calendar-action" aria-label="Accept calendar suggestion">
              <div>
                <span className="card-label">SUGGESTED CALENDAR SLOT · CAT</span>
                <p>{item.suggested_slot
                  ? `${showCatDateTime(item.suggested_slot.start)} (${item.suggested_slot.duration_minutes} min)`
                  : 'No conflict-free working slot is available within the next 60 days.'}</p>
                <small>Accepting adds this task to Google Calendar and updates its scheduled time.</small>
              </div>
              <button
                className="primary-button"
                disabled={busy || !item.suggested_slot}
                onClick={() => void run(
                  () => request(`/api/insights/${encodeURIComponent(item.id)}/accept`, json('POST', {
                    review_date: date,
                    expected_start: item.suggested_slot.start,
                  })),
                  'Insight accepted; the task was scheduled and added to Google Calendar.',
                )}
              >
                Accept &amp; add to Calendar
              </button>
            </section>}
            <footer className="insight-card-footer">
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void run(() => request(`/api/insights/${encodeURIComponent(item.id)}/dismiss`, json('POST', {})), 'Insight dismissed; underlying records remain unchanged.')}
              >
                Dismiss insight
              </button>
            </footer>
          </article>
        })}
      </div> : <section className="panel insights-empty">
        <span className="insights-empty-mark" aria-hidden="true">✓</span>
        <div>
          <h3>No workload issues found</h3>
          <p>No measurable workload signals were found for this date. If your calendar or working hours are missing, check Settings.</p>
        </div>
      </section>}
    </div>
  }

  function renderNotes() {
    const activeNote = items.find(note => note.id === activeNoteId) || (!isNewNote ? items[0] : null)
    const activeIndex = activeNote ? items.findIndex(note => note.id === activeNote.id) : -1
    const pageDate = activeNote?.created_at
      ? new Date(activeNote.created_at).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
      : new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })

    function selectNote(note: RecordItem) {
      setIsNewNote(false)
      setActiveNoteId(note.id)
      setNoteTitle(note.title || '')
      setNoteContent(note.content || '')
    }

    function startNewNote() {
      setQuery('')
      setIsNewNote(true)
      setActiveNoteId(null)
      setNoteTitle('')
      setNoteContent('')
    }

    function navigateNote(index: number) {
      const note = items[index]
      if (note) selectNote(note)
    }

    function saveCurrentNote(event: FormEvent) {
      event.preventDefault()
      void run(async () => {
        if (isNewNote || !activeNote) {
          const created = await request<RecordItem>('/api/notes', json('POST', { title: noteTitle, content: noteContent }))
          setItems(current => [created, ...current.filter(note => note.id !== created.id)])
          setActiveNoteId(created.id)
          setNoteTitle(created.title)
          setNoteContent(created.content)
          setIsNewNote(false)
        } else {
          await request(`/api/notes/${encodeURIComponent(activeNote.id)}`, json('PATCH', { title: noteTitle, content: noteContent }))
        }
      }, '')
    }

    function deleteCurrentNote() {
      if (!activeNote || !confirm(`Delete note “${activeNote.title}”?`)) return
      const nextNote = items[activeIndex + 1] || items[activeIndex - 1]
      void run(async () => {
        await request(`/api/notes/${encodeURIComponent(activeNote.id)}`, { method: 'DELETE' })
        if (nextNote) selectNote(nextNote)
        else startNewNote()
      }, '')
    }

    return <div className="productivity-view notebook-view">
      <section className="notebook-toolbar">
        <div>
          <span className="card-label">KNOWLEDGE CAPTURE</span>
          <h2>My notebook</h2>
          <p>Your notes, one page at a time.</p>
        </div>
        <button className="primary-button" type="button" onClick={startNewNote}>＋ New page</button>
      </section>
      <div className="notebook-layout">
        <section className="notebook-book" aria-label="Open notebook">
          <aside className="notebook-index">
          <label htmlFor="note-search">NOTEBOOK PAGES</label>
          <input id="note-search" className="note-search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search pages" />
          {items.length ? <nav className="notebook-page-list" aria-label="Notebook pages">
            {items.map((note, index) => <button
              type="button"
              key={note.id}
              className={!isNewNote && (activeNote?.id === note.id) ? 'notebook-page-link active' : 'notebook-page-link'}
              onClick={() => navigateNote(index)}
            >
              <strong>{note.title || 'Untitled page'}</strong>
              <time>{note.created_at ? new Date(note.created_at).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }) : 'Undated'}</time>
              <span>{note.content?.replace(/\s+/g, ' ').slice(0, 72) || 'Empty page'}</span>
            </button>)}
          </nav> : <p className="notebook-index-empty">{query ? 'No pages match your search.' : 'Your notebook is empty. Start a new page.'}</p>}
          </aside>
          <div className="notebook-spiral" aria-hidden="true">
            {Array.from({ length: 10 }, (_, index) => <span key={index} />)}
          </div>
          <form className="notebook-paper" onSubmit={saveCurrentNote}>
            <header className="notebook-page-heading">
              <div>
                <span className="card-label">{isNewNote || !activeNote ? 'NEW NOTEBOOK PAGE' : `PAGE ${activeIndex + 1} OF ${items.length}`}</span>
                <time dateTime={activeNote?.created_at || new Date().toISOString()}>{pageDate}</time>
              </div>
              {!isNewNote && activeNote && <button className="secondary notebook-delete" type="button" onClick={deleteCurrentNote}>Delete page</button>}
            </header>
            <input
              className="notebook-title"
              aria-label="Page title"
              value={noteTitle}
              onChange={event => setNoteTitle(event.target.value)}
              placeholder="Page title"
              required
              maxLength={200}
            />
            <textarea
              className="notebook-writing"
              aria-label="Note content"
              value={noteContent}
              onChange={event => setNoteContent(event.target.value)}
              placeholder="Start writing…"
              required
              maxLength={20000}
            />
            <footer className="notebook-page-footer">
              <span>{activeNote?.updated_at ? `Last edited ${new Date(activeNote.updated_at).toLocaleString()}` : 'Date added automatically when saved'}</span>
              <div>
                <button className="secondary" type="button" disabled={activeIndex <= 0 || isNewNote} onClick={() => navigateNote(activeIndex - 1)}>← Previous</button>
                <button className="secondary" type="button" disabled={activeIndex < 0 || activeIndex >= items.length - 1 || isNewNote} onClick={() => navigateNote(activeIndex + 1)}>Next →</button>
                <button className="primary-button" type="submit" disabled={busy}>{isNewNote || !activeNote ? 'Save page' : 'Save changes'}</button>
              </div>
            </footer>
          </form>
        </section>
      </div>
    </div>
  }

  function renderCapture() {
    return <div className="productivity-view"><section className="panel quick-capture-panel"><span className="card-label">QUICK CAPTURE</span><h2>Get it out of your head</h2><form onSubmit={event => { event.preventDefault(); void run(async () => { setPreview(await request('/api/capture/preview', json('POST', { text: capture }))) }, 'Review the extracted details before saving.') }}><textarea autoFocus value={capture} onChange={event => setCapture(event.target.value)} placeholder="Study Image Processing tomorrow for 90 minutes" required maxLength={4000} /><button className="primary-button" disabled={busy}>Preview details</button></form>{preview && <form className="capture-preview" onSubmit={event => { event.preventDefault(); void run(async () => { await request('/api/capture', json('POST', { text: capture, title: preview.title, deadline: preview.deadline, duration_minutes: preview.duration_minutes, priority: preview.priority, convert_to_task: true })); setCapture(''); setPreview(null) }, 'Task saved through the existing task manager.') }}><label>Title<input value={preview.title} onChange={event => setPreview({ ...preview, title: event.target.value })} required /></label><label>Deadline<input type="date" value={preview.deadline || ''} onChange={event => setPreview({ ...preview, deadline: event.target.value || null })} /></label><label>Duration (minutes)<input type="number" min="5" max="1440" value={preview.duration_minutes} onChange={event => setPreview({ ...preview, duration_minutes: Number(event.target.value) })} /></label><label>Priority<select value={preview.priority} onChange={event => setPreview({ ...preview, priority: event.target.value })}><option>low</option><option>medium</option><option>high</option></select></label><p>Original: {preview.original_text}</p><button className="primary-button" disabled={busy}>Save task</button><button type="button" className="secondary" onClick={() => setPreview(null)}>Edit message</button></form>}</section></div>
  }

  return <>
    {error && <div className="notice">{error}</div>}
    {message && <div className="success-message">{message}</div>}
    {page === 'planner' && renderPlanner()}
    {page === 'inbox' && renderInbox()}
    {page === 'projects' && renderProjects()}
    {page === 'weekly' && renderWeekly()}
    {page === 'insights' && renderInsights()}
    {page === 'notes' && renderNotes()}
    {page === 'capture' && renderCapture()}
  </>
}

function WeeklyReviewSection({ title, items, empty, tone }: {
  title: string
  items: RecordItem[]
  empty: string
  tone: 'completed' | 'outstanding' | 'overdue' | 'upcoming'
}) {
  return <section className={`weekly-section weekly-section-${tone}`}>
    <div className="weekly-section-heading">
      <h3>{title}</h3>
      <span>{items.length}</span>
    </div>
    {items.length ? <ul>
      {items.map(item => <li key={item.id}>
        <strong>{item.title}</strong>
        {item.deadline && <span>Due {item.deadline}</span>}
      </li>)}
    </ul> : <p className="weekly-section-empty">{empty}</p>}
  </section>
}

function ProjectCard({ project, busy, run }: { project: RecordItem; busy: boolean; run: (action: () => Promise<unknown>, success: string) => Promise<void> }) {
  const [milestoneTitle, setMilestoneTitle] = useState('')
  const [milestoneDate, setMilestoneDate] = useState('')
  const [taskTitle, setTaskTitle] = useState('')
  const [tasks, setTasks] = useState<RecordItem[]>([])
  useEffect(() => { void request<{ tasks: RecordItem[] }>(`/api/projects/${project.id}/tasks`).then(result => setTasks(result.tasks)).catch(() => setTasks([])) }, [project.id])
  const milestones: RecordItem[] = project.milestones || []
  return <article className="panel project-card">
    <header className="project-card-header">
      <div className="project-heading">
        <span className="project-status">{project.status.replace('_', ' ')}</span>
        <h3>{project.name}</h3>
        {project.description && <p>{project.description}</p>}
      </div>
      <div className="project-progress-value" aria-label={`${project.progress_percent}% complete`}>
        <strong>{project.progress_percent}%</strong>
        <span>complete</span>
      </div>
    </header>

    <div className="project-progress-track" role="progressbar" aria-label={`${project.name} progress`} aria-valuenow={project.progress_percent} aria-valuemin={0} aria-valuemax={100}>
      <span style={{ width: `${project.progress_percent}%` }} />
    </div>

    <div className="project-summary">
      <span><strong>{project.completed_task_count}</strong> completed</span>
      <span><strong>{project.outstanding_task_count}</strong> outstanding</span>
      <span><strong>{project.estimated_remaining_minutes}</strong> min remaining</span>
      {project.target_deadline && <span>Target {project.target_deadline}</span>}
      {project.at_risk && <span className="project-risk">{project.overdue_task_count || 0} overdue</span>}
    </div>

    <section className="project-content-section">
      <div className="project-section-heading">
        <h4>Tasks</h4>
        <span>{tasks.length}</span>
      </div>
      {tasks.length ? <ul className="project-record-list">
        {tasks.map(task => <li key={task.id}>
          <span className={`project-task-status ${task.status === 'completed' ? 'is-completed' : ''}`}>
            {task.status === 'completed' ? 'Completed' : 'Open'}
          </span>
          <strong>{task.title}</strong>
        </li>)}
      </ul> : <p className="project-section-empty">No tasks in this project yet.</p>}
    </section>

    <section className="project-content-section">
      <div className="project-section-heading">
        <h4>Milestones</h4>
        <span>{milestones.length}</span>
      </div>
      {milestones.length ? <ul className="project-record-list">
        {milestones.map((milestone: RecordItem) => <li key={milestone.id}>
          <span className={`project-task-status ${milestone.completed ? 'is-completed' : ''}`}>
            {milestone.completed ? 'Completed' : 'Upcoming'}
          </span>
          <strong>{milestone.title}</strong>
          {milestone.target_date && <span className="project-record-date">{milestone.target_date}</span>}
          {!milestone.completed && <button className="text-button" disabled={busy} onClick={() => void run(() => request(`/api/milestones/${milestone.id}`, json('PATCH', { completed: true })), 'Milestone completed.')}>Mark complete</button>}
        </li>)}
      </ul> : <p className="project-section-empty">No milestones added yet.</p>}
    </section>

    <div className="project-add-section">
      <form className="project-add-form project-milestone-form" onSubmit={event => {
        event.preventDefault()
        void run(async () => {
          await request(`/api/projects/${project.id}/milestones`, json('POST', { title: milestoneTitle, target_date: milestoneDate || null }))
          setMilestoneTitle('')
          setMilestoneDate('')
        }, 'Milestone added.')
      }}>
        <input value={milestoneTitle} onChange={event => setMilestoneTitle(event.target.value)} placeholder="Milestone name" aria-label="Milestone name" required />
        <input type="date" aria-label="Milestone target date" value={milestoneDate} onChange={event => setMilestoneDate(event.target.value)} />
        <button className="secondary" disabled={busy}>Add milestone</button>
      </form>
      <form className="project-add-form project-task-form" onSubmit={event => {
        event.preventDefault()
        void run(async () => {
          const task = await request<RecordItem>('/api/tasks', json('POST', { title: taskTitle, project_id: project.id }))
          setTasks(current => [...current, task])
          setTaskTitle('')
        }, 'Task added to project.')
      }}>
        <input value={taskTitle} onChange={event => setTaskTitle(event.target.value)} placeholder="Add a task to this project" aria-label="Project task title" required />
        <button className="secondary" disabled={busy}>Add task</button>
      </form>
    </div>
  </article>
}

function NoteCard({ note, busy, run }: { note: RecordItem; busy: boolean; run: (action: () => Promise<unknown>, success: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(note.title)
  const [content, setContent] = useState(note.content)
  return <article className="panel note-card">{editing ? <form onSubmit={event => { event.preventDefault(); void run(async () => { await request(`/api/notes/${note.id}`, json('PATCH', { title, content })); setEditing(false) }, 'Note updated.') }}><input value={title} onChange={event => setTitle(event.target.value)} required /><textarea value={content} onChange={event => setContent(event.target.value)} required /><button className="primary-button" disabled={busy}>Save changes</button><button type="button" className="secondary" onClick={() => setEditing(false)}>Cancel</button></form> : <><span className="card-label">UPDATED {new Date(note.updated_at).toLocaleString()}</span><h3>{note.title}</h3><pre>{note.content}</pre>{note.tags?.length > 0 && <p>{note.tags.join(' · ')}</p>}<button className="secondary" onClick={() => setEditing(true)}>Edit</button><button className="secondary" disabled={busy} onClick={() => { if (confirm(`Delete note “${note.title}”?`)) void run(() => request(`/api/notes/${note.id}`, { method: 'DELETE' }), 'Note deleted.') }}>Delete</button></>}</article>
}
