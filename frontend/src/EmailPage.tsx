import { FormEvent, useEffect, useState } from 'react'
import { formatCatDateTime, formatEmailDate } from './dateTime'

type Email = Record<string, any>
const API = 'http://127.0.0.1:8787'

async function localApiFailureMessage(): Promise<string> {
  try {
    const response = await fetch(`${API}/api/health`, { cache: 'no-store' })
    if (response.ok) {
      return 'The local planner API is running, but the reply request was interrupted. Try again; if it continues, restart the app.'
    }
    return `The local planner API responded with HTTP ${response.status} during its health check. Restart the app, then retry.`
  } catch (cause) {
    if (cause instanceof TypeError) {
      return 'Could not reach the local planner API. Make sure it is running, then retry.'
    }
    throw cause
  }
}

async function call<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(API + path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  } catch (cause) {
    if (cause instanceof TypeError) {
      throw new Error(await localApiFailureMessage())
    }
    throw cause
  }
  const body = await response.json().catch(() => ({})) as T & { detail?: string }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`)
  return body
}
const json = (method: string, value: unknown): RequestInit => ({ method, body: JSON.stringify(value) })
const folders = [
  ['inbox', 'Inbox'], ['starred', 'Starred'], ['sent', 'Sent'], ['drafts', 'Drafts'], ['archive', 'Archive'],
]
const gmailCategories = [
  { id: '', label: 'All mail' },
  { id: 'primary', label: 'Primary' },
  { id: 'promotions', label: 'Promotions' },
  { id: 'social', label: 'Social' },
  { id: 'updates', label: 'Updates' },
  { id: 'forums', label: 'Forums' },
]

type MailIconName = 'back' | 'archive' | 'unread' | 'read' | 'star' | 'more' | 'forward' | 'tag' | 'task'

function MailIcon({ name }: { name: MailIconName }) {
  const shared = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
  if (name === 'back') return <svg viewBox="0 0 24 24" aria-hidden="true" {...shared}><path d="m15 18-6-6 6-6" /><path d="M9 12h11" /></svg>
  if (name === 'archive') return <svg viewBox="0 0 24 24" aria-hidden="true" {...shared}><path d="M4 4h16v4H4z" /><path d="M5 8v12h14V8M9 12h6" /></svg>
  if (name === 'unread' || name === 'read') return <svg viewBox="0 0 24 24" aria-hidden="true" {...shared}><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m4 7 8 6 8-6" />{name === 'unread' && <circle cx="19" cy="5" r="2.5" fill="currentColor" stroke="white" />}</svg>
  if (name === 'star') return <svg viewBox="0 0 24 24" aria-hidden="true" {...shared}><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z" /></svg>
  if (name === 'more') return <svg viewBox="0 0 24 24" aria-hidden="true" {...shared}><circle cx="12" cy="5" r="1" fill="currentColor" /><circle cx="12" cy="12" r="1" fill="currentColor" /><circle cx="12" cy="19" r="1" fill="currentColor" /></svg>
  if (name === 'forward') return <svg viewBox="0 0 24 24" aria-hidden="true" {...shared}><path d="m14 5 7 7-7 7" /><path d="M21 12H9a6 6 0 0 0-6 6" /></svg>
  if (name === 'tag') return <svg viewBox="0 0 24 24" aria-hidden="true" {...shared}><path d="M20 13 13 20 3 10V4h6z" /><circle cx="7.5" cy="8" r="1" /></svg>
  return <svg viewBox="0 0 24 24" aria-hidden="true" {...shared}><path d="M12 3v18M5 7h14M5 17h14" /><circle cx="8" cy="7" r="2" fill="white" /><circle cx="16" cy="17" r="2" fill="white" /></svg>
}

export default function EmailPage({ onChanged }: { onChanged: () => void }) {
  const [status, setStatus] = useState<Email | null>(null)
  const [labels, setLabels] = useState<Email[]>([])
  const [folder, setFolder] = useState('inbox')
  const [category, setCategory] = useState('')
  const [gmailCategory, setGmailCategory] = useState('primary')
  const [query, setQuery] = useState('')
  const [messages, setMessages] = useState<Email[]>([])
  const [nextPage, setNextPage] = useState<string | null>(null)
  const [selected, setSelected] = useState<Email | null>(null)
  const [thread, setThread] = useState<Email | null>(null)
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [compose, setCompose] = useState<Email | null>(null)
  const [draftId, setDraftId] = useState('')
  const [tone, setTone] = useState('professional')
  const [draftingReply, setDraftingReply] = useState(false)
  const [summarizing, setSummarizing] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [summary, setSummary] = useState('')
  const [taskSuggestion, setTaskSuggestion] = useState<Email | null>(null)
  const [attachmentBusy, setAttachmentBusy] = useState(false)

  async function refreshStatus() {
    try {
      const current = await call<Email>('/api/gmail/status')
      setStatus(current)
      if (current.connected) {
        const result = await call<{ labels: Email[] }>('/api/gmail/labels')
        setLabels(result.labels.filter(label => label.type === 'user'))
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load Gmail status.')
    }
  }

  async function loadMessages(token?: string | null) {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams({ folder, q: query, page_size: '30' })
      if (category) params.set('category', category)
      if (folder === 'inbox' && gmailCategory) params.set('gmail_category', gmailCategory)
      if (token) params.set('page_token', token)
      const result = await call<{ messages: Email[]; next_page_token?: string }>('/api/gmail/messages?' + params)
      setMessages(previous => token ? [...previous, ...result.messages] : result.messages)
      setNextPage(result.next_page_token || null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load Gmail messages.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void refreshStatus() }, [])
  useEffect(() => {
    setMessages([])
    setNextPage(null)
    setSelected(null)
    setThread(null)
    if (status?.connected) void loadMessages()
  }, [folder, category, gmailCategory, query, status?.connected])

  async function run(action: () => Promise<unknown>, success: string) {
    setError('')
    setNotice('')
    try {
      await action()
      setNotice(success)
      await Promise.all([loadMessages(), refreshStatus()])
      onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The email action failed.')
    }
  }

  async function openMessage(message: Email) {
    setSelected(message)
    setMoreOpen(false)
    setSummary('')
    setTaskSuggestion(null)
    setError('')
    try {
      const detail = await call<Email>(`/api/gmail/messages/${message.id}`)
      setSelected(detail)
      const conversation = await call<Email>(`/api/gmail/threads/${detail.thread_id}`)
      setThread(conversation)
      if (detail.unread || detail.labels?.includes('UNREAD')) {
        const result = await call<Email>(`/api/gmail/messages/${detail.id}/action`, json('POST', { action: 'read' }))
        setSelected(current => current?.id === detail.id ? { ...current, unread: false, labels: result.labels } : current)
        setMessages(current => current.map(item => item.id === detail.id ? { ...item, unread: false, labels: result.labels } : item))
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not open this message.')
    }
  }

  async function syncNow() {
    setSyncing(true)
    setError('')
    setNotice('')
    const previousSync = status?.last_sync ?? null
    try {
      await call('/api/gmail/sync', json('POST', {}))
      setNotice('Syncing Gmail…')
      for (let attempt = 0; attempt < 60; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, 1000))
        const current = await call<Email>('/api/gmail/status')
        setStatus(current)
        if (current.syncing) continue
        if (current.last_sync_error) throw new Error(current.last_sync_error)
        if (current.last_sync && current.last_sync !== previousSync) {
          setNotice('Gmail is up to date.')
          await Promise.all([loadMessages(), refreshStatus()])
          onChanged()
          return
        }
        setNotice(current.sync_enabled === false ? 'Sync is paused in Gmail settings.' : 'Sync finished; no new sync time was reported.')
        await Promise.all([loadMessages(), refreshStatus()])
        return
      }
      setNotice('Sync is still running. Check the status again shortly.')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Gmail sync could not start.')
      setNotice('')
    } finally {
      setSyncing(false)
    }
  }

  async function connect() {
    setSyncing(true)
    setError('')
    try {
      const result = await call<Email>('/api/gmail/connect', json('POST', {}))
      setStatus({ ...status, connected: true, account_email: result.account_email })
      setNotice(`Connected ${result.account_email}.`)
      await refreshStatus()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Gmail connection did not complete.')
    } finally {
      setSyncing(false)
    }
  }

  async function draftReply(mode: 'reply' | 'reply_all') {
    if (!selected) return
    setDraftingReply(true)
    setError('')
    try {
      const draft = await call<Email>(`/api/gmail/messages/${selected.id}/reply-draft`, json('POST', { tone, mode }))
      setCompose(draft)
      setDraftId('')
      setMoreOpen(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not draft a reply.')
    } finally {
      setDraftingReply(false)
    }
  }

  async function startManualReply(mode: 'reply' | 'reply_all' = 'reply') {
    if (!selected) return
    setDraftingReply(true)
    setError('')
    try {
      const draft = await call<Email>(`/api/gmail/messages/${selected.id}/reply`, json('POST', { mode }))
      setCompose(draft)
      setDraftId('')
      setMoreOpen(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not open a reply.')
    } finally {
      setDraftingReply(false)
    }
  }

  async function prepareForward() {
    if (!selected) return
    try {
      const draft = await call<Email>(`/api/gmail/messages/${selected.id}/forward`, json('POST', {}))
      setCompose(draft)
      setDraftId('')
      setMoreOpen(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not prepare forwarding.')
    }
  }

  async function messageAction(action: string, success: string, closeMessage = false) {
    if (!selected) return
    const messageId = selected.id
    await run(async () => {
      const result = await call<Email>(`/api/gmail/messages/${messageId}/action`, json('POST', { action }))
      setSelected(current => current?.id === messageId
        ? { ...current, labels: result.labels, unread: result.labels.includes('UNREAD'), starred: result.labels.includes('STARRED') }
        : current)
      setMessages(current => current.map(item => item.id === messageId
        ? { ...item, labels: result.labels, unread: result.labels.includes('UNREAD'), starred: result.labels.includes('STARRED') }
        : item))
      if (closeMessage) {
        setSelected(null)
        setThread(null)
      }
      setMoreOpen(false)
    }, success)
  }

  async function saveDraft() {
    if (!compose) return
    await run(async () => {
      const saved = await call<Email>('/api/gmail/drafts', json('POST', { ...compose, draft_id: draftId || undefined }))
      setDraftId(saved.draft_id || '')
    }, 'Draft saved in Gmail.')
  }

  function startCompose() {
    setCompose({ to: '', cc: '', subject: '', body: '', thread_id: null })
    setDraftId('')
    setSelected(null)
    setThread(null)
    setError('')
    setNotice('')
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault()
    if (!compose || !confirm('Send this email now? AI-generated content is never sent automatically.')) return
    await run(async () => {
      await call('/api/gmail/send', json('POST', { ...compose, draft_id: draftId || undefined }))
      setCompose(null)
      setDraftId('')
    }, 'Gmail accepted the message for sending.')
  }

  async function generateSummary() {
    if (!selected) return
    setSummarizing(true)
    setError('')
    try {
      const result = await call<{ summary: string }>(`/api/gmail/threads/${selected.thread_id}/summary`, json('POST', {}))
      setSummary(result.summary)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not summarize this conversation.')
    } finally {
      setSummarizing(false)
    }
  }

  async function classify() {
    if (!selected) return
    await run(async () => {
      const result = await call<Email>(`/api/gmail/messages/${selected.id}/classify`, json('POST', {}))
      setSelected(current => current ? { ...current, classification: result } : current)
    }, 'Email classification updated.')
  }

  async function taskFromEmail(event: FormEvent) {
    event.preventDefault()
    if (!selected || !taskSuggestion) return
    await run(async () => {
      await call(`/api/gmail/messages/${selected.id}/task`, json('POST', taskSuggestion))
      setTaskSuggestion(null)
    }, 'Task created and linked to its source email.')
  }

  async function downloadAttachment(attachment: Email) {
    if (!selected) return
    setAttachmentBusy(true)
    try {
      const response = await fetch(`${API}/api/gmail/messages/${selected.id}/attachment/${attachment.attachment_id}`)
      if (!response.ok) throw new Error('Attachment download failed.')
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = attachment.filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Attachment download failed.')
    } finally {
      setAttachmentBusy(false)
    }
  }

  if (!status?.connected) return <section className="panel email-connect-panel">
    <header className="email-connect-heading">
      <div className="email-connect-icon" aria-hidden="true">✉</div>
      <div>
        <span className="card-label">GOOGLE WORKSPACE</span>
        <h2>Connect Gmail</h2>
        <p>Bring your inbox into your planning workspace.</p>
      </div>
    </header>
    {error && <div className="notice">{error}</div>}
    {status?.configured
      ? <div className="email-connect-ready">
        <span className="email-setup-status ready">Ready to connect</span>
        <p>Authorize Gmail using your existing local Google OAuth client. Calendar authorization remains separate.</p>
      </div>
      : <div className="email-connect-setup">
        <span className="email-setup-status">{status ? 'Setup required' : 'Checking setup…'}</span>
        <p>To connect Gmail, complete these steps on this device:</p>
        <ol>
          <li>Add your Google OAuth client <code>credentials.json</code> to the app’s local data directory.</li>
          <li>Enable the Gmail API for your Google Cloud project.</li>
          <li>Return here and connect your account.</li>
        </ol>
      </div>}
    <div className="email-connect-action">
      <button className="primary-button" onClick={() => void connect()} disabled={syncing || !status?.configured}>
        {syncing ? 'Waiting for Google…' : 'Connect Gmail'}
      </button>
      {!status?.configured && <span>The connect button becomes available when setup is complete.</span>}
    </div>
    <details className="email-connect-permissions">
      <summary>What access does Gmail need?</summary>
      <p>Requested access supports message management and sending. Google may show an unverified-app warning for a personal OAuth project in testing mode.</p>
    </details>
  </section>

  return <section className="email-client">
    <aside className="email-folders">
      <div className="email-sidebar-heading">
        <span className="card-label">MAILBOX</span>
        <button className="primary-button" onClick={startCompose}>Compose</button>
      </div>
      <p className="account-email">{status.account_email}</p>
      {folders.map(([id, name]) => <button key={id} className={folder === id ? 'email-folder active' : 'email-folder'} onClick={() => setFolder(id)}>{name}</button>)}
      <span className="card-label email-label-heading">LABELS</span>
      {labels.map(label => <button key={label.id} className={folder === label.id ? 'email-folder active' : 'email-folder'} onClick={() => setFolder(label.id)}>{label.name}</button>)}
      <div className="email-sync-info">
        <span>Last sync</span>
        <strong>{status.last_sync ? formatCatDateTime(status.last_sync) : 'Not synced yet'}</strong>
        {status.last_sync_error && <span className="email-sync-error" role="status">{status.last_sync_error}</span>}
        <button className="secondary" onClick={() => void syncNow()} disabled={syncing || status.syncing}>
          {syncing || status.syncing ? 'Syncing…' : 'Sync now'}
        </button>
      </div>
    </aside>
    <div className="email-list-panel">
      <div className="email-list-toolbar">
        <input aria-label="Search Gmail" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search messages" />
        {folder === 'inbox' && (
          <div className="email-category-tabs" role="tablist" aria-label="Gmail inbox categories">
            {gmailCategories.map(item => (
              <button key={item.id || 'all'} type="button" role="tab" aria-selected={gmailCategory === item.id}
                className={gmailCategory === item.id ? 'email-category-tab active' : 'email-category-tab'}
                onClick={() => setGmailCategory(item.id)}>{item.label}</button>
            ))}
          </div>
        )}
        <select aria-label="Filter by AI category" value={category} onChange={event => setCategory(event.target.value)}>
          <option value="">All planning categories</option>
          <option value="academic">Academic</option><option value="career">Career</option>
          <option value="meeting">Meetings</option><option value="action_required">Action required</option>
          <option value="reply_needed">Reply needed</option><option value="important">Important</option>
          <option value="financial_admin">Financial/admin</option><option value="newsletter">Newsletters</option>
          <option value="general">General</option>
        </select>
      </div>
      {error && <div className="email-inline-error" role="alert">{error}</div>}
      {notice && <div className="email-inline-notice" role="status">{notice}</div>}
      {loading && <div className="empty">Loading messages…</div>}
      {!loading && !messages.length && <div className="empty">No messages in this view.</div>}
      {messages.map(message => (
        <article key={message.id}
        className={`email-list-item ${selected?.id === message.id ? 'selected' : ''} ${message.unread ? 'unread' : ''}`}>
        <div className="email-list-top">
          <strong className="email-sender">{message.from || 'Unknown sender'}</strong>
          <time className="email-date" title={formatEmailDate(message)}>
            {formatEmailDate(message, true)}
          </time>
            <button type="button" className="email-star" aria-label={message.starred ? 'Unstar message' : 'Star message'}
              onClick={event => { event.stopPropagation(); void run(() => call(`/api/gmail/messages/${message.id}/action`, json('POST', { action: message.starred ? 'unstar' : 'star' })), 'Star updated.') }}>
              {message.starred ? '★' : '☆'}
            </button>
          </div>
          <button type="button" className="email-message-open"
            aria-label={`Open ${message.subject || 'message'} from ${message.from || 'unknown sender'}`}
            aria-current={selected?.id === message.id ? 'true' : undefined}
            onClick={() => void openMessage(message)}>
            <strong className="email-subject">{message.subject || '(no subject)'}</strong>
            <p>{message.snippet}</p>
            {message.classification?.categories?.length > 0 && (
              <span className="email-list-bottom">
                {message.classification.categories.slice(0, 2).map((tag: string) => <span className="email-category" key={tag}>{tag.replace('_', ' ')}</span>)}
              </span>
            )}
          </button>
        </article>
      ))}
      {nextPage && <button className="secondary email-more" disabled={loading} onClick={() => void loadMessages(nextPage)}>Load more</button>}
    </div>
    <div className="email-reading-panel">
      {compose ? <form className="email-composer" onSubmit={sendMessage}><div className="email-reader-header"><div><span className="card-label">MESSAGE COMPOSER</span><h2>{compose.source_message_id ? 'Reply draft' : 'New email'}</h2></div><button type="button" className="icon-button" aria-label="Close composer" onClick={() => setCompose(null)}>×</button></div><label>To<input value={compose.to} onChange={event => setCompose({ ...compose, to: event.target.value })} required /></label><label>Cc<input value={compose.cc} onChange={event => setCompose({ ...compose, cc: event.target.value })} /></label><label>Subject<input value={compose.subject} onChange={event => setCompose({ ...compose, subject: event.target.value })} /></label><label className="email-message-label">Message<textarea placeholder="Write your email here…" value={compose.body} onChange={event => setCompose({ ...compose, body: event.target.value })} /></label><div className="email-actions"><button type="button" className="secondary" onClick={() => void saveDraft()}>Save draft</button><button className="primary-button">Send</button></div><small>You can write and save a draft without AI. Sending happens only after you select Send.</small></form> : !selected ? <div className="empty email-reader-empty"><div><h2>Open an email</h2><p>Select a message to read it, or start a message of your own.</p><button className="primary-button" onClick={startCompose}>Compose</button></div></div> : <>
        <div className="mail-open-toolbar" aria-label="Message actions">
          <button type="button" className="mail-icon-button" aria-label="Back to messages" title="Back to messages" onClick={() => { setSelected(null); setThread(null); setMoreOpen(false) }}><MailIcon name="back" /></button>
          <span className="mail-toolbar-divider" />
          <button type="button" className="mail-icon-button" aria-label="Archive" title="Archive" onClick={() => void messageAction('archive', 'Message archived.', true)}><MailIcon name="archive" /></button>
          <button type="button" className="mail-icon-button" aria-label={selected.unread ? 'Mark as read' : 'Mark as unread'} title={selected.unread ? 'Mark as read' : 'Mark as unread'} onClick={() => void messageAction(selected.unread ? 'read' : 'unread', selected.unread ? 'Marked as read.' : 'Marked as unread.')}><MailIcon name={selected.unread ? 'read' : 'unread'} /></button>
          <div className="mail-more">
            <button type="button" className="mail-icon-button" aria-label="More message actions" title="More message actions" aria-haspopup="menu" aria-expanded={moreOpen} onClick={() => setMoreOpen(value => !value)}><MailIcon name="more" /></button>
            {moreOpen && <div className="mail-more-menu" role="menu">
              <button type="button" role="menuitem" onClick={() => void prepareForward()}><MailIcon name="forward" />Forward</button>
              <button type="button" role="menuitem" disabled={draftingReply} onClick={() => void startManualReply('reply_all')}>Reply all</button>
              <button type="button" role="menuitem" onClick={() => { setMoreOpen(false); void classify() }}><MailIcon name="tag" />Classify message</button>
              <button type="button" role="menuitem" onClick={() => { setTaskSuggestion({ title: selected.classification?.requested_actions?.[0] || selected.classification?.topic || selected.subject, deadline: '', priority: selected.classification?.priority || 'medium', duration_minutes: 60 }); setMoreOpen(false) }}><MailIcon name="task" />Create task suggestion</button>
              <label>AI draft tone<select value={tone} onChange={event => setTone(event.target.value)}><option>professional</option><option>friendly</option><option>formal</option><option>concise</option><option>detailed</option></select></label>
            </div>}
          </div>
        </div>
        <header className="email-open-header">
          <h2>{selected.subject || '(no subject)'}</h2>
          <span className="email-inbox-chip">{folder === 'inbox' ? 'Inbox' : folder}</span>
        </header>
        <div className="email-sender-row">
          <span className="email-sender-avatar" aria-hidden="true">{(selected.from || '?').trim().charAt(0).toUpperCase()}</span>
          <div className="email-sender-copy">
            <strong>{selected.from || 'Unknown sender'}</strong>
            <details><summary>to me</summary><div><span>To: {selected.to || '—'}</span>{selected.cc && <span>Cc: {selected.cc}</span>}</div></details>
          </div>
          <time className="email-open-date" title={formatEmailDate(selected)}>
            {formatEmailDate(selected)}
          </time>
          <button type="button" className={`mail-icon-button mail-star-button ${selected.starred || selected.labels?.includes('STARRED') ? 'active' : ''}`} aria-label={selected.starred || selected.labels?.includes('STARRED') ? 'Unstar message' : 'Star message'} title={selected.starred || selected.labels?.includes('STARRED') ? 'Unstar message' : 'Star message'} onClick={() => void messageAction(selected.starred || selected.labels?.includes('STARRED') ? 'unstar' : 'star', 'Star updated.')}><MailIcon name="star" /></button>
        </div>
        <div className="email-open-content">
        {summary && <div className="ai-summary"><span className="card-label">AI SUMMARY</span><p>{summary}</p></div>}
        {selected.classification && <details className="email-classification"><summary>AI classification · {selected.classification.categories.map((value: string) => value.replace('_', ' ')).join(' · ')}</summary><p>{selected.classification.summary}</p><small>{selected.classification.explanation}</small></details>}
        {taskSuggestion && <form className="email-task-suggestion" onSubmit={taskFromEmail}><span className="card-label">POTENTIAL TASK · REVIEW BEFORE SAVING</span><label>Title<input value={taskSuggestion.title} onChange={event => setTaskSuggestion({ ...taskSuggestion, title: event.target.value })} required /></label><label>Deadline<input type="date" value={taskSuggestion.deadline || ''} onChange={event => setTaskSuggestion({ ...taskSuggestion, deadline: event.target.value || null })} /></label><label>Priority<select value={taskSuggestion.priority} onChange={event => setTaskSuggestion({ ...taskSuggestion, priority: event.target.value })}><option>low</option><option>medium</option><option>high</option></select></label><button className="primary-button">Create Task</button><button type="button" className="secondary" onClick={() => setTaskSuggestion(null)}>Dismiss</button><p>Source email: {selected.subject}</p></form>}
        <div className="email-body">{selected.body_html ? <iframe title="Email HTML content" sandbox="" srcDoc={`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{font:15px/1.65 system-ui;color:#222;overflow-wrap:anywhere;max-width:76ch;margin:0 auto}img{max-width:100%;height:auto}table{max-width:100%;border-collapse:collapse}</style>${selected.body_html}`} /> : <pre>{selected.body_text || selected.snippet}</pre>}</div>
        {!!selected.attachments?.length && <div className="email-attachments"><span className="card-label">ATTACHMENTS</span>{selected.attachments.map((attachment: Email) => <button key={attachment.attachment_id} className="secondary" disabled={attachmentBusy} onClick={() => void downloadAttachment(attachment)}>{attachment.filename} · {Math.ceil(attachment.size / 1024)} KB</button>)}</div>}
        {!!thread?.messages?.length && thread.messages.length > 1 && <div className="email-thread"><span className="card-label">CONVERSATION · {thread.messages.length} MESSAGES</span>{thread.messages.map((item: Email) => <button key={item.id} className={item.id === selected.id ? 'thread-message current' : 'thread-message'} onClick={() => void openMessage(item)}><strong>{item.from}</strong><span>{item.subject}</span><time title={formatEmailDate(item)}>{formatEmailDate(item, true)}</time></button>)}</div>}
        </div>
        <div className="email-primary-actions">
          <button type="button" className="secondary" disabled={draftingReply} onClick={() => void startManualReply()}>{draftingReply ? 'Opening reply…' : 'Reply'}</button>
          <button type="button" className="primary-button" disabled={draftingReply} onClick={() => void draftReply('reply')}>{draftingReply ? 'Drafting…' : 'AI draft'}</button>
          <button type="button" className="secondary" disabled={summarizing} onClick={() => void generateSummary()}>{summarizing ? 'Summarising…' : 'Summarise'}</button>
        </div>
      </>}
    </div>
  </section>
}
