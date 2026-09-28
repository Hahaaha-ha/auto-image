"""强制改密：经 ASGI、重启与文件/审计 I/O 故障观察提交与撤销。"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from web.audit import AuditWriteError, ControlAudit
from web.auth import COOKIE_NAME, hash_password, issue_token
from web.tests.support import async_client, audit_lines, make_test_app


CURRENT = '旧 密码'
NEW = 'new-password!'
CHANGE = '/api/auth/change-password'


def fixture(tmp):
    root = Path(tmp)
    path = root / 'users.json'
    path.write_text(json.dumps({'users': {
        name: {'password_hash': hash_password(CURRENT, 1000), **cfg}
        for name, cfg in {
            'alice': {'must_change_password': True, 'created_at': '2026-09-01T02:00:00Z'},
            'bob': {'must_change_password': True},
            'legacy': {},
            'admin': {'role': 'admin', 'must_change_password': True},
        }.items()
    }}))
    options = dict(users_path=path, audit_dir=root / 'audit', state_path=root / 'state.json')
    return path, options, make_test_app(**options)


async def form(client, **overrides):
    me = (await client.get('/api/auth/me')).json()
    return {**dict(current_password=CURRENT, new_password=NEW, confirm_password=NEW,
                   expected_version=me['user_version']), **overrides}


async def test_pending_identity_is_restricted_across_methods_paths_and_restart():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app, 'alice', CURRENT) as client:
            me = (await client.get('/api/auth/me')).json()
            assert me['must_change_password'] is True, me
            assert me['can_manage_users'] is False
            cookie = client.cookies.get(COOKIE_NAME)
            # 枚举当前业务路由，也覆盖未知路由、认证前缀及非标准方法。
            paths = {route.path.replace('{run_id}', 'missing').replace('{task_id}', 'missing')
                     .replace('{rel_path:path}', 'deploy/x.md') for route in app.routes
                     if route.path.startswith('/api/') and not route.path.startswith('/api/auth/')}
            paths.update(['/api/unknown', '/api/auth/login/extra', '/api/auth/me/extra',
                          '/api/auth/change-password/extra', '/api/runs/', '/api/%72uns'])
            for path in paths:
                for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
                    response = await client.request(method, path)
                    assert response.status_code == 403, (method, path, response.status_code)
            for method, path in [('POST', '/api/auth/me'), ('GET', CHANGE), ('GET', '/api/auth/logout')]:
                assert (await client.request(method, path)).status_code == 403
            assert (await client.post('/api/auth/logout')).status_code == 200
        restarted = make_test_app(**options)
        async with async_client(restarted, username=None) as client:
            client.cookies.set(COOKIE_NAME, cookie)
            assert (await client.get('/api/auth/me')).json()['must_change_password'] is True
            assert (await client.get('/api/runs')).status_code == 403
        for name in ('legacy', 'admin'):
            async with async_client(restarted, name, CURRENT) as client:
                assert (await client.get('/api/auth/me')).json()['must_change_password'] is False
                assert (await client.get('/api/runs')).status_code == 200


async def test_change_revokes_all_old_logins_and_persists_without_affecting_other_users():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app, 'alice', CURRENT) as client, \
                async_client(app, 'alice', CURRENT) as old_window, \
                async_client(app, 'legacy', CURRENT) as other:
            old_cookie = old_window.cookies.get(COOKIE_NAME)
            other_cookie = other.cookies.get(COOKIE_NAME)
            body = await form(client)
            response = await client.post(CHANGE, json=body, headers={'x-request-id': 'change-alice'})
            assert response.status_code == 200, response.text
            assert response.json() == {'outcome': 'committed', 'audit_status': 'recorded'}
            assert (await old_window.get('/api/auth/me')).status_code == 401
            assert (await client.get('/api/auth/me')).status_code == 401
            assert (await other.get('/api/runs')).status_code == 200
        restarted = make_test_app(**options)
        async with async_client(restarted, username=None) as client:
            client.cookies.set(COOKIE_NAME, old_cookie)
            assert (await client.get('/api/auth/me')).status_code == 401
            assert (await client.post(CHANGE, json=body)).status_code == 401
            assert (await client.post('/api/auth/login', json={'username': 'alice', 'password': CURRENT})).status_code == 401
            response = await client.post('/api/auth/login', json={'username': 'alice', 'password': NEW})
            assert response.status_code == 200
            assert response.json()['must_change_password'] is False
            assert (await client.get('/api/runs')).status_code == 200
            client.cookies.clear()
            client.cookies.set(COOKIE_NAME, other_cookie)
            assert (await client.get('/api/runs')).status_code == 200
        async with async_client(restarted, 'admin', CURRENT) as admin:
            rows = {u['username']: u for u in (await admin.get('/api/admin/users')).json()['users']}
            assert rows['alice']['created_at'] == '2026-09-01T02:00:00Z'
            assert rows['legacy']['created_at'] is None
        records = [r for r in audit_lines(options['audit_dir']) if r['request_id'] == 'change-alice']
        assert [r['result'] for r in records] == ['prepared', 'success'], records
        assert all(r['actor'] == r['target_username'] == 'alice' for r in records)
        assert all(r['action'] == 'change_password' for r in records)
        serialized = json.dumps(records, ensure_ascii=False)
        for secret in (CURRENT, NEW, old_cookie, 'password_hash', 'pbkdf2_sha256'):
            assert secret not in serialized


async def test_password_validation_and_target_version_never_commit_rejections():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app, 'alice', CURRENT) as client:
            body = await form(client)
            cases = [({'current_password': 'wrong'}, 422),
                     ({'confirm_password': 'different'}, 422),
                     ({'expected_version': 'outdated'}, 409),
                     ({'expected_version': None}, 409)]
            for value in ('short', 'x' * 129, ' withspace', 'withspace ', 'new pass',
                          'new\tpassword', 'new\npassword', 'new\x00password', 'new\x7fpassword',
                          'new密码password', 'new\u00a0password', 12345678, None):
                cases.append(({'new_password': value, 'confirm_password': value}, 422))
            for fields, status in cases:
                response = await client.post(CHANGE, json={**body, **fields})
                assert response.status_code == status, (fields, response.text)
                assert response.json()['outcome'] == 'not_committed'
                assert (await client.get('/api/auth/me')).json()['user_version'] == body['expected_version']
        assert not any(r['result'] == 'success' and r['action'] == 'change_password'
                       for r in audit_lines(options['audit_dir']))
        async with async_client(make_test_app(**options), 'alice', CURRENT) as client:
            assert (await client.get('/api/auth/me')).json()['must_change_password'] is True


async def test_visible_ascii_boundaries_and_same_password():
    for password in ('!!!!!!!!', '~' * 128):
        with tempfile.TemporaryDirectory() as tmp:
            _, options, app = fixture(tmp)
            async with async_client(app, 'alice', CURRENT) as client:
                body = await form(client, new_password=password, confirm_password=password)
                assert (await client.post(CHANGE, json=body)).status_code == 200
            async with async_client(make_test_app(**options), 'alice', password) as client:
                assert (await client.get('/api/runs')).status_code == 200
    with tempfile.TemporaryDirectory() as tmp:
        path, _, _ = fixture(tmp)
        document = json.loads(path.read_text())
        document['users']['alice']['password_hash'] = hash_password(NEW, 1000)
        path.write_text(json.dumps(document))
        async with async_client(make_test_app(users_path=path), 'alice', NEW) as client:
            response = await client.post(CHANGE, json=await form(client, current_password=NEW))
            assert response.status_code == 422
            assert response.json()['detail'] == 'password_unchanged'


async def test_only_pending_enabled_identity_with_valid_cookie_can_submit():
    with tempfile.TemporaryDirectory() as tmp:
        path, _, app = fixture(tmp)
        async with async_client(app, 'alice', CURRENT) as client:
            body = await form(client)
            for origin in ('http://evil.example', 'https://testserver'):
                assert (await client.post(CHANGE, json=body, headers={'origin': origin})).status_code == 403
            expired = issue_token(app.state.auth_secret, 'alice', body['expected_version'], -1)
            client.cookies.clear()
            client.cookies.set(COOKIE_NAME, expired)
            assert (await client.post(CHANGE, json=body)).status_code == 401

        for name in ('admin', 'legacy'):
            async with async_client(app, name, CURRENT) as client:
                assert (await client.post(CHANGE, json=body)).status_code == 403
        async with async_client(app, 'alice', CURRENT) as client:
            document = json.loads(path.read_text())
            document['users']['alice']['enabled'] = False
            path.write_text(json.dumps(document))
            assert (await client.post(CHANGE, json=body)).status_code == 401


async def test_concurrent_windows_cannot_overwrite_and_different_users_do_not_conflict():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app, 'alice', CURRENT) as first, \
                async_client(app, 'alice', CURRENT) as second, \
                async_client(app, 'bob', CURRENT) as bob:
            alice_form, bob_form = await form(first), await form(bob)
            replies = await asyncio.gather(
                first.post(CHANGE, json=alice_form),
                second.post(CHANGE, json={**alice_form, 'new_password': 'other-password',
                                         'confirm_password': 'other-password'}),
                bob.post(CHANGE, json=bob_form))
            assert sorted(r.status_code for r in replies[:2]) == [200, 401]
            assert replies[2].status_code == 200, replies[2].text
        winner = NEW if replies[0].status_code == 200 else 'other-password'
        restarted = make_test_app(**options)
        for name, password in (('alice', winner), ('bob', NEW)):
            async with async_client(restarted, name, password) as client:
                assert (await client.get('/api/runs')).status_code == 200


async def test_missing_corrupt_or_unwritable_file_never_commits_or_rebuilds():
    for failure in ('missing', 'corrupt', 'unwritable', 'replace', 'flush'):
        with tempfile.TemporaryDirectory() as tmp:
            path, options, app = fixture(tmp)
            original = path.read_bytes()
            async with async_client(app, 'alice', CURRENT) as client:
                body = await form(client)
                if failure == 'missing':
                    path.unlink()
                elif failure == 'corrupt':
                    path.write_text('users: [broken')
                real_open = Path.open

                def open_file(p, *args, **kwargs):
                    if failure == 'unwritable' and p == path and args and args[0] == 'r+':
                        raise PermissionError('test read-only users')
                    return real_open(p, *args, **kwargs)

                real_replace = os.replace

                def replace_file(src, dst):
                    if failure == 'replace' and Path(dst) == path:
                        raise OSError('test replace failure')
                    return real_replace(src, dst)

                real_fsync = os.fsync

                def fsync_file(fd):
                    if failure == 'flush':
                        raise OSError('test flush failure')
                    return real_fsync(fd)

                with patch.object(Path, 'open', open_file), patch('os.replace', replace_file), patch('os.fsync', fsync_file):
                    response = await client.post(CHANGE, json=body)
                assert response.status_code == 503, (failure, response.text)
                assert response.json() == {'outcome': 'not_committed', 'detail': 'users_unavailable'}
                if failure == 'missing':
                    assert not path.exists()
                elif failure == 'corrupt':
                    assert path.read_text() == 'users: [broken'
                else:
                    assert path.read_bytes() == original
            records = [r for r in audit_lines(options['audit_dir']) if r['action'] == 'change_password']
            assert records[-1]['result'] == 'failure'
            assert all(r['result'] != 'success' for r in records)
            if failure in ('missing', 'corrupt'):
                # 模拟运维停服修复，未提交的密码仍可登录。
                path.write_bytes(original)
            async with async_client(make_test_app(**options), 'alice', CURRENT) as client:
                assert (await client.get('/api/auth/me')).json()['must_change_password'] is True


async def test_audit_failure_before_and_after_commit_have_distinct_outcomes():
    for failed_result in ('prepared', 'success'):
        with tempfile.TemporaryDirectory() as tmp:
            _, options, app = fixture(tmp)
            async with async_client(app, 'alice', CURRENT) as client:
                body = await form(client)
                old_cookie = client.cookies.get(COOKIE_NAME)
                real_record = ControlAudit.record

                def record(audit, **kwargs):
                    if kwargs['action'] == 'change_password' and kwargs['result'] == failed_result:
                        raise AuditWriteError('test audit I/O failure')
                    return real_record(audit, **kwargs)

                with patch.object(ControlAudit, 'record', record):
                    response = await client.post(CHANGE, json=body)
                committed = failed_result == 'success'
                assert response.status_code == (200 if committed else 503), response.text
                if committed:
                    assert response.json() == {'outcome': 'committed', 'audit_status': 'failed'}
                else:
                    assert response.json() == {'outcome': 'not_committed', 'detail': 'audit_unavailable'}
            restarted = make_test_app(**options)
            async with async_client(restarted, username=None) as client:
                client.cookies.set(COOKIE_NAME, old_cookie)
                assert (await client.get('/api/auth/me')).status_code == (401 if committed else 200)
            async with async_client(restarted, 'alice', NEW if committed else CURRENT) as client:
                assert (await client.get('/api/auth/me')).json()['must_change_password'] is not committed
            records = [r for r in audit_lines(options['audit_dir']) if r['action'] == 'change_password']
            assert not any(r['result'] == 'success' for r in records)
            assert any(r['result'] == 'prepared' for r in records) is committed


async def test_identity_is_rechecked_after_a_slow_request_body():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app, 'alice', CURRENT) as first:
            body = await form(first)
            cookie = first.cookies.get(COOKIE_NAME)
            reading, resume = asyncio.Event(), asyncio.Event()

            async def slow_body():
                reading.set()
                await resume.wait()
                yield json.dumps(body).encode()

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://testserver', cookies={COOKIE_NAME: cookie}) as second:
                stale = asyncio.create_task(second.post(CHANGE, content=slow_body()))
                await asyncio.wait_for(reading.wait(), 2)
                response = await first.post(CHANGE, json={**body, 'new_password': 'newest-password',
                                                         'confirm_password': 'newest-password'})
                assert response.status_code == 200
                resume.set()
                assert (await asyncio.wait_for(stale, 2)).status_code == 401
        async with async_client(make_test_app(**options), 'alice', 'newest-password') as client:
            assert (await client.get('/api/runs')).status_code == 200


async def test_restricted_recovery_and_state_write_failure_block_password_change():
    for failure in ('corrupt', 'write'):
        with tempfile.TemporaryDirectory() as tmp:
            path, options, app = fixture(tmp)
            original = path.read_bytes()
            if failure == 'corrupt':
                options['state_path'].write_text('broken')
                app = make_test_app(**options)
            async with async_client(app, 'alice', CURRENT) as client:
                body = await form(client)
                real_replace = os.replace

                def replace_file(src, dst):
                    if failure == 'write' and Path(dst) == options['state_path']:
                        raise OSError('test state I/O failure')
                    return real_replace(src, dst)

                with patch('os.replace', replace_file):
                    response = await client.post(CHANGE, json=body)
                assert response.status_code == 503, response.text
                assert response.json()['detail'] == 'state_unavailable'
                assert path.read_bytes() == original


async def test_malformed_pending_state_fails_closed():
    for value in (None, 'false', 1, [], {}):
        with tempfile.TemporaryDirectory() as tmp:
            path, options, _ = fixture(tmp)
            document = json.loads(path.read_text())
            document['users']['alice']['must_change_password'] = value
            path.write_text(json.dumps(document))
            async with async_client(make_test_app(**options), username=None) as client:
                response = await client.post('/api/auth/login', json={'username': 'alice', 'password': CURRENT})
                assert response.status_code == 401


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith('test_')]
    for test in tests:
        await test()
        print(f'ok {test.__name__}')
    print(f'{len(tests)} passed')


if __name__ == '__main__':
    asyncio.run(main())
