"""用户变更命令：复核、版本冲突、准备/结果审计和明确的提交结果。"""
import logging
import re
from datetime import datetime, timezone

from . import auth
from .audit import AuditWriteError
from .users import UserAlreadyExists, UserFileError, UserVersionConflict

logger = logging.getLogger('web')


class UserChangeError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def valid_new_password(password):
    return (isinstance(password, str) and 8 <= len(password) <= 128
            and all('!' <= char <= '~' for char in password))


class UserChanges:
    def __init__(self, roster, audit, secret):
        self.roster, self.audit, self.secret = roster, audit, secret

    def _record(self, actor, target, action, request_id, result, reason=None):
        self.audit.record(actor=actor, target_username=target, action=action,
                          request_id=request_id, result=result, reason=reason)

    def denied(self, actor, target, action, request_id, reason):
        try:
            self._record(actor, target, action, request_id, 'denied', reason)
        except AuditWriteError:
            logger.warning('用户变更拒绝审计不可用')

    def _commit(self, actor, target, action, request_id, expected_version, transform, *, create=False):
        def record(result, reason=None):
            self._record(actor, target, action, request_id, result, reason)

        try:
            self.roster.update_user(target, expected_version, transform,
                                    lambda: record('prepared'), create=create)
        except (UserChangeError, UserVersionConflict, UserAlreadyExists, UserFileError, AuditWriteError) as exc:
            if isinstance(exc, UserVersionConflict):
                exc = UserChangeError(409, 'user_version_conflict')
            elif isinstance(exc, UserAlreadyExists):
                exc = UserChangeError(409, 'username_exists')
            elif isinstance(exc, UserFileError):
                exc = UserChangeError(503, 'users_unavailable')
            elif isinstance(exc, AuditWriteError):
                exc = UserChangeError(503, 'audit_unavailable')
            try:
                record('failure' if exc.status == 503 else 'denied', exc.detail)
            except AuditWriteError:
                logger.warning('用户变更结果审计不可用（未提交）')
            raise exc
        try:
            record('success')
        except AuditWriteError:
            logger.error('用户变更已提交，结果审计不可用')
            return {'outcome': 'committed', 'audit_status': 'failed'}
        return {'outcome': 'committed', 'audit_status': 'recorded'}

    def _require_admin(self, actor, token, check_writable):
        """仅在目标 transform 内调用，与版本复核和提交共用清单锁。"""
        username, reason = auth.verify_token(self.secret, token, self.roster)
        if username != actor:
            raise UserChangeError(401, reason or auth.REASON_BAD_COOKIE)
        if not self.roster.is_admin(actor):
            raise UserChangeError(403, 'not_admin')
        check_writable()

    def create_user(self, actor, token, body, request_id, check_writable):
        name = body.get('username')
        target = name if isinstance(name, str) else None

        def transform(entry):
            self._require_admin(actor, token, check_writable)
            if not isinstance(name, str) or re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) is None:
                raise UserChangeError(422, 'invalid_username')
            if not valid_new_password(body.get('password')):
                raise UserChangeError(422, 'invalid_new_password')
            return {'password_hash': auth.hash_password(body.get('password')),
                    'role': 'user', 'enabled': True, 'must_change_password': True,
                    'created_at': datetime.now(timezone.utc).isoformat()}

        return self._commit(actor, target, 'create_user', request_id, None, transform, create=True)

    def change_password(self, actor, token, body, request_id, check_writable):
        def transform(entry):
            # 此复核和文件替换共用清单锁；不信任提交前的 middleware 认证。
            username, reason = auth.verify_token(self.secret, token, self.roster)
            if username != actor:
                raise UserChangeError(401, reason or auth.REASON_BAD_COOKIE)
            if not self.roster.must_change_password(actor):
                raise UserChangeError(403, 'password_change_not_required')
            check_writable()
            current, new, confirm = (body.get(key) for key in
                                     ('current_password', 'new_password', 'confirm_password'))
            if not isinstance(current, str) or not auth.verify_password(current, entry.get('password_hash', '')):
                raise UserChangeError(422, 'current_password_incorrect')
            if not valid_new_password(new):
                raise UserChangeError(422, 'invalid_new_password')
            if new != confirm:
                raise UserChangeError(422, 'password_confirmation_mismatch')
            if new == current:
                raise UserChangeError(422, 'password_unchanged')
            entry.update(password_hash=auth.hash_password(new), must_change_password=False)
            return entry

        return self._commit(actor, actor, 'change_password', request_id,
                            body.get('expected_version'), transform)

    def set_enabled(self, actor, token, body, request_id, check_writable, *, enabled):
        name = body.get('username')
        target = name if isinstance(name, str) else None

        def transform(entry):
            self._require_admin(actor, token, check_writable)
            if entry is None:
                raise UserChangeError(404, 'no_such_user')
            if entry.get('role', 'user') == 'admin':
                raise UserChangeError(403, 'admin_read_only')
            entry['enabled'] = enabled
            return entry

        return self._commit(actor, target, 'enable_user' if enabled else 'disable_user', request_id,
                            body.get('expected_version'), transform)

    def reset_password(self, actor, token, body, request_id, check_writable):
        name = body.get('username')
        target = name if isinstance(name, str) else None

        def transform(entry):
            self._require_admin(actor, token, check_writable)
            if entry is None:
                raise UserChangeError(404, 'no_such_user')
            if entry.get('role', 'user') == 'admin':
                raise UserChangeError(403, 'admin_read_only')
            if not valid_new_password(body.get('password')):
                raise UserChangeError(422, 'invalid_new_password')
            entry.update(password_hash=auth.hash_password(body['password']), must_change_password=True)
            return entry

        return self._commit(actor, target, 'reset_password', request_id,
                            body.get('expected_version'), transform)
