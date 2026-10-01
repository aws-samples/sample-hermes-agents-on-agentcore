import base64
import hashlib
import os
import secrets
import stat
import time
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.service import agent_limits, services, token_hash
from common.execution import ExecutionMode, execution_mode
from common.runs import Conflict, public_run, run_store
from common.security import directory, read_file

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
COOKIE = '__Host-agent-sandbox-session'
LOGIN_COOKIE = '__Host-agent-sandbox-login'


@app.middleware('http')
async def protections(request: Request, call_next):
    if request.method in {'POST', 'PUT', 'DELETE', 'PATCH'}:
        native_bearer = (request.headers.get('authorization', '').startswith('Bearer ')
                         and not request.cookies.get(COOKIE) and not request.headers.get('origin'))
        if not native_bearer and request.headers.get('origin') != os.environ.get('PUBLIC_URL'):
            return Response('Invalid origin', status_code=403)
        if (not request.url.path.endswith('/chat')
                and int(request.headers.get('content-length', '0')) > 32000):
            return Response('Request too large', status_code=413)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.get('/api/health')
def health():
    return {'status': 'healthy'}


def current_user(request: Request):
    svc = services()
    authorization = getattr(request, 'headers', {}).get('authorization', '')
    if authorization.startswith('Bearer '):
        try:
            try:
                claims = svc.verifier.verify(authorization[7:])
            except jwt.PyJWTError:
                if not getattr(svc, 'mobile_verifier', None):
                    raise
                claims = svc.mobile_verifier.verify(authorization[7:])
            user = {'sub': claims['sub'], 'username': claims['cognito:username'],
                    'email': claims.get('email', '')}
        except (KeyError, ValueError, jwt.PyJWTError):
            raise HTTPException(401, 'Invalid mobile identity') from None
    else:
        token = request.cookies.get(COOKIE, '')
        if not token:
            raise HTTPException(401, 'Sign in required')
        user = svc.get('SESSION#' + token_hash(token), 'SESSION')
        if not user or int(user['expires']) <= time.time():
            raise HTTPException(401, 'Session expired')
    try:
        membership = svc.membership(user['username'])
    except services().cognito.exceptions.UserNotFoundException:
        raise HTTPException(401, 'Account no longer exists') from None
    # Usernames can be reused after deletion; sessions/tokens belong to the original subject.
    if not user.get('sub') or membership.get('sub') != user['sub']:
        raise HTTPException(401, 'Account identity changed; sign in again')
    if not membership['enabled'] or 'Humans' not in membership['groups']:
        raise HTTPException(401, 'Account is disabled')
    teams = [services().team(team_id) for team_id in membership['team_ids']]
    if not teams or any(not team for team in teams):
        raise HTTPException(403, 'Ask an administrator to assign your team')
    return {**user, **membership, 'teams': teams, 'admin': 'Admins' in membership['groups']}


@app.get('/api/me')
def me(user=Depends(current_user)):
    return {'sub': user['sub'], 'username': user['username'], 'email': user.get('email', ''),
            'team_ids': user['team_ids'], 'teams': [public_team(team) for team in user['teams']],
            'admin': user['admin']}


@app.get('/api/auth/login')
def login():
    svc = services()
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    svc.put('LOGIN#' + token_hash(state), 'LOGIN', verifier=verifier, expires=int(time.time()) + 600)
    url = svc.domain + '/oauth2/authorize?' + urlencode({
        'response_type': 'code', 'client_id': svc.human_client, 'scope': 'openid email profile',
        'redirect_uri': svc.origin + '/api/auth/callback', 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256',
    })
    response = RedirectResponse(url)
    response.set_cookie(LOGIN_COOKIE, state, secure=True, httponly=True, samesite='lax', max_age=600)
    return response


