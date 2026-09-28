"""用户文件：兼容读取、按用户版本串行提交，人工维护须停服。"""
import copy
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
from pathlib import Path

import yaml

logger = logging.getLogger('web')


class UserFileError(RuntimeError):
    """用户清单不可安全读取或替换。"""


class UserVersionConflict(RuntimeError):
    """提交引用的用户版本已过期。"""


class UserRoster:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._mtime = None
        self._data = {}
        self._document = None
        self._reload()

    def _read(self):
        try:
            document = yaml.safe_load(self.path.read_text(encoding='utf-8'))
            if not isinstance(document, dict) or not isinstance(document.get('users'), dict):
                raise ValueError('users mapping required')
            for name, cfg in document['users'].items():
                if not isinstance(name, str) or not isinstance(cfg, dict):
                    raise ValueError('invalid user entry')
                if cfg.get('role', 'user') not in ('admin', 'user'):
                    raise ValueError('invalid role')
                if 'password_hash' in cfg and not isinstance(cfg['password_hash'], str):
                    raise ValueError('invalid password hash')
                for key in ('enabled', 'must_change_password'):
                    if key in cfg and not isinstance(cfg[key], bool):
                        raise ValueError('invalid user state')
                if 'revision' in cfg and (not isinstance(cfg['revision'], str) or not cfg['revision']):
                    raise ValueError('invalid revision')
            return document, self.path.stat().st_mtime_ns
        except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
            raise UserFileError('users unavailable') from exc

    def _reload(self, strict=False):
        try:
            document, mtime = self._read()
        except UserFileError:
            self._document, self._data, self._mtime = None, {}, None
            if strict:
                raise
            logger.warning('用户清单 %s 不可读或无效，登录被拒', self.path)
            return
        self._document, self._data, self._mtime = document, document['users'], mtime

    def _maybe_refresh(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._reload()

    def get(self, username):
        with self._lock:
            self._maybe_refresh()
            return copy.deepcopy(self._data.get(username))

    def is_admin(self, username):
        entry = self.get(username)
        return entry is not None and entry.get('enabled', True) and entry.get('role', 'user') == 'admin'

    def must_change_password(self, username):
        entry = self.get(username)
        return (entry is not None and entry.get('role', 'user') == 'user'
                and entry.get('must_change_password', False))

    def list_users(self):
        with self._lock:
            self._maybe_refresh()
            return [{'username': name, 'role': cfg.get('role', 'user'),
                     'enabled': cfg.get('enabled', True), 'created_at': cfg.get('created_at')}
                    for name, cfg in self._data.items()]

    def fingerprint(self, username):
        entry = self.get(username)
        if entry is None:
            return None
        # 保留旧记录的指纹算法；每次提交持久化新的 revision，其他用户不漂移。
        return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False,
                                        default=str).encode('utf-8')).hexdigest()[:16]

    def update_user(self, username, expected_version, transform, prepare):
        """同一把锁内重新读取、复核身份、检查版本、准备审计并提交。

        transform 接收目标条目的副本，复核当前身份与业务输入后返回新条目；
        prepare 必须成功才写文件。调用方只在返回后记录已提交的结果审计。
        所有用户写入口必须复用此方法，不能先在锁外验证再直接写文件。
        """
        with self._lock:
            self._reload(strict=True)
            updated = transform(copy.deepcopy(self._data.get(username)))
            if not isinstance(expected_version, str) or expected_version != self.fingerprint(username):
                raise UserVersionConflict()
            document = copy.deepcopy(self._document)
            updated['revision'] = secrets.token_hex(16)
            document['users'][username] = updated
            prepare()
            self._replace(document)
            # 原子替换是提交点；其后不做可能把已提交误报为未提交的读取。
            self._document, self._data, self._mtime = document, document['users'], None

    def _replace(self, document):
        temp = None
        try:
            # 替换需要目录写权限；同时尊重原文件的只读设置。
            with self.path.open('r+', encoding='utf-8'):
                pass
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix=f'.{self.path.name}.', delete=False) as handle:
                temp = Path(handle.name)
                yaml.safe_dump(document, handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            temp = None
        except (OSError, yaml.YAMLError) as exc:
            raise UserFileError('users unavailable') from exc
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    logger.warning('用户清单临时文件清理失败')
