import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowUp, ArrowUpRight, Plus, Folder, File, Download, LogOut, X, ChevronRight, MessageSquare, RefreshCw, Settings, ShieldCheck, UsersRound } from 'lucide-react';
import { AdminPortal } from './AdminPortal';
import { MarkdownMessage } from './MarkdownMessage';
import { attachStream, emptyStream, finalText, reduceStream, upsertMessage, type ConnectionState, type Message, type StreamEvent } from './streamState';
import { HttpError, observeRun, submitRun, type Run } from './runClient';
import { readStored, selectionKey, SubmissionStore, writeStored } from './submissions';
import { fetchDownload, saveDownload } from './download';
import { recoverConversation, recoverNextRun } from './conversationRecovery';
import './style.css';

type Team = { id: string; name: string; created_at: number };
type User = { sub: string; username: string; email: string; team_ids: string[]; teams: Team[]; admin: boolean };
type Agent = { id: string; name: string; status: string; created_at: number; team_id: string; can_access: boolean; security_test_mode: boolean; execution_mode: 'sequential' | 'concurrent'; input_limit_value: number; input_limit_unit: 'tokens' | 'mb'; max_output_tokens: number };
type Conversation = { id: string; created_at: number };
type Artifact = { name: string; directory: boolean; size: number };

const terminalEventTypes = new Set(['complete', 'partial', 'error', 'interrupted']);

async function api<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch('/api' + path, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    signal,
  } : { signal });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: 'Request failed. Please try again.' }));
    throw new Error(typeof detail.detail === 'string' ? detail.detail : 'Please check your input.');
  }
  return response.status === 204 ? undefined as T : response.json();
}

