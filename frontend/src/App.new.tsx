import { FormEvent, KeyboardEvent, useEffect, useState } from 'react'
import { invoke, isTauri } from '@tauri-apps/api/core'
import ProductivityPages from './ProductivityPages'
import EmailPage from './EmailPage'
import CalendarPage from './CalendarPage'
import { formatCatDateTime } from './dateTime'

const API = 'http://127.0.0.1:8787'
type Task = { id: number; title: string; deadline?: string | null; scheduled_at?: string | null; priority: string; duration_minutes: number }
type Event = { id?: string; summary: string; start: string; end?: string; display_start?: string; description?: string }
type ChatAction =
  | { type: 'review_plan'; proposal_id: string; date: string }
  | { type: 'confirm_event'; pending_id: string; event: { title: string; start: string; end: string } }
type ChatMessage = { role: 'user' | 'assistant'; text: string; action?: ChatAction; calendarLink?: string }
type Page = 'today' | 'planner' | 'chat' | 'inbox' | 'tasks' | 'projects' | 'calendar' | 'email' | 'notes' | 'weekly' | 'insights' | 'settings' | 'capture'
const BACKEND_DIAGNOSTICS = '%APPDATA%\\com.personalai.agent\\backend.log'

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(API + path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  } catch {
    throw new Error(`The local planner backend is unreachable at 127.0.0.1:8787. Check ${BACKEND_DIAGNOSTICS} and retry.`)
  }
  const body = await response.json().catch(() => ({})) as T & { detail?: string }
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `Request failed (${response.status})`)
  return body
}
const time = (value: string) => value.includes('T') ? new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : ''
const titles: Record<Page, string> = {
  today: 'Overview', planner: 'Daily Planner', chat: 'Assistant', inbox: 'Task Inbox', tasks: 'Tasks',
  projects: 'Projects', calendar: 'Calendar', email: 'Email', notes: 'Notes', weekly: 'Weekly Review',
  insights: 'Workload Insights', settings: 'Settings', capture: 'Quick Capture',
}

