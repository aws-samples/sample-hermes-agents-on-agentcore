import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { ArrowLeft, Plus, RefreshCw, Save, ShieldCheck, Trash2, UserPlus, UsersRound } from 'lucide-react';

type Team = { id: string; name: string; created_at: number };
type ManagedUser = { username: string; sub: string; email: string; team_ids: string[]; enabled: boolean; status: string; admin: boolean; kind: 'human' | 'agent'; agent_id?: string; agent_name?: string; created_by?: string; created_by_email?: string; security_test_mode?: boolean };

async function request<T>(path: string, method = 'GET', body?: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch('/api/admin' + path, {
    method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined, signal,
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({ detail: 'Request failed' }));
    throw new Error(result.detail ?? 'Request failed');
  }
  return response.status === 204 ? undefined as T : response.json();
}

const sortTeams = (items: Team[]) => [...items].sort((a, b) => a.name.localeCompare(b.name));
const sortUsers = (items: ManagedUser[]) => [...items].sort((a, b) => a.email.localeCompare(b.email));
const errorMessage = (value: unknown) => value instanceof Error ? value.message : 'Request failed';

export function AdminPortal({ currentUsername, onClose, onChanged }: {
  currentUsername: string; onClose: () => void; onChanged: () => void;
}) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [tab, setTab] = useState<'users' | 'teams'>('users');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [teamsLoading, setTeamsLoading] = useState(true);
  const [usersLoading, setUsersLoading] = useState(true);
  const [teamsError, setTeamsError] = useState('');
  const [usersError, setUsersError] = useState('');
  const [pending, setPending] = useState(false);
  const pendingAction = useRef(false);
  const teamsRequest = useRef<AbortController | null>(null);
  const usersRequest = useRef<AbortController | null>(null);
  const [teamName, setTeamName] = useState('');
  const [email, setEmail] = useState('');
  const [newUserTeams, setNewUserTeams] = useState<string[]>([]);
  const teamId = useRef(crypto.randomUUID());

  async function loadTeams() {
    if (teamsRequest.current) return;
    const controller = new AbortController();
    teamsRequest.current = controller;
    setTeamsLoading(true); setTeamsError('');
    try {
      const nextTeams = await request<Team[]>('/teams', 'GET', undefined, controller.signal);
      if (controller.signal.aborted) return;
      setTeams(sortTeams(nextTeams));
      setNewUserTeams(current => {
        const remaining = current.filter(id => nextTeams.some(team => team.id === id));
        return remaining.length ? remaining : nextTeams.slice(0, 1).map(team => team.id);
      });
    } catch (value) {
      if (!controller.signal.aborted) setTeamsError(errorMessage(value));
    } finally {
      if (!controller.signal.aborted) { teamsRequest.current = null; setTeamsLoading(false); }
    }
  }

  async function loadUsers() {
    if (usersRequest.current) return;
    const controller = new AbortController();
    usersRequest.current = controller;
    setUsersLoading(true); setUsersError('');
    try {
      const nextUsers = await request<ManagedUser[]>('/users', 'GET', undefined, controller.signal);
      if (!controller.signal.aborted) setUsers(sortUsers(nextUsers));
    } catch (value) {
      if (!controller.signal.aborted) setUsersError(errorMessage(value));
    } finally {
      if (!controller.signal.aborted) { usersRequest.current = null; setUsersLoading(false); }
    }
  }

  useEffect(() => {
    void loadTeams(); void loadUsers();
    return () => {
      teamsRequest.current?.abort(); usersRequest.current?.abort();
      teamsRequest.current = null; usersRequest.current = null;
    };
  }, []);

  const run = async (action: () => Promise<unknown>, message: string) => {
    if (pendingAction.current) return;
    pendingAction.current = true;
    setPending(true); setError(''); setNotice('');
    try { await action(); setNotice(message); }
    catch (value) { setError(errorMessage(value)); }
    finally {
      // Even a failed multi-step mutation may have changed membership server-side.
      onChanged(); pendingAction.current = false; setPending(false);
    }
  };
  const selected = (event: ChangeEvent<HTMLSelectElement>) =>
    [...event.target.selectedOptions].map(option => option.value);
  const edit = (username: string, values: Partial<ManagedUser>) =>
    setUsers(items => items.map(item => item.username === username ? { ...item, ...values } : item));
  const saveTeam = (team: Team) => setTeams(items => sortTeams([
    ...items.filter(item => item.id !== team.id), team,
  ]));

  function addTeam(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      const team = await request<Team>('/teams', 'POST', { id: teamId.current, name: teamName.trim() });
      saveTeam(team);
      setNewUserTeams(current => current.length ? current : [team.id]);
      teamId.current = crypto.randomUUID(); setTeamName('');
    }, 'Team created.');
  }
  function invite(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      const user = await request<ManagedUser>('/users', 'POST', {
        email: email.trim(), team_ids: newUserTeams, admin: false,
      });
      setUsers(items => sortUsers([...items.filter(item => item.username !== user.username), user]));
      setEmail('');
    }, 'Invitation sent by Cognito.');
  }
  function saveUser(user: ManagedUser) {
    const transfer = user.kind === 'agent';
    if (transfer && !confirm('Transfer this agent? Its shared workspace and skills become accessible to the new team. Conversations remain private to their owners, who must still share the agent’s team.')) return;
    void run(async () => {
      const updated = await request<ManagedUser>(`/users/${encodeURIComponent(user.username)}`, 'POST', {
        team_ids: user.team_ids, admin: user.admin, enabled: user.enabled, confirm_agent_transfer: transfer,
      });
      // Mutation responses contain live account fields; retain the agent's display metadata.
      edit(user.username, updated);
    }, 'Identity updated.');
  }
  function deleteUser(user: ManagedUser) {
    if (!confirm(`Delete ${user.email}? Team agents remain.`)) return;
    void run(async () => {
      await request(`/users/${encodeURIComponent(user.username)}`, 'DELETE');
      setUsers(items => items.filter(item => item.username !== user.username));
    }, 'User deleted.');
  }

  const loading = tab === 'users' ? usersLoading : teamsLoading;
  const loadError = tab === 'users' ? usersError : teamsError;
  return <section className="admin-portal">
    <header className="admin-header"><div><button className="back" disabled={pending} onClick={onClose}><ArrowLeft size={16}/> Back to workspace</button><p className="eyebrow">ADMINISTRATION / COGNITO</p><h1>Teams without<br/>security bridges.</h1></div><ShieldCheck size={48}/></header>
    <nav className="admin-tabs">
      <button className={tab === 'users' ? 'active' : ''} onClick={() => setTab('users')}>Identities <span>{usersLoading ? '…' : users.length}</span></button>
      <button className={tab === 'teams' ? 'active' : ''} onClick={() => setTab('teams')}>Teams <span>{teamsLoading ? '…' : teams.length}</span></button>
      <button className="admin-refresh" disabled={loading || pending} onClick={() => void (tab === 'users' ? loadUsers() : loadTeams())}><RefreshCw size={13}/> Refresh {tab === 'users' ? 'identities' : 'teams'}</button>
    </nav>
    {error && <p className="error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
    {loadError && <p className="error" role="alert">{loadError}</p>}
    {tab === 'users' && teamsError && <p className="error" role="alert">Teams could not load: {teamsError}. Retry from the Teams tab.</p>}
    {loading && <p className="notice" role="status">Loading {tab === 'users' ? 'identities' : 'teams'}…</p>}
    {pending && <p className="notice" role="status">Saving changes…</p>}
    <fieldset className="admin-controls" disabled={pending || loading || teamsLoading || !!teamsError || !!loadError}>
      {tab === 'users' ? <div className="admin-content">
        <form className="admin-create" onSubmit={invite}>
          <div><UserPlus size={20}/><h2>Invite a teammate</h2><p>Humans can belong to several teams. Cognito emails a temporary password.</p></div>
          <label>Email<input type="email" value={email} onChange={event => setEmail(event.target.value)} required placeholder="name@company.com"/></label>
          <label>Teams<select multiple value={newUserTeams} onChange={event => setNewUserTeams(selected(event))} required>{teams.map(team => <option key={team.id} value={team.id}>{team.name}</option>)}</select><small>Hold Command/Ctrl to choose multiple.</small></label>
          <button className="primary" disabled={!teams.length || !newUserTeams.length}>Send invitation</button>
        </form>
        <div className="admin-list" aria-busy={usersLoading}>
          <div className="admin-list-head"><UsersRound size={17}/><span>COGNITO IDENTITIES</span><span>TEAM MEMBERSHIP</span><span>ACCESS</span><span/></div>
          {!usersLoading && !usersError && !users.length && <p className="notice">No identities yet.</p>}
          {users.map(user => <div className="admin-user" key={user.username}>
            <div className="identity-summary"><strong>{user.kind === 'agent' ? user.agent_name || 'Unmapped agent' : user.email || user.username}</strong><small>{user.kind} · {user.status.toLowerCase().replaceAll('_', ' ')}{user.security_test_mode ? ' · security test mode' : ''}{user.username === currentUsername ? ' · you' : ''}</small>{user.kind === 'agent' && <><code title="Cognito username">{user.username}</code><small>Agent ID: {user.agent_id || 'mapping unavailable'}</small><small>Created by: {user.created_by_email || user.created_by || 'unknown'}</small></>}</div>
            <select multiple={user.kind === 'human'} aria-label={`Teams for ${user.agent_name || user.email || user.username}`} value={user.team_ids} onChange={event => edit(user.username, { team_ids: selected(event) })}>{teams.map(team => <option key={team.id} value={team.id}>{team.name}</option>)}</select>
            <div className="access-checks"><label><input type="checkbox" checked={user.admin} disabled={user.kind === 'agent' || user.username === currentUsername} onChange={event => edit(user.username, { admin: event.target.checked })}/> Admin</label><label><input type="checkbox" checked={user.enabled} disabled={user.username === currentUsername} onChange={event => edit(user.username, { enabled: event.target.checked })}/> Enabled</label></div>
            <div className="row-actions"><button aria-label={`Save ${user.agent_name || user.email || user.username}`} disabled={!user.team_ids.length || (user.kind === 'agent' && user.team_ids.length !== 1)} onClick={() => saveUser(user)}><Save size={15}/></button><button className="danger" aria-label={`Delete ${user.email || user.username}`} disabled={user.kind === 'agent' || user.username === currentUsername} onClick={() => deleteUser(user)}><Trash2 size={15}/></button></div>
          </div>)}
        </div>
      </div> : <div className="admin-content">
        <form className="admin-create" onSubmit={addTeam}><div><Plus size={20}/><h2>Create a team</h2><p>Each agent has one team. Humans may join several.</p></div><label>Team name<input value={teamName} onChange={event => setTeamName(event.target.value)} required placeholder="e.g. Product studio"/></label><button className="primary" disabled={!teamName.trim()}>Create team</button></form>
        <div className="team-grid" aria-busy={teamsLoading}>
          {!teamsLoading && !teamsError && !teams.length && <p className="notice">No teams yet.</p>}
          {teams.map(team => <TeamCard team={team} key={team.id} onRun={run} onSave={saveTeam} onDelete={() => {
            setTeams(items => items.filter(item => item.id !== team.id));
            setNewUserTeams(ids => ids.filter(id => id !== team.id));
          }}/>)}</div>
      </div>}
    </fieldset>
  </section>;
}

function TeamCard({ team, onRun, onSave, onDelete }: {
  team: Team; onRun: (action: () => Promise<unknown>, message: string) => Promise<void>;
  onSave: (team: Team) => void; onDelete: () => void;
}) {
  const [name, setName] = useState(team.name);
  useEffect(() => { setName(team.name); }, [team.name]);
  return <article className="team-card"><div className="team-monogram">{team.name.charAt(0).toUpperCase()}</div><label>TEAM NAME<input value={name} onChange={event => setName(event.target.value)} maxLength={80}/></label><small>{team.id}</small><div>
    <button disabled={!name.trim()} onClick={() => void onRun(async () => {
      const updated = await request<Team>(`/teams/${team.id}`, 'POST', { name: name.trim() });
      onSave(updated); setName(updated.name);
    }, 'Team renamed.')}><Save size={14}/> Save</button>
    <button className="danger" aria-label={`Delete ${team.name}`} onClick={() => confirm(`Delete ${team.name}?`) && void onRun(async () => {
      await request(`/teams/${team.id}`, 'DELETE'); onDelete();
    }, 'Team deleted.')}><Trash2 size={14}/></button>
  </div></article>;
}
