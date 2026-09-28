"""用户变更命令：复核、版本冲突、准备/结果审计和明确的提交结果。"""
import logging

from . import auth
from .audit import AuditWriteError
from .users import UserFileError, UserVersionConflict

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

    def _commit(self, actor, target, action, request_id, expected_version, transform):
        def record(result, reason=None):
            self._record(actor, target, action, request_id, result, reason)

        try:
            self.roster.update_user(target, expected_version, transform,
                                    lambda: record('prepared'))
        except (UserChangeError, UserVersionConflict, UserFileError, AuditWriteError) as exc:
            if isinstance(exc, UserVersionConflict):
                exc = UserChangeError(409, 'user_version_conflict')
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
