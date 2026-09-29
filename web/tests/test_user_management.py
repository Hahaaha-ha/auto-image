"""角色授权与只读用户清单的 ASGI 行为验证。"""
import asyncio
import json
import tempfile
from pathlib import Path

from web.auth import hash_password
from web.tests.support import async_client, make_test_app


async def test_role_authorized_list():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'users.json'
        password = '旧 密码'
        entries = {name: {'password_hash': hash_password(password, 1000), **cfg}
                   for name, cfg in {
                       'operator': {'role': 'admin', 'created_at': '2026-09-01T02:00:00Z'},
                       'second': {'role': 'admin'},
                       'admin': {},
                       ' Legacy 用户 ': {'role': 'user'},
                       'disabled': {'role': 'user', 'enabled': False},
                   }.items()}
        path.write_text(json.dumps({'users': entries}))
        app = make_test_app(users_path=path, default_owner='admin')
        async with async_client(app, username=None) as anonymous:
            assert (await anonymous.get('/api/admin/users')).status_code == 401
        for name in ('operator', 'second', 'admin', ' Legacy 用户 '):
            async with async_client(app, username=name, password=password) as client:
                is_admin = name in ('operator', 'second')
                me = (await client.get('/api/auth/me')).json()
                assert me == {'username': name, 'can_manage_users': is_admin, 'must_change_password': False}
                assert (await client.get('/api/obs/config')).json()['can_write'] is is_admin
                response = await client.get('/api/admin/users')
                assert response.status_code == (200 if is_admin else 403)
                if is_admin:
                    rows = response.json()['users']
                    assert all(isinstance(row['user_version'], str) and row['user_version'] for row in rows)
                    assert [{k: v for k, v in row.items() if k != 'user_version'} for row in rows] == [dict(username=n, role=c.get('role', 'user'),
                                         enabled=c.get('enabled', True), created_at=c.get('created_at'))
                                    for n, c in entries.items()]
                else:
                    assert (await client.post('/api/obs/config', json={})).status_code == 403
                run_id = (await client.post('/api/runs')).json()['run_id']
                async with async_client(app, username='second', password=password) as other:
                    if name != 'second':
                        assert (await other.get(f'/api/runs/{run_id}')).status_code == 404
                        assert (await other.post(f'/api/runs/{run_id}/end')).status_code == 404
        async with async_client(app, username=None) as client:
            for wrong in ('legacy 用户', 'Legacy 用户', 'OPERATOR'):
                assert (await client.post('/api/auth/login', json={'username': wrong, 'password': password})).status_code == 401


async def test_invalid_role_invalidates_whole_roster():
    for role in ('root', None, 1, True, [], {}):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'users.json'
            path.write_text(json.dumps({'users': {
                'valid': {'password_hash': hash_password('pw', 1000)},
                'invalid': {'role': role},
            }}))
            app = make_test_app(users_path=path)
            async with async_client(app, username=None) as client:
                assert (await client.post('/api/auth/login', json={'username': 'valid', 'password': 'pw'})).status_code == 401


async def test_no_enabled_admin_keeps_legacy_business_available():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'users.yaml'
        password_hash = hash_password('旧', 1000)
        path.write_text(f"""users:
  admin:
    password_hash: {password_hash}
  disabled:
    role: admin
    enabled: false
  observer:
    role: user
    password_hash: {password_hash}
    created_at: 2026-09-01T02:00:00Z
""")
        app = make_test_app(users_path=path, default_owner='admin')
        async with async_client(app, username='admin', password='旧') as client:
            assert (await client.get('/api/runs')).status_code == 200
            assert (await client.get('/api/auth/me')).json()['can_manage_users'] is False
            assert (await client.get('/api/admin/users')).status_code == 403
            assert (await client.post('/api/obs/config', json={})).status_code == 403


async def test_all_admins_can_write_obs_without_changing_owners():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'users.json'
        path.write_text(json.dumps({'users': {
            name: {'password_hash': hash_password('pw', 1000), 'role': 'admin'}
            for name in ('first', 'second')
        }}))
        scope = Path(tmp) / 'scope.yaml'
        scope.write_text('obs: {}\n')
        app = make_test_app(users_path=path, scope_config=scope, default_owner='someone_else',
                            obs_health_fn=lambda: {'ok': True})
        for name in ('first', 'second'):
            async with async_client(app, username=name, password='pw') as client:
                response = await client.post('/api/obs/config', json={'bucket': name})
                assert response.status_code == 200, response.text
                assert (await client.get('/api/obs/config')).json()['bucket'] == name


async def main():
    await test_role_authorized_list()
    await test_invalid_role_invalidates_whole_roster()
    await test_no_enabled_admin_keeps_legacy_business_available()
    await test_all_admins_can_write_obs_without_changing_owners()
    print('4 passed')


if __name__ == '__main__':
    asyncio.run(main())
