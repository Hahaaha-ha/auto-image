"""管理员重置密码的 ASGI、持久撤销、并发与故障结果契约。"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from web.audit import AuditWriteError, ControlAudit

from web.auth import COOKIE_NAME
from web.fake import FakeSessionFactory, DEFAULT_SCRIPT
from web.tests.support import TEST_PASSWORD, async_client, audit_lines, make_test_app, write_test_users
from web.tests.test_stream_isolation import wait_status, open_global_stream, collect_frames

USERS = '/api/admin/users'
RESET = f'{USERS}/reset-password'
TEMP_PASSWORD = 'temporary-password'
PERSONAL_PASSWORD = 'personal-password'


def fixture(tmp):
    root = Path(tmp)
    path = write_test_users(root / 'users.yaml')
    data = json.loads(path.read_text())
    data['users']['alice']['created_at'] = '2026-09-01T02:00:00Z'
    path.write_text(json.dumps(data))
    factory = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02)
    options = dict(users_path=path, audit_dir=root / 'audit', state_path=root / 'state.json',
                   session_factory=factory, list_sessions_fn=factory.list_sessions,
                   get_session_messages_fn=factory.get_session_messages)
    return path, options, make_test_app(**options)


async def row(client, username):
    response = await client.get(USERS)
    assert response.status_code == 200, response.text
    return next(user for user in response.json()['users'] if user['username'] == username)


async def reset(client, user, password=TEMP_PASSWORD, **kwargs):
    return await client.post(RESET, json={'username': user['username'], 'password': password,
                                         'expected_version': user['user_version']}, **kwargs)


async def login(client, username, password):
    return await client.post('/api/auth/login', json={'username': username, 'password': password})


async def change_password(client, version, current=TEMP_PASSWORD):
    return await client.post('/api/auth/change-password', json={'current_password': current,
        'new_password': PERSONAL_PASSWORD, 'confirm_password': PERSONAL_PASSWORD, 'expected_version': version})


async def test_reset_restart_required_change_and_owner_continuity():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app) as admin, async_client(app, 'alice') as alice, async_client(app, 'bob') as bob:
            original = await row(admin, 'alice')
            cookie = alice.cookies.get(COOKIE_NAME)
            run = (await alice.post('/api/runs')).json()['run_id']
            await alice.post(f'/api/runs/{run}/messages', json={'text': '部署示例'})
            await wait_status(alice, run, 'READY')
            response = await reset(admin, original)
            assert response.status_code == 200, response.text
            assert response.json() == {'outcome': 'committed', 'audit_status': 'recorded'}
            assert (await alice.get('/api/runs')).status_code == 401
            assert (await bob.get('/api/runs')).status_code == 200
            assert (await admin.get(f'/api/runs/{run}')).status_code == 404
        app = make_test_app(**options)
        async with async_client(app) as admin, async_client(app, None) as alice, async_client(app, None) as old:
            current = await row(admin, 'alice')
            assert current['enabled'] is True
            assert current['created_at'] == original['created_at']
            assert (await login(alice, 'alice', TEST_PASSWORD)).status_code == 401
            old.cookies.set(COOKIE_NAME, cookie)
            assert (await old.get('/api/runs')).status_code == 401
            response = await login(alice, 'alice', TEMP_PASSWORD)
            assert response.status_code == 200 and response.json()['must_change_password'] is True
            version = response.json()['user_version']
            pending_cookie = alice.cookies.get(COOKIE_NAME)
            for path in ('/api/runs', '/api/stream', '/api/artifacts', '/api/obs', '/api/ecs', USERS):
                assert (await alice.get(path)).status_code == 403
            assert (await change_password(alice, version)).status_code == 200
            old.cookies.set(COOKIE_NAME, pending_cookie)
            assert (await old.get('/api/auth/me')).status_code == 401
        app = make_test_app(**options)
        async with async_client(app, None) as alice, async_client(app) as admin:
            assert (await login(alice, 'alice', TEMP_PASSWORD)).status_code == 401
            response = await login(alice, 'alice', PERSONAL_PASSWORD)
            assert response.status_code == 200 and response.json()['must_change_password'] is False
            assert [r['run_id'] for r in (await alice.get('/api/runs')).json()['runs']] == [run]
            assert (await row(admin, 'alice'))['created_at'] == original['created_at']
        records = [r for r in audit_lines(options['audit_dir']) if r['action'] in ('reset_password', 'change_password')]
        assert [(r['action'], r['result']) for r in records] == [
            ('reset_password', 'prepared'), ('reset_password', 'success'),
            ('change_password', 'prepared'), ('change_password', 'success')]
        assert [r['actor'] for r in records] == ['tester', 'tester', 'alice', 'alice']
        assert all(r['target_username'] == 'alice' for r in records)
        assert records[0]['request_id'] == records[1]['request_id']
        assert records[2]['request_id'] == records[3]['request_id']
        for secret in (cookie, pending_cookie, TEST_PASSWORD, TEMP_PASSWORD, PERSONAL_PASSWORD, 'password_hash'):
            assert secret not in json.dumps(records)


async def test_same_password_always_revokes_and_disabled_user_stays_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app) as admin, async_client(app, 'alice') as alice:
            cookies = [alice.cookies.get(COOKIE_NAME)]
            ghost = await row(admin, 'ghost')
            assert (await reset(admin, ghost)).status_code == 200
            for _ in range(2):
                assert (await reset(admin, await row(admin, 'alice'), TEST_PASSWORD)).status_code == 200
                for cookie in cookies:
                    async with async_client(app, None) as old:
                        old.cookies.set(COOKIE_NAME, cookie)
                        assert (await old.get('/api/auth/me')).status_code == 401
                response = await login(alice, 'alice', TEST_PASSWORD)
                assert response.json()['must_change_password'] is True
                cookies.append(alice.cookies.get(COOKIE_NAME))
        async with async_client(make_test_app(**options)) as admin, async_client(make_test_app(**options), None) as client:
            current = await row(admin, 'ghost')
            assert current['enabled'] is False and current['created_at'] == ghost['created_at'] is None
            assert (await login(client, 'ghost', TEMP_PASSWORD)).status_code == 401
            assert (await login(client, 'alice', TEST_PASSWORD)).json()['must_change_password'] is True
            for cookie in cookies[:-1]:
                client.cookies.clear()
                client.cookies.set(COOKIE_NAME, cookie)
                assert (await client.get('/api/auth/me')).status_code == 401


async def test_permissions_exact_target_password_rules_and_audit_redaction():
    with tempfile.TemporaryDirectory() as tmp:
        path, options, _ = fixture(tmp)
        data = json.loads(path.read_text())
        data['users'][' Legacy 用户 / '] = data['users'].pop('alice')
        data['users']['bob']['must_change_password'] = True
        path.write_text(json.dumps(data))
        app = make_test_app(**options)
        async with async_client(app) as admin:
            target = await row(admin, ' Legacy 用户 / ')
            for actor, status in [(None, 401), (' Legacy 用户 / ', 403), ('bob', 403)]:
                async with async_client(app, actor) as client:
                    assert (await reset(client, target)).status_code == status
            for name in ('tester', 'admin'):
                response = await reset(admin, await row(admin, name))
                assert response.status_code == 403 and response.json()['detail'] == 'admin_read_only'
            for origin in ('http://evil.example', 'https://testserver'):
                assert (await reset(admin, target, headers={'origin': origin})).status_code == 403
            for name in ('Legacy 用户 /', 'LEGACY', [], None):
                assert (await admin.post(RESET, json={'username': name, 'password': TEMP_PASSWORD})).status_code == 404
            for version in (None, 1, {}, 'stale'):
                response = await reset(admin, {**target, 'user_version': version})
                assert response.status_code == 409
            for password in ('short', 'a' * 129, ' leading123', 'trailing123 ', 'tab\t12345', 'line\n12345',
                             '中文password', '1234567\x7f', '1234567\x00', None, 12345678):
                response = await reset(admin, target, password)
                assert response.status_code == 422 and response.json()['detail'] == 'invalid_new_password'
                assert await row(admin, target['username']) == target
            for password in ('!!!!!!!!', 'A' * 128):
                assert (await reset(admin, await row(admin, target['username']), password)).status_code == 200
                async with async_client(app, None) as client:
                    assert (await login(client, target['username'], password)).json()['must_change_password'] is True
        records = [r for r in audit_lines(options['audit_dir']) if r['action'] == 'reset_password']
        assert {'not_admin', 'admin_read_only', 'cross_origin', 'user_version_conflict', 'invalid_new_password'} <= {r.get('reason') for r in records}
        assert all(r['result'] == 'denied' for r in records if r.get('reason'))
        for secret in (TEST_PASSWORD, TEMP_PASSWORD, 'password_hash', '!!!!!!!!', 'A' * 128):
            assert secret not in json.dumps(records)


async def test_concurrent_resets_and_password_change_share_version_checks():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app) as first, async_client(app, 'admin') as second, async_client(app, None) as alice:
            a, b = await row(first, 'alice'), await row(first, 'bob')
            responses = await asyncio.gather(reset(first, a, 'first-password'), reset(second, a, 'second-password'), reset(second, b))
            assert sorted(r.status_code for r in responses[:2]) == [200, 409]
            assert responses[2].status_code == 200
            winner = 'first-password' if responses[0].status_code == 200 else 'second-password'
            loser = 'second-password' if responses[0].status_code == 200 else 'first-password'
            assert (await login(alice, 'alice', loser)).status_code == 401
            me = (await login(alice, 'alice', winner)).json()
            stale = await row(first, 'alice')
            assert (await change_password(alice, me['user_version'], winner)).status_code == 200
            assert (await reset(second, stale)).status_code == 409
            assert (await login(alice, 'alice', PERSONAL_PASSWORD)).status_code == 200
            assert (await reset(first, await row(first, 'alice'))).status_code == 200
            me = (await login(alice, 'alice', TEMP_PASSWORD)).json()
            reading, resume = asyncio.Event(), asyncio.Event()
            async def slow_body():
                reading.set()
                await resume.wait()
                yield json.dumps({'current_password': TEMP_PASSWORD, 'new_password': PERSONAL_PASSWORD,
                    'confirm_password': PERSONAL_PASSWORD, 'expected_version': me['user_version']}).encode()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                         cookies={COOKIE_NAME: alice.cookies.get(COOKIE_NAME)}) as slow:
                saving = asyncio.create_task(slow.post('/api/auth/change-password', content=slow_body()))
                await asyncio.wait_for(reading.wait(), 2)
                assert (await reset(second, await row(second, 'alice'), 'latest-password')).status_code == 200
                resume.set()
                assert (await asyncio.wait_for(saving, 2)).status_code == 401
            assert (await login(alice, 'alice', PERSONAL_PASSWORD)).status_code == 401
            assert (await login(alice, 'alice', 'latest-password')).json()['must_change_password'] is True
        records = audit_lines(options['audit_dir'])
        assert any(r['action'] == 'change_password' and r['result'] == 'denied' for r in records)
        assert any(r['action'] == 'reset_password' and r.get('reason') == 'user_version_conflict' for r in records)


async def test_reset_closes_stream_but_running_turn_continues():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, _ = fixture(tmp)
        options['session_factory'] = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.12)
        app = make_test_app(**options)
        async with async_client(app) as admin, async_client(app, 'alice') as alice, async_client(app, 'bob') as bob:
            target = await row(admin, 'alice')
            run = (await alice.post('/api/runs')).json()['run_id']
            stream, other = await open_global_stream(alice), await open_global_stream(bob)
            await alice.post(f'/api/runs/{run}/messages', json={'text': '部署示例'})
            await wait_status(alice, run, 'RUNNING')
            assert (await reset(admin, target)).status_code == 200
            async def drain():
                async for _ in stream.aiter_bytes():
                    pass
            await asyncio.wait_for(drain(), 0.5)
            await stream.aclose()
            capacity = (await bob.get('/api/runs')).json()
            assert capacity['running_count'] == 1 and capacity['runs'] == []
            assert run not in json.dumps(capacity)
            _, pings = await collect_frames(other, max_pings=2)
            await other.aclose()
            assert pings == 2
            me = (await login(alice, 'alice', TEMP_PASSWORD)).json()
            assert (await change_password(alice, me['user_version'])).status_code == 200
            await login(alice, 'alice', PERSONAL_PASSWORD)
            await wait_status(alice, run, 'READY')
            frames, _ = await collect_frames(await alice.get(f'/api/runs/{run}/events'))
            events = [frame['event'] for frame in frames]
            assert 'turn.completed' in events and 'turn.stopped' not in events and 'session.ended' not in events
            assert (await admin.get(f'/api/runs/{run}')).status_code == 404


async def test_file_and_audit_failures_report_password_commit_truthfully():
    for failure in ('missing', 'corrupt', 'unreadable', 'unwritable', 'replace', 'prepared', 'success'):
        with tempfile.TemporaryDirectory() as tmp:
            path, options, app = fixture(tmp)
            async with async_client(app) as admin, async_client(app, 'alice') as alice:
                target = await row(admin, 'alice')
                original, cookie = path.read_bytes(), alice.cookies.get(COOKIE_NAME)
                if failure == 'missing':
                    path.unlink()
                elif failure == 'corrupt':
                    path.write_text('users: [broken')
                real_open, real_replace, real_record = Path.open, os.replace, ControlAudit.record
                def open_file(p, *args, **kwargs):
                    if p == path and (failure == 'unreadable' or (failure == 'unwritable' and args and args[0] == 'r+')):
                        raise PermissionError('injected user file failure')
                    return real_open(p, *args, **kwargs)
                def replace_file(src, dst):
                    if failure == 'replace' and Path(dst) == path:
                        raise OSError('injected replace failure')
                    return real_replace(src, dst)
                def record(audit, **kwargs):
                    if kwargs['action'] == 'reset_password' and kwargs['result'] == failure:
                        raise AuditWriteError('injected audit failure')
                    return real_record(audit, **kwargs)
                with patch.object(Path, 'open', open_file), patch('os.replace', replace_file), patch.object(ControlAudit, 'record', record):
                    response = await reset(admin, target, headers={'x-request-id': 'failure-case'})
                committed = failure == 'success'
                assert response.status_code == (200 if committed else 503), (failure, response.text)
                assert response.json() == ({'outcome': 'committed', 'audit_status': 'failed'} if committed else {
                    'outcome': 'not_committed', 'detail': 'audit_unavailable' if failure == 'prepared' else 'users_unavailable'})
                if failure == 'missing':
                    assert not path.exists()
                    path.write_bytes(original)
                elif failure == 'corrupt':
                    assert path.read_text() == 'users: [broken'
                    path.write_bytes(original)
                elif not committed:
                    assert path.read_bytes() == original
            restarted = make_test_app(**options)
            async with async_client(restarted) as admin, async_client(restarted, None) as client:
                current = await row(admin, 'alice')
                assert current['enabled'] is True and current['created_at'] == target['created_at']
                client.cookies.set(COOKIE_NAME, cookie)
                assert (await client.get('/api/runs')).status_code == (401 if committed else 200)
                client.cookies.clear()
                assert (await login(client, 'alice', TEST_PASSWORD if committed else TEMP_PASSWORD)).status_code == 401
                result = await login(client, 'alice', TEMP_PASSWORD if committed else TEST_PASSWORD)
                assert result.status_code == 200 and result.json()['must_change_password'] is committed
            records = [r for r in audit_lines(options['audit_dir']) if r['request_id'] == 'failure-case']
            assert all(r['result'] != 'success' for r in records)
            assert all(r['target_username'] == 'alice' and r['actor'] == 'tester' for r in records)
            assert [r['result'] for r in records] == (['prepared'] if committed else
                ['prepared', 'failure'] if failure in ('unwritable', 'replace') else ['failure'])


async def test_reset_rechecks_administrator_after_request_body_and_respects_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        path, options, app = fixture(tmp)
        async with async_client(app) as admin:
            target = await row(admin, 'alice')
            reading, resume = asyncio.Event(), asyncio.Event()
            async def slow_body():
                reading.set()
                await resume.wait()
                yield json.dumps({'username': 'alice', 'expected_version': target['user_version'], 'password': TEMP_PASSWORD}).encode()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                         cookies={COOKIE_NAME: admin.cookies.get(COOKIE_NAME)}) as slow:
                saving = asyncio.create_task(slow.post(RESET, content=slow_body()))
                await asyncio.wait_for(reading.wait(), 2)
                data = yaml.safe_load(path.read_text())
                data['users']['tester']['role'] = 'user'
                path.write_text(yaml.safe_dump(data))
                resume.set()
                assert (await asyncio.wait_for(saving, 2)).status_code == 401
        async with async_client(app, 'admin') as admin:
            assert await row(admin, 'alice') == target
    for failure in ('corrupt', 'write'):
        with tempfile.TemporaryDirectory() as tmp:
            path, options, _ = fixture(tmp)
            original = path.read_bytes()
            options['state_path'].write_text('broken' if failure == 'corrupt' else '{}')
            app = make_test_app(**options)
            async with async_client(app) as admin:
                target = await row(admin, 'alice')
                real_replace = os.replace
                def replace_file(src, dst):
                    if failure == 'write' and Path(dst) == options['state_path']:
                        raise OSError('injected state failure')
                    return real_replace(src, dst)
                with patch('os.replace', replace_file):
                    response = await reset(admin, target)
                assert response.status_code == 503
                assert response.json() == {'outcome': 'not_committed', 'detail': 'state_unavailable'}
            assert path.read_bytes() == original


async def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith('test_')]
    for test in tests:
        await test()
        print('ok', test.__name__)
    print(f'{len(tests)} passed')


if __name__ == '__main__':
    asyncio.run(main())
