"""文件用户清单与 Cookie 登录态（无 Bearer 双轨）。

用户清单是 YAML/JSON 文件：用户名 → {password_hash, enabled?}，另可带
全局 iterations。password_hash 形如 pbkdf2_sha256$<iters>$<salt>$<dk>，
工具 hash_password 生成；示例清单不含可登录密码。清单按 mtime 热载——
运维改文件（禁用/改密/增删用户）即时生效，无需重启。

Cookie 是自包含 HMAC 签名令牌 v1.<payload_b64url>.<sig_b64url>：payload
携带用户名、签发/过期时刻和密码版本指纹（该用户条目的稳定短哈希——改密
或禁用让指纹漂移，该用户旧 Cookie 即刻失效，其他用户不受牵连）。密钥经
WEB_AUTH_SECRET 显式配置；缺省按机器稳定属性派生（同机重启不掉登录；
换值即全体失效，等效轮换）。7 天绝对过期、HttpOnly、SameSite=Lax。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

import yaml

logger = logging.getLogger("web")

COOKIE_NAME = "va_session"
COOKIE_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_ITERATIONS = 100_000
DEFAULT_USERS_PATH = Path(__file__).resolve().parent.parent / "users.yaml"

# 校验失败的可识别原因（审计 reason 的判定值来源，与 app 的 401/422/503 语义单处对应）
REASON_BAD_COOKIE = "bad_cookie"
REASON_EXPIRED = "expired"
REASON_NO_USER = "no_such_user"
REASON_DISABLED = "disabled_user"
REASON_STALE_FINGERPRINT = "stale_fingerprint"
REASON_BAD_CREDENTIALS = "bad_credentials"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password, iterations=DEFAULT_ITERATIONS):
    """生成 pbkdf2_sha256$<iters>$<salt_b64>$<dk_b64> 存储形（示例清单与
    运维工具共用；盐每词随机）。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64url(salt)}${_b64url(dk)}"


def verify_password(password, encoded):
    """口令校验（常数时间比较）。哈希串形状不对按不匹配处理，不抛。"""
    try:
        _scheme, iters, salt_b64, dk_b64 = encoded.split("$")
        salt = _unb64url(salt_b64)
        want = _unb64url(dk_b64)
        if _scheme != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


class UserRoster:
    """用户清单读取器（mtime 热载）。清单缺失/损坏只告警并视为无用户——
    服务照常启动但任何人都登录不了（fail-closed，不静默放行）。"""

    def __init__(self, path):
        self.path = Path(path)
        self._mtime = None
        self._data = None
        self._fingerprints = {}
        self._reload()

    def _reload(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            if self._data is None:
                logger.warning("用户清单 %s 不可读，服务以无用户启动（登录被拒）", self.path)
            self._data, self._fingerprints = {}, {}
            self._mtime = None
            return
        users = {}
        if raw.strip():
            data = yaml.safe_load(raw) or {}
            entries = data.get("users") if isinstance(data, dict) else None
            if isinstance(entries, dict):
                for name, cfg in entries.items():
                    if isinstance(name, str) and isinstance(cfg, dict):
                        users[name] = cfg
            else:
                logger.warning("用户清单 %s 形状不对（缺 users 映射），按无用户处理", self.path)
        self._data = users
        # 密码版本指纹按用户条目独立：改密/禁用只撤销该用户的 Cookie，
        # 其他用户的登录态不受清单任何变动（如新增用户）牵连
        self._fingerprints = {
            name: hashlib.sha256(
                json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:16]
            for name, cfg in users.items()
        }
        self._mtime = mtime

    def _maybe_refresh(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._reload()

    def get(self, username):
        """读用户条目（触发热载检查）；不存在返回 None。"""
        self._maybe_refresh()
        return self._data.get(username)

    def check_password(self, username, password):
        """登录口令校验：用户存在、启用且口令匹配才通过。返回 (ok, reason)；
        reason 判定值见模块顶常量（审计用），通过时为 None。"""
        entry = self.get(username)
        if entry is None:
            return False, REASON_NO_USER
        if entry.get("enabled", True) is False:
            return False, REASON_DISABLED
        if not verify_password(password, entry.get("password_hash", "")):
            return False, REASON_BAD_CREDENTIALS
        return True, None

    def fingerprint(self, username):
        """密码版本指纹：该用户条目（password_hash + enabled）的稳定短哈希
        ——改密、禁用使指纹漂移，旧 Cookie 即刻失效；其他用户不受牵连。
        用户不存在返回 None。"""
        self._maybe_refresh()
        return self._fingerprints.get(username)


def _hmac_key(secret):
    """secret 归一为 HMAC 键 bytes（str/bytes 皆可）。"""
    return secret if isinstance(secret, bytes) else secret.encode("utf-8")


def issue_token(secret: str, username: str, fingerprint: str | None, ttl: float):
    """签发自包含令牌：v1.<payload>.<sig>。payload 为 JSON（用户名、签发、
    过期、指纹），HMAC-SHA256 签名覆盖整段前缀。"""
    key = _hmac_key(secret)
    now = time.time()
    payload = {
        "u": username,
        "iat": int(now),
        "exp": int(now + ttl),
        "fp": fingerprint,
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(key, f"v1.{body}".encode("ascii"), hashlib.sha256).hexdigest()
    return f"v1.{body}.{sig}"


def verify_token(secret: str, token: str, roster: "UserRoster"):
    """校验令牌并回查清单。返回 (username, None) 或 (None, reason)；reason
    判定值见模块顶常量。"""
    key = _hmac_key(secret)
    try:
        version, body, sig = token.split(".")
        if version != "v1":
            return None, REASON_BAD_COOKIE
        want = hmac.new(key, f"v1.{body}".encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None, REASON_BAD_COOKIE
        payload = json.loads(_unb64url(body))
        username = payload["u"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None, REASON_BAD_COOKIE
    if not isinstance(payload.get("exp"), int) or payload["exp"] < time.time():
        return None, REASON_EXPIRED
    entry = roster.get(username)
    if entry is None:
        return None, REASON_NO_USER
    if entry.get("enabled", True) is False:
        return None, REASON_DISABLED
    if payload.get("fp") is not None and payload["fp"] != roster.fingerprint(username):
        return None, REASON_STALE_FINGERPRINT
    return username, None


def derive_default_secret():
    """缺省认证密钥：机器稳定属性派生（machine-id 优先，退化到主机名 +
    用户 HOME）。同机重启稳定；显式配置 WEB_AUTH_SECRET 即可整体轮换。"""
    material = None
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            material = Path(candidate).read_text(encoding="utf-8").strip()
            if material:
                break
        except OSError:
            continue
    if not material:
        material = f"{os.uname().machine}:{os.uname().nodename}:{Path.home()}"
    return hashlib.sha256(f"auto-image-web-auth:{material}".encode("utf-8")).hexdigest()


def resolve_secret(explicit=None):
    """认证密钥：显式注入 > WEB_AUTH_SECRET > 机器派生缺省。"""
    if explicit:
        return explicit
    env = os.environ.get("WEB_AUTH_SECRET")
    if env:
        return env
    return derive_default_secret()
