"""用户启停的 ASGI、持久撤销、并发与故障结果契约。"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml
import httpx

from web.auth import COOKIE_NAME
from web.audit import AuditWriteError, ControlAudit
from web.fake import FakeSessionFactory, DEFAULT_SCRIPT
from web.tests.support import TEST_PASSWORD, async_client, audit_lines, make_test_app, write_test_users
from web.tests.test_stream_isolation import open_global_stream, collect_frames, wait_status

USERS = '/api/admin/users'


def fixture(tmp):
    root = Path(tmp)
    path = write_test_users(root / 'users.yaml')
    factory = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02)
    options = dict(users_path=path, audit_dir=root / 'audit', state_path=root / 'state.json',
                   session_factory=factory, list_sessions_fn=factory.list_sessions,
                   get_session_messages_fn=factory.get_session_messages)
    return path, options, make_test_app(**options)


async def row(client, username):
    response = await client.get(USERS)
    assert response.status_code == 200, response.text
    return next(user for user in response.json()['users'] if user['username'] == username)


async def change(client, user, enabled, **kwargs):
    return await client.post(f'{USERS}/{"enable" if enabled else "disable"}',
                            json={'username': user['username'], 'expected_version': user['user_version']}, **kwargs)


async def test_disable_enable_restart_never_revives_old_login():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app) as admin, async_client(app, 'alice') as alice, async_client(app, 'bob') as bob:
            original = await row(admin, 'alice')
            cookie = alice.cookies.get(COOKIE_NAME)
            run = (await alice.post('/api/runs')).json()['run_id']
            await alice.post(f'/api/runs/{run}/messages', json={'text': '部署示例'})
            await wait_status(alice, run, 'READY')
            response = await change(admin, original, False)
            assert response.status_code == 200, response.text
            assert response.json() == {'outcome': 'committed', 'audit_status': 'recorded'}
            assert (await alice.get('/api/runs')).status_code == 401
            assert (await bob.get('/api/runs')).status_code == 200
            assert (await alice.post('/api/auth/login', json={'username': 'alice', 'password': TEST_PASSWORD})).status_code == 401
            disabled = await row(admin, 'alice')
            assert disabled['enabled'] is False and disabled['created_at'] == original['created_at'] is None
        async with async_client(make_test_app(**options)) as admin:
            assert await row(admin, 'alice') == disabled
            assert (await change(admin, disabled, True)).status_code == 200
        async with async_client(make_test_app(**options), username=None) as alice:
            alice.cookies.set(COOKIE_NAME, cookie)
            assert (await alice.get('/api/runs')).status_code == 401
            alice.cookies.clear()
            assert (await alice.post('/api/auth/login', json={'username': 'alice', 'password': TEST_PASSWORD})).status_code == 200
            assert [r['run_id'] for r in (await alice.get('/api/runs')).json()['runs']] == [run]
        records = [r for r in audit_lines(options['audit_dir']) if r['action'] in ('enable_user', 'disable_user')]
        assert [(r['action'], r['result']) for r in records] == [
            ('disable_user', 'prepared'), ('disable_user', 'success'),
            ('enable_user', 'prepared'), ('enable_user', 'success')]
        assert records[0]['request_id'] == records[1]['request_id']
        assert records[2]['request_id'] == records[3]['request_id']
        assert all(r['actor'] == 'tester' and r['target_username'] == 'alice' for r in records)
        assert cookie not in json.dumps(records)


async def test_permissions_exact_legacy_names_and_target_version():
    with tempfile.TemporaryDirectory() as tmp:
        path, options, _ = fixture(tmp)
        data = json.loads(path.read_text())
        data['users'][' Legacy 用户 / '] = data['users'].pop('alice')
        data['users']['bob']['must_change_password'] = True
        path.write_text(json.dumps(data))
        app = make_test_app(**options)
        async with async_client(app) as admin:
            legacy = await row(admin, ' Legacy 用户 / ')
            for actor, status in [(None, 401), (' Legacy 用户 / ', 403), ('bob', 403)]:
                async with async_client(app, actor) as client:
                    for enabled in (False, True):
                        assert (await change(client, legacy, enabled)).status_code == status
            for name in ('tester', 'admin'):
                for enabled in (False, True):
                    response = await change(admin, await row(admin, name), enabled)
                    assert response.status_code == 403 and response.json()['detail'] == 'admin_read_only'
            assert (await change(admin, legacy, False, headers={'origin': 'http://evil.example'})).status_code == 403
            assert (await change(admin, legacy, False, headers={'origin': 'https://testserver'})).status_code == 403
            for target in ('Legacy 用户 /', 'LEGACY', [], None):
                assert (await admin.post(f'{USERS}/disable', json={'username': target})).status_code == 404
            for version in (None, 1, {}, 'stale'):
                response = await admin.post(f'{USERS}/disable', json={'username': legacy['username'], 'expected_version': version})
                assert response.status_code == 409
            assert await row(admin, legacy['username']) == legacy
            assert (await change(admin, legacy, False)).status_code == 200
            assert (await row(admin, legacy['username']))['enabled'] is False
        records = [r for r in audit_lines(options['audit_dir']) if r['action'] in ('enable_user', 'disable_user')]
        assert {'not_admin', 'admin_read_only', 'cross_origin', 'user_version_conflict'} <= {r.get('reason') for r in records}
        assert all(r['result'] == 'denied' for r in records if r.get('reason'))
        assert TEST_PASSWORD not in json.dumps(records) and 'password_hash' not in json.dumps(records)


async def test_concurrent_changes_conflict_only_for_same_user_and_keep_pending():
    with tempfile.TemporaryDirectory() as tmp:
        path, options, _ = fixture(tmp)
        data = json.loads(path.read_text())
        data['users']['alice'].update(must_change_password=True, created_at='2026-09-01T02:00:00Z')
        path.write_text(json.dumps(data))
        app = make_test_app(**options)
        async with async_client(app) as first, async_client(app, 'admin') as second, async_client(app, 'alice') as alice:
            a, b = await row(first, 'alice'), await row(first, 'bob')
            old_identity = (await alice.get('/api/auth/me')).json()
            cookie = alice.cookies.get(COOKIE_NAME)
            replies = await asyncio.gather(change(first, a, False), change(second, a, False), change(second, b, False))
            assert sorted(r.status_code for r in replies[:2]) == [200, 409]
            assert replies[2].status_code == 200
            assert (await row(first, 'bob'))['enabled'] is False
            a_disabled = await row(first, 'alice')
            assert (await change(first, a, True)).status_code == 409
            assert (await change(first, a_disabled, True)).status_code == 200
            assert (await alice.post('/api/auth/change-password', json={
                'current_password': TEST_PASSWORD, 'new_password': 'new-password!',
                'confirm_password': 'new-password!', 'expected_version': old_identity['user_version']})).status_code == 401
        restarted = make_test_app(**options)
        async with async_client(restarted) as admin, async_client(restarted, 'alice') as alice:
            assert (await row(admin, 'alice'))['created_at'] == a['created_at']
            assert (await alice.get('/api/auth/me')).json()['must_change_password'] is True
            assert (await alice.get('/api/runs')).status_code == 403
            alice.cookies.clear()
            alice.cookies.set(COOKIE_NAME, cookie)
            assert (await alice.get('/api/auth/me')).status_code == 401


async def test_disable_closes_stream_but_running_turn_and_capacity_continue():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, _ = fixture(tmp)
        options['session_factory'] = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.12)
        app = make_test_app(**options)
        async with async_client(app) as admin, async_client(app, 'alice') as alice, async_client(app, 'bob') as bob:
            a = await row(admin, 'alice')
            run = (await alice.post('/api/runs')).json()['run_id']
            stream = await open_global_stream(alice)
            other = await open_global_stream(bob)
            await alice.post(f'/api/runs/{run}/messages', json={'text': '部署示例'})
            await wait_status(alice, run, 'RUNNING')
            assert (await change(admin, a, False)).status_code == 200
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
            assert (await change(admin, await row(admin, 'alice'), True)).status_code == 200
            assert (await alice.get('/api/runs')).status_code == 401
            alice.cookies.clear()
            await alice.post('/api/auth/login', json={'username': 'alice', 'password': TEST_PASSWORD})
            await wait_status(alice, run, 'READY')
            snapshot = await alice.get(f'/api/runs/{run}/events')
            frames, _ = await collect_frames(snapshot)
            types = [frame['event'] for frame in frames]
            assert 'turn.completed' in types and 'turn.stopped' not in types and 'session.ended' not in types
            assert (await admin.get(f'/api/runs/{run}')).status_code == 404
            assert (await bob.get('/api/runs')).json()['running_count'] == 0


async def test_file_and_audit_failures_preserve_or_report_committed_access():
    for enabled in (False, True):
        for failure in ('missing', 'corrupt', 'unreadable', 'unwritable', 'replace', 'prepared', 'success'):
            with tempfile.TemporaryDirectory() as tmp:
                path, options, app = fixture(tmp)
                async with async_client(app) as admin, async_client(app, 'alice') as alice:
                    if enabled:
                        assert (await change(admin, await row(admin, 'alice'), False)).status_code == 200
                    target = await row(admin, 'alice')
                    original = path.read_bytes()
                    cookie = alice.cookies.get(COOKIE_NAME)
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
                        if kwargs['action'] in ('enable_user', 'disable_user') and kwargs['result'] == failure:
                            raise AuditWriteError('injected audit failure')
                        return real_record(audit, **kwargs)
                    with patch.object(Path, 'open', open_file), patch('os.replace', replace_file), patch.object(ControlAudit, 'record', record):
                        response = await change(admin, target, enabled, headers={'x-request-id': 'failure-case'})
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
                async with async_client(restarted) as admin, async_client(restarted, username=None) as old:
                    current = await row(admin, 'alice')
                    assert current['enabled'] is (enabled if committed else not enabled)
                    assert current['created_at'] is None
                    old.cookies.set(COOKIE_NAME, cookie)
                    assert (await old.get('/api/runs')).status_code == (200 if not enabled and not committed else 401)
                records = [r for r in audit_lines(options['audit_dir']) if r['request_id'] == 'failure-case']
                assert all(r['result'] != 'success' for r in records)
                assert all(r['target_username'] == 'alice' and r['actor'] == 'tester' for r in records)
                assert [r['result'] for r in records] == (['prepared'] if committed else
                    ['prepared', 'failure'] if failure in ('unwritable', 'replace') else ['failure'])


async def test_password_change_wins_over_stale_disable_and_identity_is_rechecked():
    with tempfile.TemporaryDirectory() as tmp:
        path, options, _ = fixture(tmp)
        data = json.loads(path.read_text())
        data['users']['alice']['must_change_password'] = True
        path.write_text(json.dumps(data))
        app = make_test_app(**options)
        async with async_client(app) as admin, async_client(app, 'alice') as alice:
            a = await row(admin, 'alice')
            me = (await alice.get('/api/auth/me')).json()
            assert (await alice.post('/api/auth/change-password', json={'current_password': TEST_PASSWORD,
                'new_password': 'personal-password', 'confirm_password': 'personal-password',
                'expected_version': me['user_version']})).status_code == 200
            assert (await change(admin, a, False)).status_code == 409
            a = await row(admin, 'alice')
            reading, resume = asyncio.Event(), asyncio.Event()
            async def slow_body():
                reading.set()
                await resume.wait()
                yield json.dumps({'username': 'alice', 'expected_version': a['user_version']}).encode()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                         cookies={COOKIE_NAME: admin.cookies.get(COOKIE_NAME)}) as slow:
                saving = asyncio.create_task(slow.post(f'{USERS}/disable', content=slow_body()))
                await asyncio.wait_for(reading.wait(), 2)
                data = yaml.safe_load(path.read_text())
                data['users']['tester']['role'] = 'user'
                path.write_text(yaml.safe_dump(data))
                resume.set()
                assert (await asyncio.wait_for(saving, 2)).status_code == 401
        async with async_client(app, 'admin') as admin:
            assert (await row(admin, 'alice'))['enabled'] is True


async def test_restricted_recovery_and_state_write_failure_block_access_changes():
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
                    response = await change(admin, target, False)
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
