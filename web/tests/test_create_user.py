"""管理员新增到首次正常登录：ASGI、持久化及提交结果契约。"""
import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from web.audit import AuditWriteError, ControlAudit
from web.auth import COOKIE_NAME, issue_token
from web.tests.support import async_client, audit_lines, make_test_app, write_test_users

CREATE = '/api/admin/users'
INITIAL = 'initial-password!'
PERSONAL = 'personal-password!'


def fixture(tmp):
    root = Path(tmp)
    path = write_test_users(root / 'users.yaml')
    options = dict(users_path=path, audit_dir=root / 'audit', state_path=root / 'state.json')
    return path, options, make_test_app(**options)


async def test_create_restart_required_change_and_first_business_login():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app) as admin, async_client(app, 'alice') as other:
            before = datetime.now(timezone.utc)
            response = await admin.post(CREATE, json={'username': 'New.user-1', 'password': INITIAL},
                                        headers={'x-request-id': 'create-new'})
            after = datetime.now(timezone.utc)
            assert response.status_code == 201, response.text
            assert response.json() == {'outcome': 'committed', 'audit_status': 'recorded'}
            rows = (await admin.get(CREATE)).json()['users']
            new = next(row for row in rows if row['username'] == 'New.user-1')
            assert new['role'] == 'user' and new['enabled'] is True
            created = datetime.fromisoformat(new['created_at'].replace('Z', '+00:00'))
            assert before <= created <= after and created.utcoffset().total_seconds() == 0
            assert all(row['created_at'] is None for row in rows if row != new)
            assert (await other.get('/api/runs')).status_code == 200

        restarted = make_test_app(**options)
        async with async_client(restarted, 'New.user-1', INITIAL) as user:
            me = (await user.get('/api/auth/me')).json()
            assert me['must_change_password'] is True and me['can_manage_users'] is False
            cookie = user.cookies.get(COOKIE_NAME)
            assert (await user.get('/api/runs')).status_code == 403
            result = await user.post('/api/auth/change-password', json={
                'current_password': INITIAL, 'new_password': PERSONAL,
                'confirm_password': PERSONAL, 'expected_version': me['user_version'],
            })
            assert result.status_code == 200
            assert (await user.get('/api/runs')).status_code == 401

        restarted = make_test_app(**options)
        async with async_client(restarted, username=None) as user, async_client(restarted) as admin:
            user.cookies.set(COOKIE_NAME, cookie)
            assert (await user.get('/api/runs')).status_code == 401
            assert (await user.post('/api/auth/login', json={
                'username': 'New.user-1', 'password': INITIAL})).status_code == 401
            result = await user.post('/api/auth/login', json={
                'username': 'New.user-1', 'password': PERSONAL})
            assert result.status_code == 200 and result.json()['must_change_password'] is False
            run = (await user.post('/api/runs')).json()['run_id']
            assert (await admin.get(f'/api/runs/{run}')).status_code == 404
            rows = (await admin.get(CREATE)).json()['users']
            current = next(row for row in rows if row['username'] == 'New.user-1')
            assert current['user_version'] != new['user_version']
            assert {k: v for k, v in current.items() if k != 'user_version'} == {k: v for k, v in new.items() if k != 'user_version'}
        records = [r for r in audit_lines(options['audit_dir']) if r['request_id'] == 'create-new']
        assert [r['result'] for r in records] == ['prepared', 'success']
        assert all(r['actor'] == 'tester' and r['target_username'] == 'New.user-1'
                   and r['action'] == 'create_user' for r in records)


async def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith('test_')]
    for test in tests:
        await test()
        print('ok', test.__name__)
    print(f'{len(tests)} passed')


async def test_input_boundaries_exact_names_and_server_owned_fields():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app) as admin:
            original = (await admin.get(CREATE)).json()
            for username in ('', 'a' * 65, ' leading', 'trailing ', 'inner space', '中文',
                             'a/b', 'a\\b', 'a\n', 'a\t', 'a\x00', 'a\x7f', 'aé', [], {}, None, 1):
                response = await admin.post(CREATE, json={'username': username, 'password': INITIAL})
                assert response.status_code == 422, (username, response.text)
                assert response.json() == {'outcome': 'not_committed', 'detail': 'invalid_username'}
            for password in ('', 'x' * 7, 'x' * 129, ' withspace', 'withspace ', 'new pass',
                             'new\tpassword', 'new\npassword', 'new\x00password', 'new\x7fpassword',
                             'new密码password', 'new\u00a0password', 12345678, None):
                response = await admin.post(CREATE, json={'username': 'valid', 'password': password})
                assert response.status_code == 422, (password, response.text)
                assert response.json()['detail'] == 'invalid_new_password'
            assert (await admin.get(CREATE)).json() == original
            for username, password in [('a', '!!!!!!!!'), ('A', '~' * 128), ('_-.09' + 'x' * 59, INITIAL)]:
                response = await admin.post(CREATE, json={'username': username, 'password': password,
                    'role': 'admin', 'enabled': False, 'must_change_password': False,
                    'created_at': '2000-01-01T00:00:00Z', 'owner': 'tester', 'revision': 'forged'})
                assert response.status_code == 201, response.text
                async with async_client(make_test_app(**options), username, password) as user:
                    me = (await user.get('/api/auth/me')).json()
                    assert me['can_manage_users'] is False and me['must_change_password'] is True
                    assert (await user.post(CREATE, json={'username': 'injected', 'password': INITIAL})).status_code == 403
            rows = (await admin.get(CREATE)).json()['users']
            for row in rows:
                assert set(row) == {'username', 'role', 'enabled', 'created_at', 'user_version'}
                assert row['created_at'] != '2000-01-01T00:00:00Z'
            response = await admin.post(CREATE, json={'username': 'a', 'password': PERSONAL})
            assert response.status_code == 409 and response.json()['detail'] == 'username_exists'
        async with async_client(make_test_app(**options), 'a', '!!!!!!!!') as user:
            assert (await user.get('/api/auth/me')).status_code == 200


