"""跨操作验收：用户生命周期、部署升级与故障后的恢复；只用临时文件和假会话。"""
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

from web.audit import AuditWriteError, ControlAudit
from web.auth import COOKIE_NAME
from web.tests.support import TEST_PASSWORD, async_client, audit_lines, make_test_app
from web.tests.test_user_access import fixture, row, change, USERS
from web.tests.test_reset_password import reset
from web.tests.test_stream_isolation import wait_status

INITIAL = 'initial-password!'
PERSONAL = 'personal-password!'
RESET = 'reset-password!'
FINAL = 'final-password!'
CHANGE = '/api/auth/change-password'


async def login(client, name, password):
    response = await client.post('/api/auth/login', json={'username': name, 'password': password})
    assert response.status_code == 200, response.text
    return response.json()


async def password_form(client, current, new):
    me = (await client.get('/api/auth/me')).json()
    return dict(current_password=current, new_password=new, confirm_password=new,
                expected_version=me['user_version'])


async def assert_restricted(app, client):
    for route in app.routes:
        if not route.path.startswith('/api/') or route.path.startswith('/api/auth/'):
            continue
        path = route.path.replace('{run_id}', 'missing').replace('{task_id}', 'missing')
        path = path.replace('{rel_path:path}', 'deploy/missing.md')
        for method in route.methods:
            response = await client.request(method, path)
            assert response.status_code == 403, (method, path, response.text)