@app.get('/api/auth/callback')
def callback(request: Request, code: str = '', state: str = ''):
    svc = services()
    if not state or not secrets.compare_digest(state, request.cookies.get(LOGIN_COOKIE, '')):
        raise HTTPException(400, 'Invalid login state')
    key = {'pk': 'LOGIN#' + token_hash(state), 'sk': 'LOGIN'}
    stored = svc.table.delete_item(Key=key, ReturnValues='ALL_OLD').get('Attributes')
    if not stored or int(stored['expires']) <= time.time():
        raise HTTPException(400, 'Login expired')
    result = httpx.post(svc.domain + '/oauth2/token', data={
        'grant_type': 'authorization_code', 'client_id': svc.human_client,
        'code': code, 'code_verifier': stored['verifier'],
        'redirect_uri': svc.origin + '/api/auth/callback',
    }, timeout=20)
    if result.status_code != 200:
        raise HTTPException(401, 'Authentication failed')
    try:
        claims = svc.verifier.verify(result.json()['id_token'])
    except (ValueError, KeyError, jwt.PyJWTError):
        raise HTTPException(403, 'A human portal account is required') from None
    session = secrets.token_urlsafe(48)
    svc.put('SESSION#' + token_hash(session), 'SESSION', sub=claims['sub'],
            username=claims['cognito:username'], email=claims.get('email', ''),
            team_ids=claims['team_ids'], expires=int(time.time()) + 8 * 3600)
    response = RedirectResponse(svc.origin)
    response.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True)
    response.set_cookie(COOKIE, session, secure=True, httponly=True, samesite='lax', max_age=8 * 3600)
    return response


@app.post('/api/auth/logout')
def logout(request: Request):
    svc = services()
    svc.table.delete_item(Key={'pk': 'SESSION#' + token_hash(request.cookies.get(COOKIE, '')), 'sk': 'SESSION'})
    response = JSONResponse({'logout_url': svc.domain + '/logout?' + urlencode({
        'client_id': svc.human_client, 'logout_uri': svc.origin,
    })})
    response.delete_cookie(COOKIE, secure=True, httponly=True)
    return response


def public_agent(agent, user=None):
    result = {key: agent[key] for key in ('id', 'name', 'status', 'created_at', 'team_id')}
    result['security_test_mode'] = bool(agent.get('security_test_mode', False))
    result['execution_mode'] = execution_mode(agent)
    result.update(agent_limits(agent))
    result['can_access'] = user is not None and agent['team_id'] in user['team_ids']
    return result


def owned_agent(user, agent_id):
    agent = services().agent(str(agent_id))
    if not agent or agent['team_id'] not in user['team_ids']:
        raise HTTPException(404, 'Agent not found')
    return agent


class CreateAgent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: UUID
    name: str = Field(min_length=1, max_length=80)
    team_id: UUID
    security_test_mode: bool = False
    execution_mode: ExecutionMode = 'sequential'
    input_limit_value: int = Field(default=0, ge=0)
    input_limit_unit: str = Field(default='tokens', pattern=r'^(tokens|mb)$')
    max_output_tokens: int = Field(default=0, ge=0)


@app.get('/api/agents')
def agents(user=Depends(current_user)):
    return [public_agent(item, user) for item in services().agents()]


@app.post('/api/agents')
def create_agent(payload: CreateAgent, user=Depends(current_user)):
    try:
        team_id = str(payload.team_id)
        if team_id not in user['team_ids']:
            raise HTTPException(403, 'You can only create an agent in one of your teams')
        return public_agent(services().provision(
            team_id, user['sub'], str(payload.id), payload.name.strip(),
            payload.security_test_mode, payload.input_limit_value,
            payload.input_limit_unit, payload.max_output_tokens,
            execution_mode=payload.execution_mode), user)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@app.get('/api/agents/{agent_id}/conversations')
def conversations(agent_id: UUID, user=Depends(current_user)):
    owned_agent(user, agent_id)
    return [{k: item[k] for k in ('id', 'created_at')} for item in services().query(
        'AGENT#' + str(agent_id), 'CONV#') if item.get('owner_sub') == user['sub']]


@app.post('/api/agents/{agent_id}/conversations')
def create_conversation(agent_id: UUID, user=Depends(current_user)):
    agent = owned_agent(user, agent_id)
    if agent['status'] != 'ready':
        raise HTTPException(409, 'Agent is not ready')
    conversation_id = str(uuid4())
    runtime_session_id = str(uuid4())
    created = int(time.time())
    services().put('AGENT#' + str(agent_id), 'CONV#' + conversation_id,
                   id=conversation_id, agent_id=str(agent_id), created_at=created,
                   runtime_session_id=runtime_session_id, owner_sub=user['sub'])
    return {'id': conversation_id, 'created_at': created}