function Mark({ large = false }: { large?: boolean }) {
  return <svg className={large ? 'mark large' : 'mark'} viewBox="0 0 64 64" fill="none" aria-hidden="true">
    <path d="M32 5v54M10 18l44 28M10 46l44-28M19 9l26 46M45 9L19 55" stroke="currentColor" strokeWidth="1.5"/>
    <circle cx="32" cy="32" r="14" fill="var(--paper)" stroke="currentColor" strokeWidth="1.5"/>
    <path d="M25 23v18m14-18v18M25 32h14" stroke="currentColor" strokeWidth="2"/>
  </svg>;
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [availableOnly, setAvailableOnly] = useState(false);
  const [selected, setSelected] = useState<Agent | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversation, setConversation] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [prompt, setPrompt] = useState('');
  const [streamView, setStreamView] = useState(emptyStream);
  const streamViewRef = useRef(streamView);
  const attachment = useRef<{ controller: AbortController | null; generation: number }>({ controller: null, generation: 0 });
  const [busy, setBusy] = useState(false);
  const [recoveryBlocked, setRecoveryBlocked] = useState(false);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [adminMode, setAdminMode] = useState(() => window.location.hash === '#admin');
  const [activeTeam, setActiveTeam] = useState('');
  const [securityTestMode, setSecurityTestMode] = useState(false);
  const [executionMode, setExecutionMode] = useState<'sequential' | 'concurrent'>('sequential');
  const [creationInputLimit, setCreationInputLimit] = useState(0);
  const [creationInputUnit, setCreationInputUnit] = useState<'tokens' | 'mb'>('tokens');
  const [creationMaxOutput, setCreationMaxOutput] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [inputLimit, setInputLimit] = useState(0);
  const [inputUnit, setInputUnit] = useState<'tokens' | 'mb'>('tokens');
  const [maxOutput, setMaxOutput] = useState(0);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState('');
  const [files, setFiles] = useState<Artifact[]>([]);
  const [filePath, setFilePath] = useState('');
  const [downloading, setDownloading] = useState<string | null>(null);
  const downloadController = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const creationId = useRef(crypto.randomUUID());
  const workspaceDirty = useRef(false);
  const [submissions] = useState(() => {
    try { return new SubmissionStore(localStorage); } catch { return new SubmissionStore(); }
  });
  const report = (error: unknown) => setError(error instanceof Error ? error.message : 'Something went wrong.');
  const visibleAgents = availableOnly ? agents.filter(agent => agent.can_access) : agents;

  function stopAttachment() {
    attachment.current.generation += 1;
    attachment.current.controller?.abort();
    attachment.current.controller = null;
  }

  function beginAttachment() {
    stopAttachment();
    const controller = new AbortController();
    attachment.current.controller = controller;
    return { controller, generation: attachment.current.generation };
  }

  useEffect(() => () => stopAttachment(), []);

  useEffect(() => {
    setDownloading(null);
    return () => {
      downloadController.current?.abort();
      downloadController.current = null;
    };
  }, [selected?.id, user?.sub, adminMode]);

  useEffect(() => {
    fetch('/api/me').then(async response => {
      if (response.status === 401) return;
      if (!response.ok) throw new Error('The portal is temporarily unavailable.');
      const current = await response.json();
      setUser(current);
      setAdminMode(current.admin && window.location.hash === '#admin');
      const remembered = localStorage.getItem('agent-sandbox-active-team');
      setActiveTeam(remembered && current.team_ids.includes(remembered) ? remembered : current.team_ids[0]);
    }).catch(report).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!user || adminMode) return;
    let active = true;
    setAgentsLoading(true);
    async function loadWorkspace() {
      if (workspaceDirty.current) {
        const current = await api<User>('/me');
        if (!active) return;
        setUser(current);
        setActiveTeam(previous => current.team_ids.includes(previous) ? previous : current.team_ids[0]);
        workspaceDirty.current = false;
      }
      const list = await api<Agent[]>('/agents');
      if (!active) return;
      setAgents(list);
      const remembered = readStored(selectionKey(user!.sub));
      setSelected(previous => list.find(agent => agent.id === (previous?.id ?? remembered))
        ?? list.find(agent => agent.can_access) ?? list[0] ?? null);
    }
    void loadWorkspace().catch(value => { if (active) report(value); })
      .finally(() => { if (active) setAgentsLoading(false); });
    return () => { active = false; };
  }, [user?.sub, adminMode]);

  useEffect(() => {
    stopAttachment();
    setBusy(false); setRecoveryBlocked(false); setStatus(''); setPrompt(''); setError('');
    setMessages([]); setConversation(null); setFilePath(''); setFiles([]); setConversations([]);
    streamViewRef.current = emptyStream(); setStreamView(streamViewRef.current);
    setInputLimit(selected?.input_limit_value ?? 0);
    setInputUnit(selected?.input_limit_unit ?? 'tokens');
    setMaxOutput(selected?.max_output_tokens ?? 0); setSettingsOpen(false);
    if (user && selected) writeStored(selectionKey(user.sub), selected.id);
    return () => stopAttachment();
  }, [selected?.id, user?.sub, adminMode]);

  useEffect(() => {
    if (adminMode || agentsLoading) return;
    let active = true;
    if (selected?.status === 'ready' && selected.can_access) {
      api<Conversation[]>(`/agents/${selected.id}/conversations`).then(list => {
        if (!active) return;
        setConversations(list);
        const remembered = user && readStored(selectionKey(user.sub, selected.id));
        if (remembered && list.some(item => item.id === remembered)) void openConversation(remembered);
      }).catch(value => { if (active) report(value); });
    }
    return () => { active = false; };
  }, [selected?.id, selected?.status, selected?.can_access, adminMode, agentsLoading]);

  useEffect(() => {
    if (adminMode || agentsLoading || !selected || selected.status !== 'ready' || !selected.can_access) return;
    let active = true;
    api<Artifact[]>(`/agents/${selected.id}/files?path=${encodeURIComponent(filePath)}`)
      .then(list => { if (active) setFiles(list); }).catch(report);
    return () => { active = false; };
  }, [selected?.id, selected?.status, selected?.can_access, filePath, busy, adminMode, agentsLoading]);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, streamView]);

  function updateStream(data: StreamEvent, eventId: number) {
    streamViewRef.current = reduceStream(streamViewRef.current, data, eventId);
    setStreamView(streamViewRef.current);
  }

  function setStreamConnection(runId: string, connection: ConnectionState) {
    streamViewRef.current = attachStream(streamViewRef.current, runId, connection);
    setStreamView(streamViewRef.current);
  }

  async function create(event: FormEvent) {
    event.preventDefault(); setSaving(true); setError('');
    try {
      const agent = await api<Agent>('/agents', {
        id: creationId.current, name: name.trim(), team_id: activeTeam,
        security_test_mode: securityTestMode,
        execution_mode: executionMode,
        input_limit_value: creationInputLimit, input_limit_unit: creationInputUnit,
        max_output_tokens: creationMaxOutput,
      });
      setAgents(list => [agent, ...list.filter(item => item.id !== agent.id)]);
      setSelected(agent); setCreating(false); setName('');
      setSecurityTestMode(false);
      setExecutionMode('sequential');
      setCreationInputLimit(0); setCreationInputUnit('tokens'); setCreationMaxOutput(0);
      creationId.current = crypto.randomUUID();
    } catch (error) { report(error); } finally { setSaving(false); }
  }

  async function saveSettings(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    setSettingsSaving(true); setError('');
    try {
      const updated = await api<Agent>(`/agents/${selected.id}/settings`, {
        input_limit_value: inputLimit, input_limit_unit: inputUnit,
        max_output_tokens: maxOutput,
      });
      setSelected(updated);
      setAgents(items => items.map(item => item.id === updated.id ? updated : item));
      setSettingsOpen(false);
    } catch (error) { report(error); } finally { setSettingsSaving(false); }
  }

  async function attachRun(agentId: string, conversationId: string, run: Run, view: ReturnType<typeof beginAttachment>) {
    const { controller, generation } = view;
    setBusy(true);
    const base = `/api/agents/${agentId}/conversations/${conversationId}`;
    let currentRun: Run | null = run;
    try {
      while (currentRun && !controller.signal.aborted && attachment.current.generation === generation) {
        const observed: Run = currentRun;
        let terminalEvent: StreamEvent | undefined;
        setStreamConnection(observed.id, 'connecting');
        await observeRun({
          base,
          runId: observed.id, signal: controller.signal, after: streamViewRef.current.lastEventId,
          onConnection: connection => {
            setStreamConnection(observed.id, connection);
            setStatus(connection === 'idle' ? 'Working on it' : connection === 'connecting'
              ? 'Connecting to your agent' : 'Reconnecting to your agent');
          },
          onEvent: (data, seq) => {
            updateStream(data, seq);
            if (data.type === 'status' && data.text) setStatus(data.text);
            if (terminalEventTypes.has(data.type)) terminalEvent = data;
          },
          onTerminal: status => {
            const text = finalText(streamViewRef.current, terminalEvent);
            if (text) setMessages(current => upsertMessage(current, {
              role: 'assistant', run_id: observed.id, text, partial: status !== 'complete',
              exit_reason: terminalEvent?.reason || (status === 'complete' ? undefined : status),
            }));
            if (status !== 'complete') setError(terminalEvent?.message ||
              `Agent stopped before completing: ${terminalEvent?.reason || status}`);
            streamViewRef.current = { ...streamViewRef.current, current: '', interim: '', finalChunks: '', connection: 'idle' };
            setStreamView(streamViewRef.current);
          },
        });
        if (controller.signal.aborted || attachment.current.generation !== generation) return;
        const next = await recoverNextRun(base, observed.id, controller.signal);
        if (controller.signal.aborted || attachment.current.generation !== generation) return;
        if (next) setMessages(next.history);
        currentRun = next?.run ?? null;
      }
      if (!controller.signal.aborted && attachment.current.generation === generation) setRecoveryBlocked(false);
    } catch (error) {
      if (!controller.signal.aborted && attachment.current.generation === generation) {
        setRecoveryBlocked(true);
        report(error);
        streamViewRef.current = emptyStream(); setStreamView(streamViewRef.current);
      }
    } finally {
      if (attachment.current.generation === generation) {
        attachment.current.controller = null;
        setBusy(false); setStatus('');
      }
    }
  }

  async function openConversation(id: string) {
    if (!selected || !user) return;
    const agentId = selected.id;
    const view = beginAttachment();
    const { signal } = view.controller;
    setBusy(true); setRecoveryBlocked(true); setStatus('Loading conversation'); setError(''); setMessages([]); setConversation(id);
    streamViewRef.current = emptyStream(); setStreamView(streamViewRef.current);
    setPrompt(submissions.get(user.sub, agentId, id)?.message ?? '');
    writeStored(selectionKey(user.sub, agentId), id);
    try {
      const base = `/api/agents/${agentId}/conversations/${id}`;
      const { history, run } = await recoverConversation(base, signal);
      if (signal.aborted || attachment.current.generation !== view.generation) return;
      setMessages(history);
      if (run) await attachRun(agentId, id, run, view);
      else setRecoveryBlocked(false);
    } catch (error) { if (!signal.aborted) report(error); }
    finally {
      if (attachment.current.generation === view.generation) { setBusy(false); setStatus(''); }
    }
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!selected || !user || !prompt.trim() || busy || recoveryBlocked) return;
    const text = prompt.trim();
    const agentId = selected.id;
    const userId = user.sub;
    const view = beginAttachment();
    const { signal } = view.controller;
    let id = conversation;
    let submitted = false;
    setBusy(true); setError(''); setPrompt('');
    try {
      if (!id) {
        const created = await api<Conversation>(`/agents/${agentId}/conversations`, {}, signal);
        if (signal.aborted) return;
        id = created.id; setConversation(id); setConversations(list => [...list, created]);
        writeStored(selectionKey(userId, agentId), id);
      }
      const submission = submissions.prepare(userId, agentId, id, text);
      submitted = true;
      const run = await submitRun(`/api/agents/${agentId}/conversations/${id}`, submission, signal);
      if (signal.aborted || attachment.current.generation !== view.generation) return;
      submissions.clear(userId, agentId, id);
      setMessages(list => upsertMessage(list, { role: 'user', text, run_id: run.id }));
      await attachRun(agentId, id, run, view);
    } catch (error) {
      // Network errors (including lost response bodies and aborts) retain the same retry key.
      if (!signal.aborted && attachment.current.generation === view.generation) {
        if (id && submitted && error instanceof HttpError && error.permanent) submissions.clear(userId, agentId, id);
        report(error); setPrompt(id ? submissions.get(userId, agentId, id)?.message ?? text : text);
      }
    } finally {
      if (attachment.current.generation === view.generation) { setBusy(false); setStatus(''); }
    }
  }

  function newConversation() {
    stopAttachment(); setBusy(false); setRecoveryBlocked(false); setStatus(''); setError(''); setPrompt('');
    setConversation(null); setMessages([]);
    streamViewRef.current = emptyStream(); setStreamView(streamViewRef.current);
    if (user && selected) writeStored(selectionKey(user.sub, selected.id), null);
  }

  async function download(file: Artifact) {
    if (!selected || file.directory || downloadController.current) return;
    const path = [filePath, file.name].filter(Boolean).join('/');
    const controller = new AbortController();
    downloadController.current = controller;
    setDownloading(path); setError('');
    try {
      const blob = await fetchDownload(`/api/agents/${selected.id}/download?path=${encodeURIComponent(path)}`,
        { signal: controller.signal });
      if (!controller.signal.aborted) saveDownload(blob, file.name);
    } catch (error) { if (!controller.signal.aborted) report(error); }
    finally {
      if (downloadController.current === controller) {
        downloadController.current = null;
        setDownloading(null);
      }
    }
  }

  async function logout() {
    stopAttachment();
    downloadController.current?.abort();
    try { const result = await api<{ logout_url: string }>('/auth/logout', {}); window.location.assign(result.logout_url); }
    catch (error) { report(error); }
  }

  if (loading) return <main className="loading"><Mark/><p>Opening your workspace</p></main>;
  if (!user) return <main className="login">
    <header><Mark/><span>H <small>AGENT WORKSPACE</small></span></header>
    <div className="login-grid"><section><p className="eyebrow">A SPACE FOR WORK THAT LASTS</p>
      <h1>Good work<br/>has a memory.</h1><p className="intro">Create an agent. Give it a purpose. Return to a workspace that remembers its skills, its files, and where you left off.</p>
      <a className="primary login-button" href="/api/auth/login">Enter your workspace <ArrowUpRight size={20}/></a>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="login-notes"><span>01 / PRIVATE BY DESIGN</span><span>02 / BUILT TO PERSIST</span></div>
    </section><div className="orbital"><div className="orbit o1"/><div className="orbit o2"/><div className="orbit o3"/><Mark large/><span className="orbit-label">YOUR IDEAS, IN CONTINUITY</span></div></div>
  </main>;

  return <div className="shell">
    <aside className="sidebar"><div className="brand"><Mark/><span>H<small>YOUR AGENT WORKSPACE</small></span></div>
      <button className="new-agent" onClick={() => setCreating(true)} disabled={busy}><Plus size={17}/> Create agent</button>
      <div className="section-label">AGENT DIRECTORY <span>{visibleAgents.length.toString().padStart(2, '0')}</span></div>
      <button className="agent-availability-toggle" role="switch" aria-checked={availableOnly} aria-controls="agent-directory" onClick={() => setAvailableOnly(current => !current)}><span>Available only</span><span className="toggle-track" aria-hidden="true"/></button>
      <nav id="agent-directory" aria-label="Agents" className="agent-list">{visibleAgents.map((agent, i) => <button key={agent.id} className={`agent ${selected?.id === agent.id ? 'active' : ''}`} onClick={() => { if (agent.id !== selected?.id) { stopAttachment(); setSelected(agent); } }}>
        <span className="agent-symbol">{agent.name.charAt(0).toUpperCase()}</span><span>{agent.name}<small>{agent.can_access ? `Teammate · workspace available${agent.security_test_mode ? ' · test mode' : ''}` : 'No shared team'}</small></span><span className="agent-index">{String(i + 1).padStart(2, '0')}</span>
      </button>)}{availableOnly && !visibleAgents.length && <p className="agent-directory-empty" role="status">No agents are available to your teams.</p>}</nav>
      <div className="sidebar-bottom">{user.admin && <button className={`admin-link ${adminMode ? 'active' : ''}`} onClick={() => { stopAttachment(); setAdminMode(true); window.location.hash = 'admin'; }}><UsersRound size={16}/><span>Admin portal</span></button>}<div className="isolation-note"><ShieldCheck size={16}/><span>{user.teams.length} team{user.teams.length === 1 ? '' : 's'}<br/><small>Sessions require a shared team</small></span></div><div className="user"><span className="avatar">{user.email.charAt(0).toUpperCase()}</span><span title={user.email}>{user.email}</span><button aria-label="Sign out" onClick={logout} disabled={busy}><LogOut size={16}/></button></div></div>
    </aside>
    <main className="main">{adminMode ? <AdminPortal currentUsername={user.username} onChanged={() => { workspaceDirty.current = true; }} onClose={() => { setAgentsLoading(true); setAdminMode(false); history.replaceState(null, '', location.pathname); }}/> : <><header className="workspace-header"><div><p className="eyebrow">AGENT DIRECTORY / {selected?.can_access ? 'TEAMMATE' : selected ? 'NO SHARED TEAM' : 'WELCOME'}</p><h2>{selected?.name ?? 'Make room for your next idea.'}</h2></div>{selected?.can_access && <div className="header-actions"><button className="quiet" disabled={busy || agentsLoading} onClick={() => setSettingsOpen(true)}><Settings size={16}/> Agent settings</button><button className="quiet" disabled={agentsLoading} onClick={newConversation}><Plus size={16}/> New conversation</button></div>}</header>
      {error && <div className="error banner" role="alert"><span>{error}</span><button aria-label="Dismiss error" onClick={() => setError('')}><X size={16}/></button></div>}
      {agentsLoading ? <section className="empty" role="status"><Mark large/><p>Loading agent directory…</p></section> : !selected ? <section className="empty"><Mark large/><p className="eyebrow">YOUR FIRST AGENT STARTS HERE</p><h1>A little help.<br/>A lasting workspace.</h1><p>Create an agent for research, writing, or building.<br/>Its work stays with it, session after session.</p><button className="primary" onClick={() => setCreating(true)}>Create your first agent <Plus size={18}/></button></section>
        : !selected.can_access ? <section className="empty unavailable"><ShieldCheck size={54}/><p className="eyebrow">VISIBLE TO EVERYONE / SESSIONS ARE TEAM-BOUND</p><h1>{selected.name} is not<br/>on your team.</h1><p>You can see this agent in the global directory, but conversations,<br/>files, and its workspace require a shared team.</p></section> : <div className="work-area"><section className="conversation"><div className="messages" aria-live="polite">
          {!messages.length && <div className="conversation-empty"><Mark large/><p className="eyebrow">READY WHEN YOU ARE</p><h1>What shall we<br/>work on?</h1><p>Give {selected.name} a task. Files and learned skills<br/>will be here when you come back.</p><div className="suggestions">{['Create a project plan and save it as a file', 'Show me what is in my workspace'].map(text => <button key={text} onClick={() => setPrompt(text)}>{text}<ArrowUpRight size={15}/></button>)}</div></div>}
          {messages.map((message, i) => <article key={message.run_id ? `${message.run_id}:${message.role}` : i} className={`message ${message.role}${message.partial ? ' partial' : ''}`}><div className="message-label">{message.role === 'user' ? 'YOU' : selected.name.toUpperCase()}</div><MarkdownMessage>{message.text}</MarkdownMessage>{message.partial && <small className="partial-note">Stopped: {message.exit_reason || 'incomplete'}</small>}</article>)}
          {busy && streamView.interim && !messages.some(message => message.role === 'assistant' && message.run_id === streamView.runId) && <aside className="interim-update"><div className="message-label">LATEST WORK UPDATE</div><MarkdownMessage>{streamView.interim}</MarkdownMessage></aside>}
          {streamView.current && !messages.some(message => message.role === 'assistant' && message.run_id === streamView.runId) && <article className="message assistant streaming"><div className="message-label">{selected.name.toUpperCase()}</div><MarkdownMessage>{streamView.current}</MarkdownMessage></article>}
          <div ref={bottom}/></div>
          <div className="composer-area">{busy && <div className="working" role="status"><span/>{status || 'Working on it'}</div>}{recoveryBlocked && !busy && <p role="status">Reopen this conversation to retry recovery, or start a new conversation.</p>}<form className="composer" onSubmit={send}><textarea aria-label="Message your agent" value={prompt} onChange={event => setPrompt(event.target.value)} placeholder={`Message ${selected.name}…`} disabled={busy || recoveryBlocked || selected.status !== 'ready'} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); if (prompt.trim()) event.currentTarget.form?.requestSubmit(); } }}/><button className="send" type="submit" aria-label="Send message" disabled={busy || recoveryBlocked || !prompt.trim() || selected.status !== 'ready'}><ArrowUp size={21}/></button></form><p className="composer-note">Enter to send · Shift + Enter for a new line <span>INPUT {selected.input_limit_value ? `${selected.input_limit_value.toLocaleString()} ${selected.input_limit_unit.toUpperCase()}` : 'UNLIMITED'} · OUTPUT {selected.max_output_tokens ? selected.max_output_tokens.toLocaleString() : 'UNSET'}</span></p></div>
        </section><aside className="details"><div className="section-label">WORKSPACE FILES <Folder size={15}/></div><div className="file-toolbar"><button onClick={() => setFilePath(filePath.split('/').slice(0, -1).join('/'))} disabled={!filePath}>/ {filePath || 'workspace'}</button><button aria-label="Refresh files" onClick={() => api<Artifact[]>(`/agents/${selected.id}/files?path=${encodeURIComponent(filePath)}`).then(setFiles).catch(report)}><RefreshCw size={13}/></button></div>{files.length ? files.map(file => <div className="file" key={file.name}>{file.directory ? <Folder size={15}/> : <File size={15}/>}<button title={file.name} onClick={() => file.directory && setFilePath([filePath, file.name].filter(Boolean).join('/'))} disabled={!file.directory}>{file.name}</button>{file.directory ? <ChevronRight size={13}/> : <button className="file-download" aria-label={`Download ${file.name}`} aria-busy={downloading === [filePath, file.name].filter(Boolean).join('/')} disabled={downloading !== null} onClick={() => void download(file)}><Download size={14}/></button>}</div>) : <p className="details-empty">A clean slate.<br/>Your agent’s files will appear here.</p>}
          <div className="section-label history-label">CONVERSATIONS <MessageSquare size={15}/></div>{conversations.map((item, i) => <button className={`history ${item.id === conversation ? 'chosen' : ''}`} key={item.id} onClick={() => openConversation(item.id)}><span>Conversation {i + 1}</span><small>{new Date(item.created_at * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</small></button>)}
          <div className="workspace-footnote"><span className="dot"/> Workspace persists across sessions<br/>{selected.execution_mode === 'concurrent' ? 'Concurrent execution · no agent-wide lock' : 'Sequential execution · locked'}</div></aside></div>}
    </>}</main>
    {creating && <dialog ref={node => { if (node && !node.open) node.showModal(); }} onCancel={() => !saving && setCreating(false)}>
      <form onSubmit={create}>
        <div className="modal-top"><p className="eyebrow">A NEW COLLABORATOR</p><button type="button" aria-label="Close" disabled={saving} onClick={() => setCreating(false)}><X size={18}/></button></div>
        <h2>Give your agent a name.</h2><p>An agent belongs to exactly one team. Everyone on that team can use its workspace.</p>
        <label htmlFor="agent-name">AGENT NAME</label><input autoFocus id="agent-name" value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder="e.g. Research companion" required disabled={saving}/>
        <label htmlFor="agent-team">TEAM</label><select id="agent-team" value={activeTeam} onChange={event => { setActiveTeam(event.target.value); localStorage.setItem('agent-sandbox-active-team', event.target.value); }} required disabled={saving}>{user.teams.map(team => <option key={team.id} value={team.id}>{team.name}</option>)}</select>
        <label htmlFor="agent-execution-mode">EXECUTION MODE</label>
        <select id="agent-execution-mode" value={executionMode} onChange={event => setExecutionMode(event.target.value as 'sequential' | 'concurrent')} disabled={saving} aria-describedby="agent-execution-help">
          <option value="sequential">Sequential (lock)</option>
          <option value="concurrent">Concurrent (no agent-wide lock)</option>
        </select>
        <p id="agent-execution-help" className="field-help">{executionMode === 'sequential'
          ? 'One conversation runs at a time for this agent.'
          : 'Different conversations can run at the same time and may edit the same workspace or skills.'} Conversations stay private. This setting is fixed at creation.</p>
        <div className="limit-grid"><label htmlFor="agent-input-limit">INPUT LIMIT<input id="agent-input-limit" type="number" min="0" step="1" value={creationInputLimit} onChange={event => setCreationInputLimit(Math.max(0, Number(event.target.value) || 0))}/></label><label htmlFor="agent-input-unit">UNIT<select id="agent-input-unit" value={creationInputUnit} onChange={event => setCreationInputUnit(event.target.value as 'tokens' | 'mb')}><option value="tokens">Tokens</option><option value="mb">MB</option></select></label></div>
        <p className="field-help">0 disables the application input-size check.</p><label htmlFor="agent-max-output">MAX OUTPUT TOKENS</label><input id="agent-max-output" type="number" min="0" step="1" value={creationMaxOutput} onChange={event => setCreationMaxOutput(Math.max(0, Number(event.target.value) || 0))}/><p className="field-help">0 leaves Hermes max_tokens unset.</p>
        <label className="test-mode-option"><input type="checkbox" checked={securityTestMode} onChange={event => setSecurityTestMode(event.target.checked)} disabled={saving}/><span>Security test mode<small>Injects SECURITY_TEST_MODE=true into this agent's sandbox. This setting is fixed at creation.</small></span></label>
        <button className="primary" disabled={saving || !name.trim() || !activeTeam}>{saving ? 'Preparing workspace…' : 'Create agent'}<ArrowUpRight size={18}/></button>{error && <p className="error" role="alert">{error}</p>}
      </form>
    </dialog>}
    {settingsOpen && selected && <dialog ref={node => { if (node && !node.open) node.showModal(); }} onCancel={() => !settingsSaving && setSettingsOpen(false)}><form onSubmit={saveSettings}><div className="modal-top"><p className="eyebrow">AGENT SETTINGS</p><button type="button" aria-label="Close" disabled={settingsSaving} onClick={() => setSettingsOpen(false)}><X size={18}/></button></div><h2>{selected.name}</h2><p>Changing limits restarts this conversation's Hermes worker from its latest checkpoint on the next turn.</p><div className="limit-grid"><label htmlFor="settings-input-limit">INPUT LIMIT<input autoFocus id="settings-input-limit" type="number" min="0" step="1" value={inputLimit} onChange={event => setInputLimit(Math.max(0, Number(event.target.value) || 0))}/></label><label htmlFor="settings-input-unit">UNIT<select id="settings-input-unit" value={inputUnit} onChange={event => setInputUnit(event.target.value as 'tokens' | 'mb')}><option value="tokens">Tokens</option><option value="mb">MB</option></select></label></div><p className="field-help">Limits each new user message using a rough token estimate or UTF-8 size. 0 disables the check.</p><label htmlFor="settings-max-output">MAX OUTPUT TOKENS</label><input id="settings-max-output" type="number" min="0" step="1" value={maxOutput} onChange={event => setMaxOutput(Math.max(0, Number(event.target.value) || 0))}/><p className="field-help">0 passes max_tokens=None to Hermes.</p><button className="primary" disabled={settingsSaving}>{settingsSaving ? 'Saving…' : 'Save settings'}<Settings size={18}/></button></form></dialog>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