async def test_complete_lifecycle_with_two_admins_owner_isolation_and_restart():
    with tempfile.TemporaryDirectory() as tmp:
        path, options, _ = fixture(tmp)
        options['default_owner'] = 'alice'
        artifact = Path(tmp) / 'deploy' / 'result.md'
        artifact.parent.mkdir()
        artifact.write_text('shared lifecycle artifact')
        options['artifact_roots'] = {'deploy': artifact.parent}
        options['obs_list_fn'] = lambda **kwargs: {'objects': [{'key': 'shared-result.md'}]}
        options['obs_health_fn'] = lambda: {'ok': True}
        app = make_test_app(**options)
        revoked = []
        async with async_client(app) as first, async_client(app, 'admin') as second, \
                async_client(app, 'alice') as other, async_client(app, None) as user:
            for admin in (first, second):
                for name in ('tester', 'admin'):
                    target = await row(admin, name)
                    for response in (await change(admin, target, False),
                                     await change(admin, target, True), await reset(admin, target)):
                        assert response.status_code == 403
                        assert response.json()['detail'] == 'admin_read_only'
            assert (await other.get(USERS)).status_code == 403
            assert (await other.post('/api/obs/config', json={})).status_code == 403
            result = await first.post(USERS, json={'username': 'new-user', 'password': INITIAL,
                'role': 'admin', 'must_change_password': False})
            assert result.status_code == 201
            created = await row(first, 'new-user')
            assert created['role'] == 'user' and created['created_at']
            assert (await login(user, 'new-user', INITIAL))['must_change_password'] is True
            await assert_restricted(app, user)
            revoked.append(user.cookies.get(COOKIE_NAME))
            assert (await user.post(CHANGE, json=await password_form(user, INITIAL, PERSONAL))).status_code == 200
            assert (await user.get('/api/runs')).status_code == 401
            await login(user, 'new-user', PERSONAL)
            for client in (first, second, other, user):
                shared = await client.get('/api/artifacts/file/deploy/result.md')
                assert shared.status_code == 200 and shared.json()['content'] == 'shared lifecycle artifact'
                assert (await client.get('/api/obs/objects')).json()['objects'] == [{'key': 'shared-result.md'}]
                is_admin = client in (first, second)
                assert (await client.get('/api/obs/config')).json()['can_write'] is is_admin
                response = await client.post('/api/obs/config', json={'bucket': 'local-bucket'})
                assert response.status_code == (200 if is_admin else 403)
            run = (await user.post('/api/runs', json={})).json()['run_id']
            assert (await user.post(f'/api/runs/{run}/messages', json={'text': 'local lifecycle'})).status_code == 200
            await wait_status(user, run, 'READY')
            other_run = (await other.post('/api/runs', json={})).json()['run_id']
            for client, target_run in ((first, run), (second, run), (other, run), (user, other_run)):
                for suffix, body in (('', None), ('/events', None), ('/messages', {'text': 'forbidden'}),
                                     ('/stop', {}), ('/clone', {}), ('/end', {})):
                    response = await client.get(f'/api/runs/{target_run}{suffix}') if body is None else \
                        await client.post(f'/api/runs/{target_run}{suffix}', json=body)
                    assert response.status_code == 404
            revoked.append(user.cookies.get(COOKIE_NAME))
            assert (await change(first, await row(first, 'new-user'), False)).status_code == 200
            assert (await user.get('/api/runs')).status_code == 401
            assert (await change(second, await row(second, 'new-user'), True)).status_code == 200
            assert (await user.get('/api/runs')).status_code == 401
            await login(user, 'new-user', PERSONAL)
            assert (await user.get(f'/api/runs/{run}')).status_code == 200
            revoked.append(user.cookies.get(COOKIE_NAME))
            assert (await reset(second, await row(second, 'new-user'), RESET)).status_code == 200
            assert (await user.get('/api/runs')).status_code == 401
            assert (await login(user, 'new-user', RESET))['must_change_password'] is True
            await assert_restricted(app, user)
            revoked.append(user.cookies.get(COOKIE_NAME))
            assert (await user.post(CHANGE, json=await password_form(user, RESET, FINAL))).status_code == 200
            await login(user, 'new-user', FINAL)
            assert (await other.get(f'/api/runs/{other_run}')).status_code == 200
            before_restart = (await first.get(USERS)).json()

        restarted = make_test_app(**options)
        async with async_client(restarted) as admin, async_client(restarted, 'new-user', FINAL) as user:
            assert (await admin.get(USERS)).json() == before_restart
            assert (await row(admin, 'new-user'))['created_at'] == created['created_at']
            assert (await row(admin, 'alice'))['created_at'] is None
            assert (await user.get('/api/auth/me')).json()['must_change_password'] is False
            assert (await user.get(f'/api/runs/{run}')).status_code == 200
            fork = await user.post(f'/api/runs/{run}/clone', json={})
            assert fork.status_code == 200, fork.text
            fork_id = fork.json()['run_id']
            assert fork_id != run
            assert (await admin.get(f'/api/runs/{fork_id}')).status_code == 404
            assert (await user.post(f'/api/runs/{fork_id}/end')).status_code == 200
            assert (await user.get(f'/api/runs/{fork_id}')).json()['status'] == 'ENDED'
            assert (await user.get(f'/api/runs/{run}')).json()['status'] != 'ENDED'
            for cookie in revoked:
                async with async_client(restarted, None) as old:
                    old.cookies.set(COOKIE_NAME, cookie)
                    assert (await old.get('/api/auth/me')).status_code == 401
            public = json.dumps(before_restart) + (await user.get('/api/auth/me')).text
        records = [r for r in audit_lines(options['audit_dir'])
                   if r.get('target_username') == 'new-user']
        assert [(r['action'], r['result']) for r in records] == [
            (action, result) for action in ('create_user', 'change_password', 'disable_user',
                'enable_user', 'reset_password', 'change_password') for result in ('prepared', 'success')]
        for prepared, success in zip(records[::2], records[1::2]):
            assert prepared['request_id'] == success['request_id']
            assert prepared['actor'] == success['actor']
        for secret in (INITIAL, PERSONAL, RESET, FINAL, *revoked, 'password_hash', 'pbkdf2_sha256'):
            assert secret not in json.dumps(records) + public
        for secret in (INITIAL, PERSONAL, RESET, FINAL):
            assert secret not in path.read_text()