async def test_permissions_origin_expiry_and_audit_exclude_credentials():
    with tempfile.TemporaryDirectory() as tmp:
        path, options, app = fixture(tmp)
        body = {'username': 'new-user', 'password': INITIAL}
        for actor, status in [(None, 401), ('alice', 403), ('bob', 403)]:
            async with async_client(app, actor) as client:
                response = await client.post(CREATE, json=body, headers={'x-request-id': str(actor)})
                assert response.status_code == status
        async with async_client(app) as admin:
            cookie = admin.cookies.get(COOKIE_NAME)
            for origin in ('http://evil.example', 'https://testserver'):
                assert (await admin.post(CREATE, json=body, headers={'origin': origin})).status_code == 403
            admin.cookies.clear()
            admin.cookies.set(COOKIE_NAME, issue_token(app.state.auth_secret, 'tester', 'unused', -1))
            assert (await admin.post(CREATE, json=body)).status_code == 401
        for actor in ('tester', 'admin'):
            async with async_client(app, actor) as admin:
                assert (await admin.post(CREATE, json={**body, 'username': actor + '-created'})).status_code == 201
                assert (await admin.post(CREATE, json={'username': actor, 'password': INITIAL})).status_code == 409
        records = [r for r in audit_lines(options['audit_dir']) if r['action'] == 'create_user']
        assert any(r.get('reason') == 'not_admin' and r['actor'] == 'alice'
                   and r['target_username'] == 'new-user' for r in records)
        assert any(r.get('reason') == 'cross_origin' for r in records)
        assert any(r.get('reason') == 'username_exists' and r['result'] == 'denied' for r in records)
        serialized = json.dumps(records)
        for secret in (INITIAL, cookie, 'password_hash', 'pbkdf2_sha256'):
            assert secret not in serialized
        # 文件是密码存储边界：明文不能持久化。
        assert INITIAL not in path.read_text()


async def test_concurrent_duplicate_create_and_password_change_preserve_all_users():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app) as first, async_client(app, 'admin') as second:
            assert (await first.post(CREATE, json={'username': 'pending', 'password': INITIAL})).status_code == 201
            async with async_client(app, 'pending', INITIAL) as pending:
                me = (await pending.get('/api/auth/me')).json()
                replies = await asyncio.gather(
                    first.post(CREATE, json={'username': 'same', 'password': INITIAL}),
                    second.post(CREATE, json={'username': 'same', 'password': PERSONAL}),
                    second.post(CREATE, json={'username': 'different', 'password': INITIAL}),
                    pending.post('/api/auth/change-password', json={'current_password': INITIAL,
                        'new_password': PERSONAL, 'confirm_password': PERSONAL, 'expected_version': me['user_version']}))
                assert sorted(r.status_code for r in replies[:2]) == [201, 409]
                assert [r.status_code for r in replies[2:]] == [201, 200]
            assert (await first.get('/api/runs')).status_code == 200
            assert (await second.get('/api/runs')).status_code == 200
        restarted = make_test_app(**options)
        winner = INITIAL if replies[0].status_code == 201 else PERSONAL
        for name, password, restricted in [('same', winner, True), ('different', INITIAL, True), ('pending', PERSONAL, False)]:
            async with async_client(restarted, name, password) as client:
                assert (await client.get('/api/auth/me')).json()['must_change_password'] is restricted
        async with async_client(restarted) as admin:
            names = [r['username'] for r in (await admin.get(CREATE)).json()['users']]
            assert names.count('same') == 1 and len(names) == 8


