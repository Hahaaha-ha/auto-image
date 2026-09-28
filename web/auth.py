"""文件用户清单与 Cookie 登录态（无 Bearer 双轨）。

用户清单由 users.UserRoster 串行维护；人工维护须停服备份。password_hash
形如 pbkdf2_sha256$<iters>$<salt>$<dk>，由 hash_password 生成。

Cookie 是自包含 HMAC 签名令牌 v1.<payload_b64url>.<sig_b64url>：payload
携带用户名、签发/过期时刻和用户版本指纹（每次 Web 变更持久化新的 revision，
使旧 Cookie 永久失效，其他用户不受牵连）。密钥经
WEB_AUTH_SECRET 显式配置；缺省按机器稳定属性派生（同机重启不掉登录；
换值即全体失效，等效轮换）。7 天绝对过期、HttpOnly、SameSite=Lax。
"""
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from .users import UserRoster

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


def check_password(roster, username, password):
    entry = roster.get(username)
    if entry is None:
        return False, REASON_NO_USER
    if entry.get("enabled", True) is False:
        return False, REASON_DISABLED
    if not verify_password(password, entry.get("password_hash", "")):
        return False, REASON_BAD_CREDENTIALS
    return True, None


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


def read_token(secret: str, token: str):
    """仅验证签名、形状和绝对过期；业务授权必须再回查当前用户清单。"""
    key = _hmac_key(secret)
    try:
        version, body, sig = token.split(".")
        if version != "v1":
            return None, REASON_BAD_COOKIE
        want = hmac.new(key, f"v1.{body}".encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None, REASON_BAD_COOKIE
        payload = json.loads(_unb64url(body))
        if (not isinstance(payload, dict) or not isinstance(payload.get("u"), str)
                or not isinstance(payload.get("fp"), str)):
            return None, REASON_BAD_COOKIE
    except (ValueError, KeyError, TypeError, UnicodeError):
        return None, REASON_BAD_COOKIE
    if not isinstance(payload.get("exp"), int) or payload["exp"] < time.time():
        return None, REASON_EXPIRED
    return payload, None


def verify_token(secret: str, token: str, roster: "UserRoster"):
    """校验令牌并回查清单，返回 (username, None) 或 (None, reason)。"""
    payload, reason = read_token(secret, token)
    if payload is None:
        return None, reason
    username = payload["u"]
    entry = roster.get(username)
    if entry is None:
        return None, REASON_NO_USER
    if entry.get("enabled", True) is False:
        return None, REASON_DISABLED
    if payload["fp"] != roster.fingerprint(username):
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