async def test_interleaved_access_reset_and_password_forms_keep_latest_state():
    with tempfile.TemporaryDirectory() as tmp:
        _, options, app = fixture(tmp)
        async with async_client(app) as first, async_client(app, 'admin') as second, \
                async_client(app, None) as user:
            assert (await first.post(USERS, json={'username': 'pending', 'password': INITIAL})).status_code == 201
            await login(user, 'pending', INITIAL)
            stale_form = await password_form(user, INITIAL, PERSONAL)
            stale_row = await row(first, 'pending')
            assert (await change(second, stale_row, False)).status_code == 200
            assert (await reset(first, stale_row, RESET)).status_code == 409
            assert (await reset(first, await row(first, 'pending'), RESET)).status_code == 200
            assert (await row(first, 'pending'))['enabled'] is False
            assert (await user.post(CHANGE, json=stale_form)).status_code == 401
            assert (await change(second, await row(second, 'pending'), True)).status_code == 200
            assert (await user.post(CHANGE, json=stale_form)).status_code == 401
            await login(user, 'pending', RESET)
            stale_row = await row(first, 'pending')
            # 不同目标的启停、重置与改密不应产生冲突或丢失更新。
            replies = await asyncio.gather(
                user.post(CHANGE, json=await password_form(user, RESET, FINAL)),
                change(first, await row(first, 'alice'), False),
                reset(second, await row(second, 'bob'), RESET),
                first.post(USERS, json={'username': 'same', 'password': INITIAL}),
                second.post(USERS, json={'username': 'same', 'password': PERSONAL}),
            )
            assert [r.status_code for r in replies[:3]] == [200, 200, 200]
            assert sorted(r.status_code for r in replies[3:]) == [201, 409]
            assert (await change(second, stale_row, False)).status_code == 409
            assert (await reset(first, stale_row, INITIAL)).status_code == 409
        restarted = make_test_app(**options)
        async with async_client(restarted) as admin, async_client(restarted, None) as user:
            assert (await row(admin, 'alice'))['enabled'] is False
            assert (await login(user, 'bob', RESET))['must_change_password'] is True
            assert (await login(user, 'pending', FINAL))['must_change_password'] is False
            winner = INITIAL if replies[3].status_code == 201 else PERSONAL
            assert (await login(user, 'same', winner))['must_change_password'] is True
            rows = (await admin.get(USERS)).json()['users']
            assert len(rows) == 7 and sum(r['username'] == 'same' for r in rows) == 1


async def test_faults_across_disable_reset_and_change_match_persistence_and_audit():
    with tempfile.TemporaryDirectory() as tmp:
        path, options, app = fixture(tmp)
        async with async_client(app) as admin, async_client(app, 'alice') as user:
            cookie = user.cookies.get(COOKIE_NAME)
            original = path.read_bytes()
            record = ControlAudit.record

            def reject_prepared(audit, **kwargs):
                if kwargs['action'] == 'disable_user' and kwargs['result'] == 'prepared':
                    raise AuditWriteError('injected prepare failure')
                return record(audit, **kwargs)

            with patch.object(ControlAudit, 'record', reject_prepared):
                response = await change(admin, await row(admin, 'alice'), False,
                                        headers={'x-request-id': 'disable-failed'})
            assert response.status_code == 503 and response.json()['outcome'] == 'not_committed'
            assert path.read_bytes() == original
            assert (await user.get('/api/runs')).status_code == 200

            replace = os.replace

            def reject_users_replace(src, dst):
                if Path(dst) == path:
                    raise OSError('injected replace failure')
                return replace(src, dst)

            with patch('os.replace', reject_users_replace):
                response = await reset(admin, await row(admin, 'alice'), RESET,
                                       headers={'x-request-id': 'reset-failed'})
            assert response.status_code == 503 and response.json()['outcome'] == 'not_committed'
            assert path.read_bytes() == original

            def reject_success(audit, **kwargs):
                if kwargs['action'] in ('reset_password', 'change_password') and kwargs['result'] == 'success':
                    raise AuditWriteError('injected result failure')
                return record(audit, **kwargs)

            with patch.object(ControlAudit, 'record', reject_success):
                response = await reset(admin, await row(admin, 'alice'), RESET,
                                       headers={'x-request-id': 'reset-committed'})
                assert response.status_code == 200
                assert response.json() == {'outcome': 'committed', 'audit_status': 'failed'}
                assert (await user.get('/api/auth/me')).status_code == 401
                assert (await login(user, 'alice', RESET))['must_change_password'] is True
                response = await user.post(CHANGE, json=await password_form(user, RESET, FINAL),
                                           headers={'x-request-id': 'change-committed'})
                assert response.status_code == 200
                assert response.json() == {'outcome': 'committed', 'audit_status': 'failed'}
        restarted = make_test_app(**options)
        async with async_client(restarted, None) as user, async_client(restarted) as admin:
            user.cookies.set(COOKIE_NAME, cookie)
            assert (await user.get('/api/auth/me')).status_code == 401
            for password in (TEST_PASSWORD, RESET):
                assert (await user.post('/api/auth/login', json={'username': 'alice', 'password': password})).status_code == 401
            assert (await login(user, 'alice', FINAL))['must_change_password'] is False
            assert (await row(admin, 'alice'))['enabled'] is True
        records = audit_lines(options['audit_dir'])
        for request_id, expected in (
            ('disable-failed', ['failure']), ('reset-failed', ['prepared', 'failure']),
            ('reset-committed', ['prepared']), ('change-committed', ['prepared']),
        ):
            chain = [r for r in records if r['request_id'] == request_id]
            assert [r['result'] for r in chain] == expected
            assert all(r['target_username'] == 'alice' for r in chain)
        for secret in (TEST_PASSWORD, RESET, FINAL, cookie, 'password_hash'):
            assert secret not in json.dumps(records)


