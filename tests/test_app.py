import asyncio
import json
import sqlite3
import sys
import types
import warnings

from fastapi.testclient import TestClient

import main
from settings import settings as settings_store
from settings.validation import remote_server


def test_health_and_home():
    with TestClient(main.app) as client:
        assert client.get('/health').json() == {'status': 'ok'}
        home = client.get('/')
        assert home.status_code == 200
        assert 'Board' in home.text
        assert '/static/js/settings.js' not in home.text


def test_lifespan_initializes_database_without_deprecation():
    async def run():
        for _ in range(2):
            async with main.app.router.lifespan_context(main.app):
                with sqlite3.connect(main.DATABASE_PATH) as connection:
                    assert connection.execute(
                        "SELECT value FROM app_meta WHERE key = 'schema_version'"
                    ).fetchone() == ('1',)
                    tables = {row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )}
                    assert {'board_tasks', 'board_attachments', 'agent_runs'} <= tables

    with warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        asyncio.run(run())


def test_settings_storage(tmp_path):
    ui_dir = tmp_path / 'ui'
    user_dir = tmp_path / 'user'
    ui_dir.mkdir()
    (ui_dir / 'general.json').write_text(json.dumps({
        'title': 'General',
        'groups': [{'fields': [
            {'id': 'name', 'type': 'string', 'default': 'default name'},
            {'id': 'enabled', 'type': 'checkbox'},
        ]}],
    }))

    sections = settings_store.load_sections(ui_dir)

    assert sections[0]['key'] == 'general'
    assert settings_store.load_values(sections[0], user_dir) == {
        'name': 'default name',
        'enabled': False,
    }

    settings_store.save_values('general', {'name': 'saved name', 'enabled': True}, user_dir)

    assert settings_store.load_values(sections[0], user_dir) == {
        'name': 'saved name',
        'enabled': True,
    }


def test_validation_is_loaded_from_validation_directory(tmp_path):
    validation_dir = tmp_path / 'validation'
    validation_dir.mkdir()
    (validation_dir / 'sample.py').write_text(
        'def validate(values):\n    return ["invalid"] if values.get("name") != "ok" else []\n'
    )

    assert settings_store.validate_section('sample', {'name': 'bad'}, validation_dir) == ['invalid']
    assert settings_store.validate_section('sample', {'name': 'ok'}, validation_dir) == []


def test_remote_server_validation_runs_ssh_command(monkeypatch):
    calls = []

    class Channel:
        def recv_ready(self):
            return False

        def recv_stderr_ready(self):
            return False

        def exit_status_ready(self):
            return True

        def recv_exit_status(self):
            return 0

        def close(self):
            pass

    class Stream:
        channel = Channel()

        def read(self):
            return b''

    class Client:
        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            calls.append(kwargs)

        def exec_command(self, command, timeout):
            assert timeout == remote_server.COMMAND_TIMEOUT
            calls.append(command)
            return Stream(), Stream(), Stream()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'paramiko', types.SimpleNamespace(SSHClient=Client, RejectPolicy=object))

    assert remote_server.validate({'host': 'example.org:2222', 'username': 'alice', 'password': 'secret', 'test_shell': 'uname'}) == []
    assert calls[0]['hostname'] == 'example.org'
    assert calls[0]['port'] == 2222
    assert calls[1] == 'uname'


def test_remote_server_validation_reports_connection_error(monkeypatch):
    class Client:
        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            raise OSError('unreachable')

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'paramiko', types.SimpleNamespace(SSHClient=Client, RejectPolicy=object))

    assert remote_server.validate({'host': 'example.org', 'username': 'alice', 'password': 'secret', 'test_shell': 'uname'}) == ['SSH connection or test command failed']


def test_settings_save_and_validation(tmp_path, monkeypatch):
    user_dir = tmp_path / 'user'
    monkeypatch.setattr(settings_store, 'USER_SETTINGS_DIR', user_dir)
    monkeypatch.setattr(
        settings_store,
        'validate_section',
        lambda section_key, values: [] if values.get('password') or values.get('ssh_key') else ['Authentication: password or SSH key is required'],
    )
    with TestClient(main.app) as client:
        page = client.get('/settings')
        assert page.status_code == 200
        assert 'Remote server' in page.text
        assert '<h1>Settings</h1>' not in page.text
        assert 'class="main-nav"' in page.text
        assert 'class="settings-tabs"' in page.text
        assert 'class="field-group-title"' in page.text
        assert '<fieldset class="field-group">' not in page.text
        assert 'id="ssh_key" name="ssh_key"' in page.text
        assert 'class="input-big"' in page.text
        assert 'id="form-status"' not in page.text
        assert '/static/css/toast.css' in page.text
        assert '/static/js/toast.js' in page.text
        assert '/static/css/spinner.css' in page.text
        assert '/static/js/spinner.js' in page.text
        assert '/static/js/settings.js' in page.text
        assert 'class="sidebar"' not in page.text
        spinner_css = client.get('/static/css/spinner.css')
        assert spinner_css.status_code == 200
        assert '.spinner' in spinner_css.text
        spinner_js = client.get('/static/js/spinner.js')
        assert spinner_js.status_code == 200
        assert 'window.spinner' in spinner_js.text
        stylesheet = client.get('/static/css/app.css')
        assert stylesheet.status_code == 200
        assert 'grid-column: 2' in stylesheet.text
        assert '.settings-form .field-control:has(.input-big)' in stylesheet.text
        assert 'justify-content: flex-end' in stylesheet.text
        invalid = client.post('/api/settings/remote_server', json={'values': {'username': '', 'host': ''}})
        assert invalid.status_code == 422
        assert 'Authentication: password or SSH key is required' in invalid.json()['detail']

        missing_auth = client.post(
            '/api/settings/remote_server',
            json={'values': {'username': 'alice', 'host': 'example.org'}},
        )
        assert missing_auth.status_code == 422

        password_auth = client.post(
            '/api/settings/remote_server',
            json={'values': {'username': 'alice', 'host': 'example.org', 'password': 'secret'}},
        )
        assert password_auth.status_code == 200
        assert json.loads((user_dir / 'remote_server.json').read_text())['password'] == 'secret'

        key_auth = client.post(
            '/api/settings/remote_server',
            json={'values': {'username': 'alice', 'host': 'example.org', 'ssh_key': '/keys/id_ed25519'}},
        )
        assert key_auth.status_code == 200
        saved = json.loads((user_dir / 'remote_server.json').read_text())
        assert saved['ssh_key'] == '/keys/id_ed25519'
        assert '`ssh_key`' not in saved