export default function App() {
  const [page, setPage] = useState<Page>('today')
  const [tasks, setTasks] = useState<Task[]>([])
  const [events, setEvents] = useState<Event[]>([])
  const [health, setHealth] = useState<any>(null)
  const [geminiModel, setGeminiModel] = useState('')
  const [savingGeminiModel, setSavingGeminiModel] = useState(false)
  const [notice, setNotice] = useState('')
  const [apiAvailable, setApiAvailable] = useState(false)
  const [startupPending, setStartupPending] = useState(true)
  const [connected, setConnected] = useState(false)
  const [busy, setBusy] = useState(false)
  const [title, setTitle] = useState('')
  const [deadline, setDeadline] = useState('')
  const [scheduledAt, setScheduledAt] = useState('')
  const [priority, setPriority] = useState('medium')
  const [chat, setChat] = useState('')
  const [chatting, setChatting] = useState(false)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [plannerDate, setPlannerDate] = useState('')
  async function refresh(): Promise<boolean> {
    try {
      const status = await api<any>('/api/health')
      setApiAvailable(true)
      setHealth(status)
      setConnected(status.calendar_connected)
      setTasks((await api<{ tasks: Task[] }>('/api/tasks')).tasks)
      setEvents(status.calendar_connected ? (await api<{ events: Event[] }>('/api/events?days=365')).events : [])
      try { status.gmail = await api<any>('/api/gmail/status') } catch { status.gmail = { configured: false, connected: false } }
      setHealth(status)
      setNotice('')
      if (isTauri()) await invoke('notify_planner_data_changed')
      return true
    } catch (error) {
      setApiAvailable(false)
      setNotice(error instanceof Error ? error.message : 'Could not reach the local API')
      return false
    }
  }

  useEffect(() => {
    let active = true
    const waitForBackend = async () => {
      for (let attempt = 0; attempt < 40 && active; attempt += 1) {
        try {
          await api('/api/health')
          if (!active) return
          const loaded = await refresh()
          if (active) {
            setApiAvailable(loaded)
            setStartupPending(false)
          }
          return
        } catch {
          if (attempt < 39) await new Promise(resolve => window.setTimeout(resolve, 250))
        }
      }
      if (active) {
        setApiAvailable(false)
        setNotice(`The local planner service did not become ready. Check ${BACKEND_DIAGNOSTICS}, then retry startup.`)
        setStartupPending(false)
      }
    }
    void waitForBackend()
    return () => { active = false }
  }, [])

  async function retryConnection() {
    setStartupPending(true)
    setNotice('')
    try {
      if (isTauri()) await invoke('retry_backend')
      await api('/api/health')
      const loaded = await refresh()
      if (!loaded) throw new Error('The backend responded to health checks but planner data could not be loaded.')
      setStartupPending(false)
    } catch (error) {
      setApiAvailable(false)
      setNotice(error instanceof Error ? error.message : 'Backend startup failed. Check the local diagnostics and retry.')
      setStartupPending(false)
    }
  }

  useEffect(() => {
    if (health?.gemini?.model) setGeminiModel(health.gemini.model)
  }, [health?.gemini?.model])

  async function saveGeminiModel() {
    setSavingGeminiModel(true)
    setNotice('')
    try {
      const configuration = await api<any>('/api/gemini/model', {
        method: 'PUT',
        body: JSON.stringify({ model: geminiModel }),
      })
      setHealth((current: any) => current ? { ...current, gemini: configuration } : current)
      setNotice('Gemini model updated.')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Gemini model could not be updated.')
    } finally {
      setSavingGeminiModel(false)
    }
  }

  useEffect(() => {
    let active = true
    const probe = async () => {
      try {
        await api('/api/health')
        if (active) setApiAvailable(true)
      } catch {
        if (active) {
          setApiAvailable(false)
          setNotice(`The local planner service stopped responding. Check ${BACKEND_DIAGNOSTICS} and retry startup.`)
        }
      }
    }
    const interval = window.setInterval(() => { void probe() }, 5000)
    return () => {
      active = false
      window.clearInterval(interval)
    }
  }, [])
  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPage('capture')
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  async function addTask(event: FormEvent) {
    event.preventDefault()
    try {
      await api('/api/tasks', { method: 'POST', body: JSON.stringify({ title, deadline: deadline || null, scheduled_at: scheduledAt || null, priority }) })
      setTitle('')
      setDeadline('')
      setScheduledAt('')
      await refresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Task could not be added')
    }
  }

  async function completeTask(id: number) {
    try {
      await api(`/api/tasks/${id}/complete`, { method: 'PATCH' })
      await refresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Task could not be completed')
    }
  }

  async function removeTask(id: number) {
    if (!confirm('Delete this task?')) return
    try {
      await api(`/api/tasks/${id}`, { method: 'DELETE' })
      await refresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Task could not be deleted')
    }
  }

  async function connectCalendar() {
    setBusy(true)
    try {
      await api('/api/calendar/connect', { method: 'POST' })
      await refresh()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Calendar connection failed')
    } finally {
      setBusy(false)
    }
  }

  async function connectGmail() {
    setBusy(true)
    try {
      await api('/api/gmail/connect', { method: 'POST', body: JSON.stringify({}) })
      await refresh()
      setNotice('Gmail connected.')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Gmail connection did not complete')
    } finally {
      setBusy(false)
    }
  }

  async function sendChat(event: FormEvent) {
    event.preventDefault()
    const text = chat.trim()
    if (!text || chatting) return
    setChat('')
    setMessages(current => [...current, { role: 'user', text }])
    setChatting(true)
    try {
      const result = await api<{ reply: string; action?: ChatAction | null }>('/api/chat', { method: 'POST', body: JSON.stringify({ message: text }) })
      setMessages(current => [...current, { role: 'assistant', text: result.reply, action: result.action || undefined }])
      await refresh()
    } catch (error) {
      setMessages(current => [...current, { role: 'assistant', text: error instanceof Error ? error.message : 'Assistant unavailable' }])
    } finally {
      setChatting(false)
    }
  }

  function chatKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
    }
  }

  async function handleChatAction(index: number, action: ChatAction, confirm: boolean) {
    try {
      if (action.type === 'review_plan') {
        setPlannerDate(action.date)
        setPage('planner')
        return
      }
      if (confirm) {
        const result = await api<{ created: boolean; event: { id: string; htmlLink?: string } }>(
          `/api/events/${action.pending_id}/confirm`,
          { method: 'POST', body: '{}' },
        )
        if (!result.created || !result.event?.id) {
          throw new Error('Google Calendar did not return a verifiable event ID. The event was not confirmed.')
        }
        setMessages(current => current.map((item, itemIndex) => itemIndex === index
          ? {
            ...item,
            text: `Added "${action.event.title}" to Google Calendar (event ID ${result.event.id}).`,
            action: undefined,
            calendarLink: result.event.htmlLink,
          }
          : item))
        await refresh()
      } else {
        await api(`/api/events/${action.pending_id}/cancel`, { method: 'POST', body: '{}' })
        setMessages(current => current.map((item, itemIndex) => itemIndex === index
          ? { ...item, text: 'Cancelled the event proposal. Nothing was added to Google Calendar.', action: undefined }
          : item))
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'The planner action failed.')
    }
  }

  const taskView = (full = false) => <section className="panel tasks-panel">
    <div className="panel-heading"><span className="card-label">{full ? 'MANAGE' : 'NEXT UP'}</span><h2>{full ? 'All pending tasks' : 'Tasks'}</h2></div>
    <form className="quick-add" onSubmit={addTask}>
      <input value={title} onChange={event => setTitle(event.target.value)} placeholder="What needs doing?" required />
      <input aria-label="Deadline" type="date" value={deadline} onChange={event => setDeadline(event.target.value)} />
      <input aria-label="Scheduled date and time (optional)" type="datetime-local" value={scheduledAt} onChange={event => setScheduledAt(event.target.value)} />
      <select value={priority} onChange={event => setPriority(event.target.value)}><option>low</option><option>medium</option><option>high</option></select>
      <button>Add task</button>
    </form>
    <div className="task-list">{tasks.length ? tasks.map(task => <article className="task" key={task.id}>
      <button className="check" onClick={() => void completeTask(task.id)} aria-label={`Complete ${task.title}`} />
      <div><h3>{task.title}</h3><p>{task.scheduled_at ? `Scheduled ${new Date(task.scheduled_at).toLocaleString()}` : 'Not scheduled'} · {task.deadline ? `Due ${task.deadline}` : 'No deadline'} · {task.duration_minutes} min</p></div>
      <span className={`priority ${task.priority}`}>{task.priority}</span>
      <button className="delete-task" onClick={() => void removeTask(task.id)} aria-label={`Delete ${task.title}`}>×</button>
    </article>) : <div className="empty">Nothing pending.</div>}</div>
  </section>

  const navButton = (target: Page, label: string, icon: string, badge?: number) => <button key={target} className={page === target ? 'active' : ''} onClick={() => setPage(target)}><span aria-hidden="true">{icon}</span>{label}{badge !== undefined && <b>{badge}</b>}</button>

  if (startupPending) {
    return <main className="startup-screen" role="status"><span className="brand-mark">P</span><h1>Starting Personal AI Planner</h1><p>Waiting for the local planner service to become healthy…</p></main>
  }

  if (!apiAvailable) {
    return <main className="startup-screen" role="alert"><span className="brand-mark">P</span><h1>Planner service unavailable</h1><p>{notice || 'The local backend did not respond to its health check.'}</p><small>Diagnostics: {BACKEND_DIAGNOSTICS}</small><button onClick={() => void retryConnection()}>Retry startup</button></main>
  }

  return <div className="app-shell">
    <aside>
      <div className="brand"><span className="brand-mark">P</span><span>Personal<br /><strong>AI Agent</strong></span></div>
      <nav className="workspace-nav" aria-label="Main navigation">
        {navButton('today', 'Overview', '⌂')}
        {navButton('planner', 'Daily Planner', '▤')}
        {navButton('chat', 'Chat', '◌')}
        {navButton('inbox', 'Inbox', '↓')}
        {navButton('tasks', 'Tasks', '✓', tasks.length)}
        {navButton('projects', 'Projects', '▧')}
        {navButton('calendar', 'Calendar', '▦')}
        {navButton('email', 'Email', '✉')}
        {navButton('notes', 'Notes', '▱')}
        {navButton('weekly', 'Weekly Review', '◷')}
        {navButton('insights', 'Insights', '⌁')}
        {navButton('settings', 'Settings', '⚙')}
      </nav>
      <button className="quick-capture-launch" onClick={() => setPage('capture')} title="Quick Capture (Ctrl+Shift+K)">＋ Quick Capture <kbd>Ctrl+Shift+K</kbd></button>
      <div className="local-status"><i />{notice ? 'API needs attention' : 'Local API connected'}</div>
    </aside>
    <main>
      <header><div><p className="eyebrow">YOUR PERSONAL WORKSPACE</p><h1>{titles[page]}</h1></div><div className="header-actions">{isTauri() && <button className="secondary" onClick={() => void invoke('open_widget')}>Open desktop widget</button>}<button className="secondary" onClick={() => setPage('capture')}>＋ Capture</button><button className="icon-button" onClick={() => void refresh()} aria-label="Refresh">↻</button></div></header>
      {notice && <div className="notice">{notice}</div>}
      {page === 'today' && <>
        <section className="overview"><article className="hero-card"><span className="card-label">FOCUS</span><h2>{tasks.length ? `${tasks.length} tasks on your list` : 'Your list is clear'}</h2><p>Choose one meaningful task and give it an uninterrupted block of time.</p><button className="secondary" onClick={() => setPage('planner')}>Plan today</button></article><article className="metric"><span className="card-label">CALENDAR</span><strong>{connected ? events.length : '—'}</strong><p>{connected ? 'events loaded' : 'not connected'}</p></article></section>
        <section className="workspace-grid">{taskView()}<div className="panel"><span className="card-label">SCHEDULE</span><h2>Upcoming</h2>{connected && events.slice(0, 5).map((event, index) => <article className="event" key={event.id || index}><time>{event.display_start || time(event.start)}</time><h3>{event.summary}</h3></article>)}</div></section>
      </>}
      {page === 'tasks' && taskView(true)}
      {page === 'chat' && <section className="panel chat-panel"><div className="chat-log">{!messages.length && <div className="chat-intro">Ask about your tasks and calendar, create tasks or events, or generate a daily plan. Calendar changes always wait for your confirmation.</div>}{messages.map((message, index) => <div key={index} className={`message ${message.role}`}><div>{message.text}</div>{message.calendarLink && <a href={message.calendarLink} target="_blank" rel="noreferrer">Open in Google Calendar</a>}{message.action?.type === 'review_plan' && <div className="chat-action-card"><p>Plan proposal for {message.action.date}. Review it before applying any changes.</p><button className="secondary" onClick={() => handleChatAction(index, message.action!, true)}>Review in Daily Planner</button></div>}{message.action?.type === 'confirm_event' && <div className="chat-action-card"><p><strong>{message.action.event.title}</strong><br />{formatCatDateTime(message.action.event.start)} – {formatCatDateTime(message.action.event.end)}</p><button className="primary-button" onClick={() => handleChatAction(index, message.action!, true)}>Add to Google Calendar</button><button className="secondary" onClick={() => handleChatAction(index, message.action!, false)}>Cancel</button></div>}</div>)}{chatting && <div className="message assistant typing">Thinking…</div>}</div><form className="chat-compose" onSubmit={sendChat}><textarea value={chat} onChange={event => setChat(event.target.value)} onKeyDown={chatKey} placeholder="Ask your planner…" /><button disabled={chatting || !chat.trim()}>Send</button></form></section>}
      {page === 'calendar' && <CalendarPage onChanged={() => void refresh()} />}
      {page === 'email' && <EmailPage onChanged={() => void refresh()} />}
      {page === 'settings' && <section className="settings-page">
        <div className="settings-intro">
          <span className="card-label">PREFERENCES & CONNECTIONS</span>
          <h2>Application settings</h2>
          <p>Manage integrations, planning defaults, and where your information is stored.</p>
        </div>
        <div className="settings-grid">
          <article className="panel settings-card">
            <header className="settings-card-heading">
              <div><span className="card-label">AI PROVIDER</span><h2>Gemini</h2></div>
              <span className={health?.gemini?.configured ? 'settings-status connected' : 'settings-status'}>
                {health?.gemini?.configured ? 'Configured' : 'Needs setup'}
              </span>
            </header>
            <dl className="settings-details">
              <div><dt>Current model</dt><dd>{health?.gemini?.model || 'Unavailable'}</dd></div>
              <div><dt>API key</dt><dd>{health?.gemini?.configured ? 'Available' : 'Not configured'}</dd></div>
            </dl>
            <label className="gemini-model-control">
              AI model
              <select value={geminiModel} onChange={event => setGeminiModel(event.target.value)} disabled={!health?.gemini?.models?.length}>
                {(health?.gemini?.models || []).map((model: { id: string; label: string }) =>
                  <option value={model.id} key={model.id}>{model.label}</option>,
                )}
              </select>
              <small>{health?.gemini?.models?.find((model: { id: string }) => model.id === geminiModel)?.description || 'Choose a Gemini model for assistant requests.'}</small>
            </label>
            <button className="secondary" onClick={() => void saveGeminiModel()}
              disabled={savingGeminiModel || !geminiModel || geminiModel === health?.gemini?.model}>
              {savingGeminiModel ? 'Saving…' : 'Save model'}
            </button>
            <details className="settings-help">
              <summary>Configuration help</summary>
              <p>Set <code>GEMINI_API_KEY</code> in <code>%APPDATA%\\com.personalai.agent\\.env</code>, then restart the app.</p>
            </details>
          </article>
          <article className="panel settings-card">
            <header className="settings-card-heading">
              <div><span className="card-label">CALENDAR</span><h2>Google Calendar</h2></div>
              <span className={connected ? 'settings-status connected' : 'settings-status'}>
                {connected ? 'Connected' : 'Not connected'}
              </span>
            </header>
            <p className="settings-description">Connect your calendar to view events and plan around existing commitments.</p>
            <dl className="settings-details">
              <div><dt>OAuth credentials</dt><dd>{health?.calendar?.credentials_present ? 'Found' : 'Missing'}</dd></div>
              <div><dt>Saved authorization</dt><dd>{health?.calendar?.authorization_present ? 'Available' : 'Not found'}</dd></div>
            </dl>
            {health?.calendar?.error && <p className="notice" role="alert">{health.calendar.error}</p>}
            <details className="settings-help">
              <summary>OAuth setup</summary>
              <p>For this desktop runtime, place your Google OAuth desktop-client file at <code>{health?.calendar?.credentials_location || '%APPDATA%\\com.personalai.agent\\credentials.json'}</code>. Reconnect to authorize Calendar event access. The authorization token is stored beside that file and is never sent to the frontend.</p>
            </details>
            <button className="secondary" onClick={() => void connectCalendar()} disabled={busy}>
              {busy ? 'Connecting…' : connected ? 'Reconnect calendar' : 'Connect calendar'}
            </button>
          </article>
          <article className="panel settings-card settings-whatsapp">
            <header className="settings-card-heading">
              <div><span className="card-label">MESSAGING INTEGRATION</span><h2>WhatsApp</h2></div>
              <span className={health?.whatsapp?.ready ? 'settings-status connected' : 'settings-status'}>
                {health?.whatsapp?.ready ? 'Configured' : 'Not ready'}
              </span>
            </header>
            <dl className="settings-details">
              <div><dt>Integration</dt><dd>{health?.whatsapp?.enabled ? 'Enabled' : 'Disabled'}</dd></div>
              <div><dt>Credentials</dt><dd>{health?.whatsapp?.configuration_present ? 'Present' : 'Incomplete'}</dd></div>
              <div><dt>Public HTTPS URL</dt><dd>{health?.whatsapp?.public_url_configured ? 'Configured' : 'Required'}</dd></div>
            </dl>
            {health?.whatsapp?.webhook_url && <div className="settings-webhook">
              <code>{health.whatsapp.webhook_url}</code>
              <button className="secondary" onClick={() => void navigator.clipboard.writeText(health.whatsapp.webhook_url)}>Copy URL</button>
            </div>}
            <details className="settings-help">
              <summary>Setup and troubleshooting</summary>
              <p>Create a WhatsApp Business Platform app and phone number. Configure the access token, phone number ID, verify token, app secret, and allowed phone number in the backend environment, then subscribe to the messages webhook field.</p>
              <p>Receiving messages requires a publicly reachable HTTPS webhook. The desktop API stays loopback-only; publish only <code>/webhooks/whatsapp</code> through a path-restricted secure tunnel or reverse proxy. A local 127.0.0.1 URL is not reachable by Meta.</p>
              <p>“Configured” confirms local settings only; it does not verify Meta approval, webhook subscription, or message delivery. If messages do not arrive, check HTTPS forwarding, Meta webhook logs, and the sender allowlist.</p>
            </details>
          </article>
          <article className="panel settings-card">
            <header className="settings-card-heading">
              <div><span className="card-label">PLANNING</span><h2>Work hours</h2></div>
            </header>
            <p className="settings-description">Working hours are configured in Daily Planner. Plans do not assume work outside those hours.</p>
            <dl className="settings-details">
              <div><dt>Timezone</dt><dd>Africa/Harare</dd></div>
              <div><dt>Work hours</dt><dd>Daily Planner defaults</dd></div>
            </dl>
          </article>
          <article className="panel settings-card">
            <header className="settings-card-heading">
              <div><span className="card-label">YOUR DATA</span><h2>Local-first storage</h2></div>
              <span className="settings-status connected">On this device</span>
            </header>
            <p className="settings-description">Your productivity data is stored locally and remains on this device.</p>
            <dl className="settings-details">
              <div><dt>Tasks</dt><dd>tasks.json</dd></div>
              <div><dt>Inbox, projects, notes & plans</dt><dd>productivity.json</dd></div>
            </dl>
          </article>
          <GmailSettingsPanel onConnected={refresh} />
        </div>
      </section>}
      {(['planner', 'inbox', 'projects', 'weekly', 'insights', 'notes', 'capture'] as const).includes(page as any) && <ProductivityPages page={page as 'planner' | 'inbox' | 'projects' | 'weekly' | 'insights' | 'notes' | 'capture'} onChanged={() => void refresh()} initialDate={page === 'planner' ? plannerDate : undefined} />}
    </main>
  </div>
}