def owned_conversation(user, agent_id, conversation_id):
    agent = owned_agent(user, agent_id)
    conversation = services().conversation(str(agent_id), str(conversation_id))
    if not conversation or conversation.get('owner_sub') != user['sub']:
        raise HTTPException(404, 'Conversation not found')
    return agent, conversation


@app.get('/api/agents/{agent_id}/conversations/{conversation_id}/messages')
def messages(agent_id: UUID, conversation_id: UUID, cursor: str | None = None,
             user=Depends(current_user)):
    owned_conversation(user, agent_id, conversation_id)
    try:
        return services().history_page(agent_id, conversation_id, cursor)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except OverflowError as error:
        raise HTTPException(413, str(error)) from None


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_limit_value: int = Field(ge=0)
    input_limit_unit: str = Field(pattern=r'^(tokens|mb)$')
    max_output_tokens: int = Field(ge=0)


@app.post('/api/agents/{agent_id}/settings')
def update_agent_settings(agent_id: UUID, payload: AgentSettings,
                          user=Depends(current_user)):
    agent = owned_agent(user, agent_id)
    try:
        return public_agent(services().update_agent_limits(
            agent, payload.input_limit_value, payload.input_limit_unit,
            payload.max_output_tokens), user)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


class Chat(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=16000)


@app.post('/api/agents/{agent_id}/conversations/{conversation_id}/runs')
def create_run(agent_id: UUID, conversation_id: UUID, payload: Chat, request: Request,
               user=Depends(current_user)):
    agent, conversation = owned_conversation(user, agent_id, conversation_id)
    key = request.headers.get('idempotency-key', '')
    if not key or len(key) > 128:
        raise HTTPException(400, 'Idempotency-Key required')
    try:
        return public_run(run_store().create(agent, conversation, user, key, payload.message))
    except Conflict as error:
        raise HTTPException(409, str(error)) from None


@app.get('/api/agents/{agent_id}/conversations/{conversation_id}/runs/active')
def get_active_run(agent_id: UUID, conversation_id: UUID, user=Depends(current_user)):
    _, conversation = owned_conversation(user, agent_id, conversation_id)
    run = run_store().run(conversation['latest_run_id']) if conversation.get('latest_run_id') else None
    return (public_run(run) if run and run.get('creator_sub') == user['sub']
            and int(run['expires']) > time.time() else None)


def owned_run(user, agent_id, conversation_id, run_id):
    owned_conversation(user, agent_id, conversation_id)
    run = run_store().run(str(run_id))
    if (not run or run.get('creator_sub') != user['sub'] or run['agent_id'] != str(agent_id)
            or run['conversation_id'] != str(conversation_id)):
        raise HTTPException(404, 'Run not found')
    if int(run['expires']) <= time.time():
        raise HTTPException(410, 'Run replay expired; conversation history remains available')
    return run


@app.get('/api/agents/{agent_id}/conversations/{conversation_id}/runs/{run_id}')
def get_run(agent_id: UUID, conversation_id: UUID, run_id: UUID, user=Depends(current_user)):
    return public_run(owned_run(user, agent_id, conversation_id, run_id))


