import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import jwt

from common.execution import execution_mode


def subject(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError('Noncanonical subject')
    return value


class Tokens:
    def __init__(self, issuer: str, client_id: str, kind: str, group: str):
        self.issuer, self.client_id, self.kind, self.group = issuer, client_id, kind, group
        self.keys = jwt.PyJWKClient(f'{issuer}/.well-known/jwks.json', lifespan=300)

    def verify(self, token: str) -> dict:
        if len(token) > 16384:
            raise ValueError('Token too large')
        key = self.keys.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=['RS256'], issuer=self.issuer,
                            audience=self.client_id if self.kind == 'id' else None,
                            options={'verify_aud': self.kind == 'id',
                                     'require': ['exp', 'iat', 'iss', 'sub', 'token_use']})
        if claims['token_use'] != self.kind:
            raise ValueError('Wrong token type')
        if self.kind == 'access' and claims.get('client_id') != self.client_id:
            raise ValueError('Wrong app client')
        if self.group not in claims.get('cognito:groups', []):
            raise ValueError('Wrong account type')
        subject(claims['sub'])
        if self.group == 'Agents':
            claims['execution_mode'] = execution_mode(claims)
            if 'team_ids' in claims:
                raise ValueError('Agent token cannot span teams')
            claims['team_id'] = subject(claims.get('team_id', ''))
            if not isinstance(claims.get('security_test_mode'), bool):
                raise ValueError('Agent security test mode claim required')
            for name in ('input_limit_value', 'max_output_tokens'):
                value = claims.get(name)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f'Agent {name} claim required')
            if claims.get('input_limit_unit') not in {'tokens', 'mb'}:
                raise ValueError('Agent input limit unit claim required')
        else:
            team_ids = claims.get('team_ids')
            if not isinstance(team_ids, list) or not team_ids:
                raise ValueError('Team membership required')
            claims['team_ids'] = sorted({subject(team_id) for team_id in team_ids})
        return claims


@contextmanager
def directory(root: Path, *parts: str, create: bool = False):
    """Resolve every path component using directory handles, never following links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(root, flags)
    try:
        for part in parts:
            if not part or part in {'.', '..'} or '/' in part or '\\' in part or '\0' in part:
                raise ValueError('Invalid path component')
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_file(fd: int, name: str, limit: int = 32 * 1024 * 1024) -> bytes:
    if '/' in name or name in {'', '.', '..'}:
        raise ValueError('Invalid filename')
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(file_fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Not a regular file or exceeds size limit')
        result = handle.read(limit + 1)
        if len(result) > limit:
            raise ValueError('File exceeds size limit')
        return result
