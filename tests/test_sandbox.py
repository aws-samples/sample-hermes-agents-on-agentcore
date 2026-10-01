import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from probe.sandbox import run, workspace_fd
from probe.server import app

SUB = '11111111-1111-4111-8111-111111111111'


@pytest.mark.parametrize('subject', ['../other', '/etc', 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA',
                                    '{11111111-1111-4111-8111-111111111111}'])
def test_invalid_identity_cannot_select_path(tmp_path, subject):
    with pytest.raises(ValueError), workspace_fd(tmp_path, subject):
        pytest.fail('Invalid subject accepted')


def test_workspace_selection_rejects_symlink(tmp_path):
    (tmp_path / SUB).symlink_to('/tmp', target_is_directory=True)
    with pytest.raises(OSError), workspace_fd(tmp_path, SUB):
        pytest.fail('Symlink accepted')


def test_workspace_descriptor_is_closed(tmp_path):
    (tmp_path / SUB).mkdir()
    with workspace_fd(tmp_path, SUB) as descriptor:
        assert os.fstat(descriptor).st_ino == (tmp_path / SUB).stat().st_ino
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_launch_failure_never_retries_unsandboxed(tmp_path):
    (tmp_path / SUB).mkdir()
    failure = SimpleNamespace(returncode=1)
    with patch('probe.sandbox.subprocess.run', return_value=failure) as execute:
        result = run(tmp_path, SUB, '{}')
    assert result.returncode == 1
    assert execute.call_count == 1
    arguments = execute.call_args.args[0]
    assert arguments[0] == '/usr/bin/bwrap'
    assert '--unshare-all' in arguments
    assert '--clearenv' in arguments


def test_probe_rejects_arbitrary_commands():
    client = TestClient(app)
    response = client.post('/invocations', json={'marker': 'test', 'command': 'id'})
    assert response.status_code == 422
    response = client.post('/invocations', json={'marker': 'test', 'agent': '../../etc'})
    assert response.status_code == 422


def test_probe_health_contract():
    assert TestClient(app).get('/ping').json() == {'status': 'Healthy'}