@app.get('/api/agents/{agent_id}/conversations/{conversation_id}/runs/{run_id}/events')
def run_events(agent_id: UUID, conversation_id: UUID, run_id: UUID, after: int = 0,
               user=Depends(current_user)):
    run = owned_run(user, agent_id, conversation_id, run_id)
    try:
        return run_store().page(run, after)
    except LookupError as error:
        raise HTTPException(410, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.post('/api/agents/{agent_id}/conversations/{conversation_id}/runs/{run_id}/subscription')
def subscription(agent_id: UUID, conversation_id: UUID, run_id: UUID, user=Depends(current_user)):
    run = owned_run(user, agent_id, conversation_id, run_id)
    token = secrets.token_urlsafe(48)
    expires = int(time.time()) + 120
    channel = '/runs/' + run['id']
    run_store().table.put_item(Item={
        'pk': 'TICKET#' + token_hash(token), 'sk': 'META', 'expires': expires,
        'username': user['username'], 'sub': user['sub'], 'agent_id': str(agent_id),
        'conversation_id': str(conversation_id), 'run_id': str(run_id),
        'channel': channel,
    })
    return {'url': f"wss://{os.environ['EVENTS_REALTIME_DOMAIN']}/event/realtime",
            'host': os.environ['EVENTS_HTTP_DOMAIN'], 'channel': channel,
            'token': token, 'expires': expires}


@app.post('/api/agents/{agent_id}/conversations/{conversation_id}/chat')
def chat(agent_id: UUID, conversation_id: UUID, payload: Chat, user=Depends(current_user)):
    owned_conversation(user, agent_id, conversation_id)
    raise HTTPException(410, 'Use POST /runs with Idempotency-Key and AppSync subscriptions')


def path_parts(path: str):
    if len(path) > 1024 or path.startswith('/'):
        raise HTTPException(400, 'Invalid artifact path')
    parts = path.split('/') if path else []
    if any(part in {'', '.', '..'} or '\\' in part or '\0' in part for part in parts):
        raise HTTPException(400, 'Invalid artifact path')
    return parts


@app.get('/api/agents/{agent_id}/files')
def files(agent_id: UUID, path: str = '', user=Depends(current_user)):
    agent = owned_agent(user, agent_id)
    try:
        with directory(services().root, agent['sub'], 'workspace', *path_parts(path)) as fd:
            result = []
            with os.scandir(fd) as entries:
                for item in entries:
                    if len(result) >= 1000:
                        break
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
                        result.append({'name': item.name, 'directory': stat.S_ISDIR(info.st_mode),
                                       'size': info.st_size})
            return sorted(result, key=lambda item: (not item['directory'], item['name']))
    except (OSError, ValueError):
        raise HTTPException(404, 'Directory not found') from None


@app.get('/api/agents/{agent_id}/download')
def download(agent_id: UUID, path: str, request: Request, user=Depends(current_user)):
    agent = owned_agent(user, agent_id)
    parts = path_parts(path)
    if not parts:
        raise HTTPException(400, 'File required')
    try:
        with directory(services().root, agent['sub'], 'workspace', *parts[:-1]) as fd:
            data = read_file(fd, parts[-1])
    except (OSError, ValueError):
        raise HTTPException(404, 'File unavailable or exceeds 32 MiB') from None
    headers = {
        'Content-Disposition': f"attachment; filename*=UTF-8''{quote(parts[-1], safe='')}",
        'Accept-Ranges': 'bytes', 'ETag': '"' + hashlib.sha256(data).hexdigest() + '"',
    }
    limit = 2 * 1024 * 1024  # base64 Lambda response remains below 6 MiB
    byte_range = request.headers.get('range')
    if byte_range:
        try:
            unit, value = byte_range.split('=', 1)
            start, end = value.split('-', 1)
            start, end = int(start), int(end)
            if unit != 'bytes' or start < 0 or end < start or start >= len(data):
                raise ValueError()
        except ValueError:
            raise HTTPException(416, 'Use a single explicit byte range') from None
        if request.headers.get('if-range', headers['ETag']) != headers['ETag']:
            raise HTTPException(412, 'File changed during download; restart download')
        end = min(end, start + limit - 1, len(data) - 1)
        headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
        return Response(data[start:end + 1], status_code=206,
                        media_type='application/octet-stream', headers=headers)
    if len(data) > limit:
        raise HTTPException(413, 'Use byte-range downloads for files over 2 MiB')
    return Response(data, media_type='application/octet-stream', headers=headers)


def administrator(user=Depends(current_user)):
    if not user['admin']:
        raise HTTPException(403, 'Administrator access required')
    return user


def public_team(team):
    return {key: team[key] for key in ('id', 'name', 'created_at')}


def public_user(user):
    result = {key: user[key] for key in ('username', 'sub', 'email', 'team_ids', 'enabled', 'status', 'kind')} | {
        'admin': 'Admins' in user['groups'],
    }
    for key in ('agent_id', 'agent_name', 'created_by', 'created_by_email',
                'security_test_mode', 'input_limit_value', 'input_limit_unit',
                'max_output_tokens'):
        if key in user:
            result[key] = user[key]
    return result


class TeamInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: UUID
    name: str = Field(min_length=1, max_length=80)


class TeamUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)


class UserInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(min_length=3, max_length=254, pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    team_ids: list[UUID] = Field(min_length=1, max_length=20)
    admin: bool = False


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    team_ids: list[UUID] = Field(min_length=1, max_length=20)
    admin: bool
    enabled: bool
    confirm_agent_transfer: bool = False


@app.get('/api/admin/teams')
def admin_teams(_user=Depends(administrator)):
    return sorted((public_team(team) for team in services().teams()), key=lambda team: team['name'].lower())


@app.post('/api/admin/teams')
def create_team(payload: TeamInput, _user=Depends(administrator)):
    existing = services().team(str(payload.id))
    if existing:
        if existing['name'] == payload.name.strip():
            return public_team(existing)
        raise HTTPException(409, 'Team ID already exists')
    try:
        return public_team(services().create_team(payload.name, str(payload.id)))
    except services().table.meta.client.exceptions.ConditionalCheckFailedException:
        raise HTTPException(409, 'Team already exists') from None


@app.post('/api/admin/teams/{team_id}')
def update_team(team_id: UUID, payload: TeamUpdate, _user=Depends(administrator)):
    team = services().team(str(team_id))
    if not team:
        raise HTTPException(404, 'Team not found')
    team['name'] = payload.name.strip()
    services().table.put_item(Item=team)
    return public_team(team)


@app.delete('/api/admin/teams/{team_id}')
def delete_team(team_id: UUID, _user=Depends(administrator)):
    team_id = str(team_id)
    if team_id == os.environ['DEFAULT_TEAM_ID']:
        raise HTTPException(409, 'The default team cannot be deleted')
    if services().team_has_users(team_id):
        raise HTTPException(409, 'Move or delete all team members first')
    if services().query('TEAM#' + team_id, 'AGENT#'):
        raise HTTPException(409, 'Teams with agents cannot be deleted')
    services().table.delete_item(Key={'pk': 'TEAM#' + team_id, 'sk': 'TEAM'})
    return Response(status_code=204)


@app.get('/api/admin/users')
def admin_users(_user=Depends(administrator)):
    return sorted((public_user(user) for user in services().users()), key=lambda user: user['email'].lower())


@app.post('/api/admin/users')
def create_user(payload: UserInput, _user=Depends(administrator)):
    try:
        return public_user(services().create_human(
            payload.email.lower(), [str(team_id) for team_id in payload.team_ids], payload.admin))
    except services().cognito.exceptions.UsernameExistsException:
        raise HTTPException(409, 'User already exists') from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.post('/api/admin/users/{username}')
def update_user(username: str, payload: UserUpdate, user=Depends(administrator)):
    if username == user['username'] and (not payload.admin or not payload.enabled):
        raise HTTPException(409, 'You cannot disable or remove your own administrator access')
    try:
        current = services().membership(username)
        requested_teams = [str(team_id) for team_id in payload.team_ids]
        if (current['kind'] == 'agent' and current['team_ids'] != requested_teams
                and not payload.confirm_agent_transfer):
            raise HTTPException(
                409, 'Confirm agent transfer: shared workspace and skills move to the new team; conversations remain private')
        return public_user(services().update_account(
            username, requested_teams, payload.admin, payload.enabled, current=current))
    except services().cognito.exceptions.UserNotFoundException:
        raise HTTPException(404, 'User not found') from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.delete('/api/admin/users/{username}')
def delete_user(username: str, user=Depends(administrator)):
    if username == user['username']:
        raise HTTPException(409, 'You cannot delete your own account')
    try:
        if services().membership(username)['kind'] == 'agent':
            raise HTTPException(409, 'Delete agents through an agent lifecycle operation')
        services().cognito.admin_delete_user(UserPoolId=services().pool, Username=username)
    except services().cognito.exceptions.UserNotFoundException:
        raise HTTPException(404, 'User not found') from None
    return Response(status_code=204)