function GmailSettingsPanel({ onConnected }: { onConnected: () => Promise<boolean> }) {
  const [settings, setSettings] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function load() {
    try { setSettings(await api<any>('/api/gmail/status')) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Gmail status unavailable.') }
  }
  useEffect(() => { void load() }, [])
  async function save(values: Record<string, unknown>) {
    setBusy(true)
    setError('')
    try {
      setSettings(await api('/api/gmail/settings', { method: 'PATCH', body: JSON.stringify(values) }))
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Gmail settings could not be saved.') }
    finally { setBusy(false) }
  }
  async function disconnect(clearCache: boolean) {
    if (!confirm(clearCache ? 'Disconnect Gmail and clear locally cached email data? Gmail messages will not be deleted.' : 'Disconnect Gmail? Cached email data will remain on this device.')) return
    setBusy(true)
    try {
      await api(`/api/gmail/disconnect?clear_cache=${clearCache}`, { method: 'POST' })
      await load()
      await onConnected()
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Gmail could not be disconnected.') }
    finally { setBusy(false) }
  }
  return <section className="panel settings-card gmail-settings-panel">
    <header className="settings-card-heading">
      <div><span className="card-label">EMAIL INTEGRATION</span><h2>Gmail</h2></div>
      <span className={settings?.connected ? 'settings-status connected' : 'settings-status'}>
        {settings?.connected ? 'Connected' : settings?.configured ? 'Ready to connect' : 'Needs setup'}
      </span>
    </header>
    {error && <div className="notice">{error}</div>}
    <dl className="settings-details">
      <div><dt>Account</dt><dd>{settings?.account_email || (settings?.configured ? 'Not connected' : 'OAuth credentials not configured')}</dd></div>
      <div><dt>Last sync</dt><dd>{settings?.last_sync ? formatCatDateTime(settings.last_sync) : 'Not synced'}</dd></div>
      <div><dt>Sync status</dt><dd>{settings?.last_sync_error || 'No sync errors'}</dd></div>
    </dl>
    <div className="settings-primary-action">
      <button className="primary-button" disabled={busy || !settings?.configured} onClick={async () => {
        setBusy(true)
        try {
          await api('/api/gmail/connect', { method: 'POST', body: '{}' })
          await load()
          await onConnected()
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : 'Gmail connection did not complete.')
        } finally {
          setBusy(false)
        }
      }}>{settings?.connected ? 'Reconnect Gmail' : 'Connect Gmail'}</button>
    </div>
    <div className="settings-control-section">
      <h3>Sync preferences</h3>
      <div className="gmail-settings-controls">
        <label className="gmail-interval">Sync interval <span>minutes</span><input type="number" min="1" max="1440" value={settings?.sync_interval_minutes || 5} onChange={event => setSettings({ ...settings, sync_interval_minutes: Number(event.target.value) })} onBlur={() => void save({ sync_interval_minutes: settings?.sync_interval_minutes || 5 })} /></label>
        {[['sync_enabled', 'Background sync'], ['auto_sync', 'Automatic polling'], ['classification_enabled', 'AI classification'], ['notifications_enabled', 'Email notifications'], ['briefing_enabled', 'Daily briefing']].map(([key, title]) => <label className="gmail-toggle" key={key}><input type="checkbox" checked={Boolean(settings?.[key])} onChange={event => { setSettings({ ...settings, [key]: event.target.checked }); void save({ [key]: event.target.checked }) }} />{title}</label>)}
      </div>
    </div>
    <div className="settings-card-actions">
      <button className="secondary" disabled={busy || !settings?.connected} onClick={() => void disconnect(false)}>Disconnect</button>
      <button className="secondary" disabled={busy || !settings?.connected} onClick={() => void disconnect(true)}>Disconnect and clear cache</button>
    </div>
    <details className="settings-help">
      <summary>Gmail access and local data</summary>
      <p>Gmail uses a separate local token and requests Google’s <code>gmail.modify</code> scope plus the existing Calendar scope. The existing token.json and Calendar authorization are not replaced.</p>
    </details>
  </section>
}
