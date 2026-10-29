import { FormEvent, useEffect, useState } from 'react'

const API = 'http://127.0.0.1:8787'
type Task = { id: number; title: string; deadline?: string | null; priority: string; duration_minutes: number; status: string }
type CalendarEvent = { id?: string; summary: string; start: string; display_start?: string }

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(API + path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  const body = await response.json().catch(() => ({})) as T & { detail?: string }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`)
  return body
}

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [events, setEvents] = useState<CalendarEvent[]>([])
  const [connected, setConnected] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [title, setTitle] = useState('')
  const [deadline, setDeadline] = useState('')
  const [priority, setPriority] = useState('medium')
  const [chat, setChat] = useState('')
  const [messages, setMessages] = useState<{ role: 'assistant' | 'user'; text: string }[]>([])
  const [chatting, setChatting] = useState(false)
  const [activePage, setActivePage] = useState<'today' | 'tasks' | 'calendar'>('today')

  async function refresh() {
    try {
      const health = await api<{ calendar_connected: boolean }>('/api/health')
      setConnected(health.calendar_connected)
      setTasks((await api<{ tasks: Task[] }>('/api/tasks')).tasks)
      if (health.calendar_connected) setEvents((await api<{ events: CalendarEvent[] }>('/api/events')).events)
      setNotice('')
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Could not reach the local API') }
  }
  useEffect(() => { refresh() }, [])

  async function addTask(event: FormEvent) {
    event.preventDefault()
    try {
      await api('/api/tasks', { method: 'POST', body: JSON.stringify({ title, deadline: deadline || null, priority }) })
      setTitle(''); setDeadline(''); await refresh()
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Task could not be added') }
  }
  async function completeTask(id: number) {
    try { await api(`/api/tasks/${id}/complete`, { method: 'PATCH' }); await refresh() }
    catch (error) { setNotice(error instanceof Error ? error.message : 'Task could not be completed') }
  }
  async function connectCalendar() {
    setBusy(true)
    try { await api('/api/calendar/connect', { method: 'POST' }); await refresh() }
    catch (error) { setNotice(error instanceof Error ? error.message : 'Calendar connection failed') }
    finally { setBusy(false) }
  }

  async function sendChat(event: FormEvent) {
    event.preventDefault()
    const message = chat.trim()
    if (!message || chatting) return
    setChat('')
    setMessages(current => [...current, { role: 'user', text: message }])
    setChatting(true)
    try {
      const result = await api<{ reply: string }>('/api/chat', { method: 'POST', body: JSON.stringify({ message }) })
      setMessages(current => [...current, { role: 'assistant', text: result.reply }])
      await refresh()
    } catch (error) {
      const reply = error instanceof Error ? error.message : 'The assistant could not respond'
      setMessages(current => [...current, { role: 'assistant', text: reply }])
    } finally { setChatting(false) }
  }

  const pendingCount = tasks.length
  return <div className="app-shell">
    <aside><div className="brand"><span className="brand-mark">P</span><span>Personal<br/><strong>AI Agent</strong></span></div>
      <nav>
        <button className={activePage === 'today' ? 'active' : ''} onClick={() => setActivePage('today')}><span>⌂</span> Today</button>
        <button className={activePage === 'tasks' ? 'active' : ''} onClick={() => setActivePage('tasks')}><span>✓</span> Tasks <b>{pendingCount}</b></button>
        <button className={activePage === 'calendar' ? 'active' : ''} onClick={() => setActivePage('calendar')}><span>◷</span> Calendar</button>
      </nav>
      <div className="local-status"><i /> Local API {notice ? 'needs attention' : 'connected'}</div>
    </aside>
    <main>
      <header><div><p className="eyebrow">YOUR PERSONAL WORKSPACE</p><h1>{activePage === 'today' ? 'Good day.' : activePage === 'tasks' ? 'Tasks' : 'Calendar'}</h1></div><button className="icon-button" onClick={refresh} aria-label="Refresh">↻</button></header>
      {notice && <div className="notice" role="alert">{notice}</div>}
      {activePage === 'today' && <>
      <section className="overview">
        <article className="hero-card"><span className="card-label">FOCUS</span><h2>{pendingCount ? `${pendingCount} tasks on your list` : 'Your list is clear'}</h2><p>Choose one meaningful task, then give it an uninterrupted block of time.</p></article>
        <article className="metric"><span className="card-label">CALENDAR</span><strong>{connected ? events.length : '—'}</strong><p>{connected ? 'upcoming events' : 'not connected'}</p></article>
      </section>
      <section className="workspace-grid">
        <div className="panel">
          <div className="panel-heading"><div><span className="card-label">NEXT UP</span><h2>Tasks</h2></div></div>
          <form className="quick-add" onSubmit={addTask}><input value={title} onChange={e => setTitle(e.target.value)} placeholder="What needs doing?" required/><input aria-label="Deadline" type="date" value={deadline} onChange={e => setDeadline(e.target.value)} /><select value={priority} onChange={e => setPriority(e.target.value)}><option>low</option><option>medium</option><option>high</option></select><button>Add task</button></form>
          <div className="task-list">{tasks.length === 0 ? <div className="empty">Nothing pending. Enjoy the breathing room.</div> : tasks.map(task => <article key={task.id} className="task"><button className="check" onClick={() => completeTask(task.id)} aria-label={`Complete ${task.title}`} /><div><h3>{task.title}</h3><p>{task.deadline || 'No deadline'} · {task.duration_minutes} min</p></div><span className={`priority ${task.priority}`}>{task.priority}</span></article>)}</div>
        </div>
        <div className="panel calendar-panel"><div className="panel-heading"><div><span className="card-label">SCHEDULE</span><h2>Calendar</h2></div>{!connected && <button className="secondary" onClick={connectCalendar} disabled={busy}>{busy ? 'Connecting…' : 'Connect'}</button>}</div>
          {connected ? <div className="event-list">{events.length ? events.map((event, index) => <article className="event" key={event.id || index}><time>{event.display_start || event.start}</time><div><h3>{event.summary}</h3></div></article>) : <div className="empty">No upcoming events.</div>}</div> : <div className="connect-copy"><div className="calendar-glyph">31</div><h3>Bring your schedule into focus</h3><p>Connect Google Calendar locally to see commitments alongside your tasks.</p></div>}
        </div>
      </section>
      <section className="panel chat-panel">
        <div className="panel-heading"><div><span className="card-label">YOUR ASSISTANT</span><h2>Conversation</h2></div></div>
        <div className="chat-log" aria-live="polite">
          {!messages.length && <p className="chat-intro">Tell me how your day feels, what you need to finish, or what is getting in the way.</p>}
          {messages.map((message, index) => <div key={index} className={`message ${message.role}`}>{message.text}</div>)}
          {chatting && <div className="message assistant typing">Thinking…</div>}
        </div>
        <form className="chat-compose" onSubmit={sendChat}><input value={chat} onChange={event => setChat(event.target.value)} placeholder="Talk with your assistant…" disabled={chatting}/><button disabled={chatting || !chat.trim()}>Send</button></form>
      </section>
      </>}
      {activePage === 'tasks' && <section className="panel page-panel">
        <div className="panel-heading"><div><span className="card-label">MANAGE</span><h2>All pending tasks</h2></div></div>
        <form className="quick-add" onSubmit={addTask}><input value={title} onChange={e => setTitle(e.target.value)} placeholder="What needs doing?" required/><input aria-label="Deadline" type="date" value={deadline} onChange={e => setDeadline(e.target.value)} /><select value={priority} onChange={e => setPriority(e.target.value)}><option>low</option><option>medium</option><option>high</option></select><button>Add task</button></form>
        <div className="task-list">{tasks.length === 0 ? <div className="empty">Nothing pending. Enjoy the breathing room.</div> : tasks.map(task => <article key={task.id} className="task"><button className="check" onClick={() => completeTask(task.id)} aria-label={`Complete ${task.title}`} /><div><h3>{task.title}</h3><p>{task.deadline || 'No deadline'} · {task.duration_minutes} min</p></div><span className={`priority ${task.priority}`}>{task.priority}</span></article>)}</div>
      </section>}
      {activePage === 'calendar' && <section className="panel page-panel calendar-page">
        <div className="panel-heading"><div><span className="card-label">SCHEDULE</span><h2>Upcoming calendar</h2></div>{!connected && <button className="secondary" onClick={connectCalendar} disabled={busy}>{busy ? 'Connecting…' : 'Connect Google Calendar'}</button>}</div>
        {connected ? <div className="event-list calendar-page-list">{events.length ? events.map((event, index) => <article className="event" key={event.id || index}><time>{event.display_start || event.start}</time><div><h3>{event.summary}</h3></div></article>) : <div className="empty">No upcoming events.</div>}</div> : <div className="connect-copy"><div className="calendar-glyph">31</div><h3>Connect your calendar</h3><p>Google Calendar stays local and only connects through the authorized desktop backend.</p></div>}
      </section>}
    </main>
  </div>
}