async def test_user_file_failures_do_not_create_or_replace_roster():
    for failure in ('missing', 'corrupt', 'unreadable', 'unwritable', 'replace', 'flush'):
        with tempfile.TemporaryDirectory() as tmp:
            path, options, app = fixture(tmp)
            original = path.read_bytes()
            async with async_client(app) as admin:
                if failure == 'missing':
                    path.unlink()
                elif failure == 'corrupt':
                    path.write_text('users: [broken')
                real_open, real_replace, real_fsync = Path.open, os.replace, os.fsync

                def open_file(p, *args, **kwargs):
                    if p == path and (failure == 'unreadable' or
                                      (failure == 'unwritable' and args and args[0] == 'r+')):
                        raise PermissionError('test users file unavailable')
                    return real_open(p, *args, **kwargs)

                def replace_file(src, dst):
                    if failure == 'replace' and Path(dst) == path:
                        raise OSError('test replace failure')
                    return real_replace(src, dst)

                def fsync_file(fd):
                    if failure == 'flush' and str(path.parent) in os.readlink(f'/proc/self/fd/{fd}') \
                            and f'.{path.name}.' in os.readlink(f'/proc/self/fd/{fd}'):
                        raise OSError('test users flush failure')
                    return real_fsync(fd)

                with patch.object(Path, 'open', open_file), patch('os.replace', replace_file), patch('os.fsync', fsync_file):
                    response = await admin.post(CREATE, json={'username': 'new-user', 'password': INITIAL})
                assert response.status_code == 503, (failure, response.text)
                assert response.json() == {'outcome': 'not_committed', 'detail': 'users_unavailable'}
            if failure == 'missing':
                assert not path.exists()
            elif failure == 'corrupt':
                assert path.read_text() == 'users: [broken'
            else:
                assert path.read_bytes() == original
            records = [r for r in audit_lines(options['audit_dir']) if r['action'] == 'create_user']
            assert records[-1]['result'] == 'failure' and all(r['result'] != 'success' for r in records)
            if failure in ('missing', 'corrupt'):
                path.write_bytes(original)
            async with async_client(make_test_app(**options)) as admin:
                assert 'new-user' not in [r['username'] for r in (await admin.get(CREATE)).json()['users']]


async def test_prepared_audit_failure_blocks_but_result_failure_keeps_user():
    for failure in ('prepared', 'success'):
        with tempfile.TemporaryDirectory() as tmp:
            _, options, app = fixture(tmp)
            async with async_client(app) as admin:
                real_record = ControlAudit.record

                def record(audit, **kwargs):
                    if kwargs['action'] == 'create_user' and kwargs['result'] == failure:
                        raise AuditWriteError('test audit failure')
                    return real_record(audit, **kwargs)

                with patch.object(ControlAudit, 'record', record):
                    response = await admin.post(CREATE, json={'username': 'new-user', 'password': INITIAL})
                committed = failure == 'success'
                assert response.status_code == (201 if committed else 503)
                assert response.json() == ({'outcome': 'committed', 'audit_status': 'failed'} if committed
                                          else {'outcome': 'not_committed', 'detail': 'audit_unavailable'})
            restarted = make_test_app(**options)
            async with async_client(restarted, 'new-user', INITIAL) as user:
                assert (await user.get('/api/auth/me')).status_code == (200 if committed else 401)
            async with async_client(restarted) as admin:
                names = [r['username'] for r in (await admin.get(CREATE)).json()['users']]
                assert ('new-user' in names) is committed
            records = [r for r in audit_lines(options['audit_dir']) if r['action'] == 'create_user']
            assert all(r['result'] != 'success' for r in records)
            assert any(r['result'] == 'prepared' for r in records) is committed


async def test_admin_identity_is_rechecked_after_reading_body():
    with tempfile.TemporaryDirectory() as tmp:
        path, _, app = fixture(tmp)
        async with async_client(app) as admin:
            reading, resume = asyncio.Event(), asyncio.Event()

            async def slow_body():
                reading.set()
                await resume.wait()
                yield json.dumps({'username': 'new-user', 'password': INITIAL}).encode()

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                         cookies={COOKIE_NAME: admin.cookies.get(COOKIE_NAME)}) as slow:
                creating = asyncio.create_task(slow.post(CREATE, content=slow_body()))
                await asyncio.wait_for(reading.wait(), 2)
                document = yaml.safe_load(path.read_text())
                document['users']['tester']['role'] = 'user'
                path.write_text(yaml.safe_dump(document))
                resume.set()
                assert (await asyncio.wait_for(creating, 2)).status_code == 401
        async with async_client(app, 'admin') as admin:
            assert 'new-user' not in [r['username'] for r in (await admin.get(CREATE)).json()['users']]


async def test_restricted_recovery_and_state_failure_block_creation():
    for failure in ('corrupt', 'write'):
        with tempfile.TemporaryDirectory() as tmp:
            path, options, _ = fixture(tmp)
            original = path.read_bytes()
            options['state_path'].write_text('broken' if failure == 'corrupt' else '{}')
            app = make_test_app(**options)
            async with async_client(app) as admin:
                real_replace = os.replace

                def replace_file(src, dst):
                    if failure == 'write' and Path(dst) == options['state_path']:
                        raise OSError('test state unavailable')
                    return real_replace(src, dst)

                with patch('os.replace', replace_file):
                    response = await admin.post(CREATE, json={'username': 'new-user', 'password': INITIAL})
                assert response.status_code == 503
                assert response.json() == {'outcome': 'not_committed', 'detail': 'state_unavailable'}
            assert path.read_bytes() == original


if __name__ == '__main__':
    asyncio.run(main())