async def test_stopped_upgrade_backup_manual_admin_and_invalid_role_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        path, options, _ = fixture(tmp)
        document = yaml.safe_load(path.read_text())
        for entry in document['users'].values():
            entry.pop('role')
        path.write_text(yaml.safe_dump(document))
        options['default_owner'] = 'admin'
        app = make_test_app(**options)
        async with async_client(app, 'admin') as legacy:
            assert (await legacy.get('/api/auth/me')).json()['can_manage_users'] is False
            assert (await legacy.get('/api/runs')).status_code == 200
            assert (await legacy.get(USERS)).status_code == 403
            run = (await legacy.post('/api/runs', json={})).json()['run_id']
            assert (await legacy.post(f'/api/runs/{run}/messages', json={'text': 'before upgrade'})).status_code == 200
            await wait_status(legacy, run, 'READY')
        # 客户端与回合均已结束后才维护；仅备份临时环境，包含初始化标记。
        backup = Path(tmp) / 'backup'
        backup.mkdir()
        sources = [p for p in Path(tmp).iterdir() if p != backup]
        for source in sources:
            if source.is_dir():
                shutil.copytree(source, backup / source.name)
            else:
                shutil.copy2(source, backup / source.name)
        assert (backup / path.name).read_bytes() == path.read_bytes()
        assert (backup / options['state_path'].name).read_bytes() == options['state_path'].read_bytes()
        document['users']['tester']['role'] = 'admin'
        path.write_text(yaml.safe_dump(document))
        upgraded = make_test_app(**options)
        async with async_client(upgraded) as admin, async_client(upgraded, 'admin') as legacy:
            assert (await admin.get('/api/auth/me')).json()['can_manage_users'] is True
            assert all(r['created_at'] is None for r in (await admin.get(USERS)).json()['users'])
            assert (await legacy.get(f'/api/runs/{run}')).status_code == 200
            assert (await admin.get(f'/api/runs/{run}')).status_code == 404
        valid = path.read_bytes()
        for role in ('root', None, True, [], {}):
            document['users']['tester']['role'] = role
            path.write_text(yaml.safe_dump(document))
            broken = make_test_app(**options)
            async with async_client(broken, None) as user:
                response = await user.post('/api/auth/login', json={'username': 'admin', 'password': TEST_PASSWORD})
                assert response.status_code == 401
            path.write_bytes(valid)
        document = yaml.safe_load(valid)
        document['users']['tester']['enabled'] = False
        path.write_text(yaml.safe_dump(document))
        no_admin = make_test_app(**options)
        async with async_client(no_admin, 'admin') as user:
            assert (await user.get(f'/api/runs/{run}')).status_code == 200
            assert (await user.get(USERS)).status_code == 403
        # 再次停用所有客户端后，运维恢复管理员原记录，无 Web 接管入口。
        path.write_bytes(valid)
        restored = make_test_app(**options)
        async with async_client(restored) as admin, async_client(restored, 'admin') as legacy:
            assert (await admin.get(USERS)).status_code == 200
            assert (await legacy.get(f'/api/runs/{run}')).status_code == 200
            assert (await legacy.get('/api/auth/me')).json()['must_change_password'] is False


async def main():
    for name, test in sorted(globals().items()):
        if name.startswith('test_'):
            await test()
            print('ok', name)


if __name__ == '__main__':
    asyncio.run(main())
