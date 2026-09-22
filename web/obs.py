"""华为云 OBS 对象浏览（Web 后端）。

复用仓库根 scope.yaml 的 obs 段与顶层 ak/sk/region（与 obs-skill 同一
配置源，实现独立——服务不跨包 import skill 脚本）。esdk-obs-python 延迟
导入：SDK 未装或 scope 无 obs 段时抛 ObsNotConfigured（端点 503），不阻断
Web 启动与其余功能。

SDK 调用是阻塞 IO——app.py 侧端点一律同步 def（FastAPI 自动派线程池，
不占事件循环）。客户端按 scope 路径进程内缓存。输出不含 AK/SK；签名
URL 按 OBS 约定携带 AccessKeyId（带签名下载机制本身如此，与 SK 无关）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

import yaml

DEFAULT_SCOPE_PATH = Path(__file__).resolve().parent.parent / "scope.yaml"

# 文本预览上限：内容端点把整个对象读进内存再解码，超限按不可预览处理
# （下载走签名 URL 端点，不占服务内存）
MAX_PREVIEW_BYTES = 2 * 1024 * 1024

# SDK 原生环境变量名（env 优先，与 ecs/ims/obs skill 对齐）——通用华为云
# 通道（ECS 同款），OBS 无独立凭据时回落到这里
ENV_AK = "HUAWEICLOUD_SDK_AK"
ENV_SK = "HUAWEICLOUD_SDK_SK"
ENV_REGION = "HUAWEICLOUD_SDK_REGION"

# OBS 专用通道（独立账号时用，优先于通用通道）：env 明文 / scope obs 段
# 明文 ak、sk / 加密 ak_enc、sk_enc（AES-256-GCM，密钥 env 或密钥文件）
ENV_OBS_AK = "OBS_AK"
ENV_OBS_SK = "OBS_SK"
ENV_ENC_KEY = "OBS_ENC_KEY"
ENC_KEY_FILE = Path(__file__).resolve().parent.parent / ".obs-secret.key"
ENC_PREFIX = "enc:v1"

# 密钥文件首读缓存（hex）。读到后驻内存——运行期密钥文件被删/被换不
# 影响已启动进程的解密；新进程仍需文件或 OBS_ENC_KEY env（否则密钥无
# 从获取，如实报未配置）。env 不走缓存（进程内 env 一般不变，直读零成本）。
_enc_key_cached: str | None = None


def _load_enc_key() -> str:
    global _enc_key_cached
    env_key = os.environ.get(ENV_ENC_KEY, "").strip()
    if env_key:
        return env_key
    if _enc_key_cached is not None:
        return _enc_key_cached
    if ENC_KEY_FILE.is_file():
        _enc_key_cached = ENC_KEY_FILE.read_text(encoding="utf-8").strip()
        return _enc_key_cached
    return ""

MAX_KEY_LEN = 1024


def normalize_zip_name(name):
    """zip 归档包名入口校验：非空、无路径分隔/遍历段、主体 ≤100 字符；
    .zip 后缀缺省自动补。返回完整文件名（含 .zip）。"""
    n = str(name or "").strip()
    if not n:
        raise ValueError("zip name required")
    if not n.lower().endswith(".zip"):
        n += ".zip"
    stem = n[:-4]
    if (
        not stem
        or len(stem) > 100
        or "/" in n
        or "\\" in n
        or ".." in stem
        or stem.startswith(".")
    ):
        raise ValueError("invalid zip name (1-100 chars, no path separators)")
    return n


class ObsNotConfigured(RuntimeError):
    """SDK 缺失或 scope 未配 obs 段（端点 503）。"""


class ObsApiError(RuntimeError):
    """OBS 服务端/网络失败（端点 502）；error 属性为可 JSON 错误面。"""

    def __init__(self, resp):
        self.error = {
            "status": getattr(resp, "status", None),
            "reason": getattr(resp, "reason", None),
            "error_code": getattr(resp, "errorCode", None),
            "error_message": getattr(resp, "errorMessage", None),
            "request_id": getattr(resp, "requestId", None),
        }
        super().__init__(str(self.error))


def normalize_key(key):
    """对象名入口校验：非空、≤1024、去首部 '/'。"""
    k = str(key or "").strip().lstrip("/")
    if not k:
        raise ValueError("key required")
    if len(k) > MAX_KEY_LEN:
        raise ValueError(f"key too long (> {MAX_KEY_LEN})")
    return k


def _decrypt_token(token):
    """enc:v1:<b64url nonce>:<b64url ct> → 明文（AES-256-GCM）。

    密钥按序：OBS_ENC_KEY env（64 hex）> 仓库根 .obs-secret.key（首读
    驻内存，运行期文件被删不影响本进程）。密钥缺失/非法/不匹配统一报
    ObsNotConfigured（端点 503，配置问题而非云故障）；格式约定与
    tools/obs_secret.py、obs-skill 的 obs_client.py 一致（三处实现互不
    import，格式由单测互验钉死）。
    """
    parts = str(token).strip().split(":")
    if len(parts) != 4 or parts[0] != "enc" or parts[1] != "v1":
        raise ObsNotConfigured("obs 凭据密文格式非法（应为 enc:v1:<nonce>:<ct>）")
    hexkey = _load_enc_key()
    if not hexkey:
        raise ObsNotConfigured(
            "加密的 obs 凭据需要密钥：OBS_ENC_KEY 环境变量或 .obs-secret.key"
        )
    try:
        key = bytes.fromhex(hexkey)
        if len(key) != 32:
            raise ValueError("not 32 bytes")
    except ValueError:
        raise ObsNotConfigured("OBS 加密密钥非法（需 64 hex 字符）") from None
    try:
        import base64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        un = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        return AESGCM(key).decrypt(un(parts[2]), un(parts[3]), None).decode("utf-8")
    except ImportError as exc:
        raise ObsNotConfigured(f"cryptography not installed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 —— 密钥不匹配/密文损坏统一面
        raise ObsNotConfigured(
            f"obs 凭据解密失败（密钥不匹配或密文损坏）：{type(exc).__name__}"
        ) from exc


def _obs_ak_sk(obs_cfg):
    """OBS 独立凭据：OBS_AK/OBS_SK env > obs.ak/sk 明文 > obs.ak_enc/sk_enc
    解密。ak 与 sk 各自独立解析（可混搭）。全缺返回 (None, None)——回落
    通用华为云通道（HUAWEICLOUD_SDK_* env > 顶层 ak/sk，与 ECS 同一套）。"""
    ak = (os.getenv(ENV_OBS_AK, "") or str(obs_cfg.get("ak", ""))).strip()
    sk = (os.getenv(ENV_OBS_SK, "") or str(obs_cfg.get("sk", ""))).strip()
    if not ak and str(obs_cfg.get("ak_enc", "")).strip():
        ak = _decrypt_token(obs_cfg["ak_enc"])
    if not sk and str(obs_cfg.get("sk_enc", "")).strip():
        sk = _decrypt_token(obs_cfg["sk_enc"])
    return ak or None, sk or None


def _load_conf(scope_path):
    """scope.yaml + env → (ak, sk, region, bucket, endpoint, domain)。

    读不到 scope / 缺 ak/sk/region/obs.bucket 任一项 → ObsNotConfigured
    （端点 503）；endpoint/domain 缺省按 region 推导。
    """
    path = Path(scope_path)
    data = {}
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    obs = data.get("obs") if isinstance(data.get("obs"), dict) else {}
    # 凭据：OBS 专用通道优先（独立账号），缺省回落通用通道（ECS 同套）
    ak, sk = _obs_ak_sk(obs)
    if not ak:
        ak = (os.getenv(ENV_AK) or str(data.get("ak", ""))).strip()
    if not sk:
        sk = (os.getenv(ENV_SK) or str(data.get("sk", ""))).strip()
    region = (str(obs.get("region", "")).strip()
              or (os.getenv(ENV_REGION) or str(data.get("region", ""))).strip())
    bucket = str(obs.get("bucket", "")).strip()
    if not (ak and sk and region and bucket):
        raise ObsNotConfigured(
            "OBS not configured: scope.yaml needs ak/sk/region + obs.bucket"
            " (obs 可独立配 ak/sk 或 ak_enc/sk_enc；或环境变量"
            " OBS_AK/_SK、HUAWEICLOUD_SDK_AK/_SK/_REGION)"
        )
    endpoint = str(obs.get("endpoint", "")).strip() or f"https://obs.{region}.myhuaweicloud.com"
    domain = str(obs.get("domain", "")).strip() or f"https://{bucket}.obs.{region}.myhuaweicloud.com"
    return ak, sk, region, bucket, endpoint.rstrip("/"), domain.rstrip("/")


_clients: dict[str, object] = {}


def _get_client(scope_path):
    """按 scope 路径缓存的 ObsClient（endpoint 随缓存固化；手工改 scope 需
    重启，save_config 保存路径已自动失效缓存即时生效）。"""
    cache_key = str(Path(scope_path).resolve())
    if cache_key not in _clients:
        ak, sk, _region, _bucket, endpoint, _domain = _load_conf(scope_path)
        try:
            from obs import ObsClient  # 延迟导入：未装 SDK 报未配置而非崩
        except ImportError as exc:
            raise ObsNotConfigured(f"esdk-obs-python not installed: {exc}") from exc
        _clients[cache_key] = ObsClient(access_key_id=ak, secret_access_key=sk, server=endpoint)
    return _clients[cache_key]


def health_check(scope_path):
    """OBS 健康检查：headBucket 单请求覆盖全链路——scope 解析、凭据解密
    （独立/加密通道）、SDK 可用、网络可达、凭据有效、桶存在且可访问。

    返回 ok 载荷（含时延）；配置/解密问题抛 ObsNotConfigured（503），云侧
    失败（凭据错误/网络不通/桶不存在）抛 ObsApiError（502）——端点把两类
    映射为 HTTP 状态码，监控可直接 curl -w %{http_code} 判定。
    """
    ak, sk, region, bucket, endpoint, domain = _load_conf(scope_path)
    client = _get_client(scope_path)
    started = time.monotonic()
    resp = client.headBucket(bucketName=bucket)
    latency_ms = round((time.monotonic() - started) * 1000)
    if resp.status < 300:
        return {
            "ok": True, "bucket": bucket, "region": region,
            "endpoint": endpoint, "domain": domain, "latency_ms": latency_ms,
        }
    raise ObsApiError(resp)


def list_objects(scope_path, limit=1000):
    """桶内全量列举（marker 分页取到 limit）→ 浏览清单。"""
    _ak, _sk, region, bucket, _endpoint, domain = _load_conf(scope_path)
    client = _get_client(scope_path)
    objects = []
    marker = None
    truncated = False
    while len(objects) < limit:
        resp = client.listObjects(
            bucketName=bucket, prefix=None,
            max_keys=min(limit - len(objects), 1000), marker=marker,
        )
        if resp.status >= 300:
            raise ObsApiError(resp)
        body = resp.body
        for c in (getattr(body, "contents", None) or []):
            objects.append({
                "key": getattr(c, "key", None),
                "size": getattr(c, "size", None),
                "etag": getattr(c, "etag", None),
                "last_modified": str(getattr(c, "lastModified", "") or ""),
                "storage_class": getattr(c, "storageClass", None),
            })
        marker = (getattr(body, "next_marker", None)
                  or getattr(body, "nextMarker", None))
        truncated = bool(getattr(body, "is_truncated", False)
                         or getattr(body, "isTruncated", False))
        if not truncated or not marker:
            truncated = False
            break
    return {
        "bucket": bucket, "region": region, "domain": domain,
        "count": len(objects), "truncated": truncated, "objects": objects,
    }


def signed_url(scope_path, key, expires=3600):
    """对象下载链接（本地签名不触网）：公开 URL + 带签名 URL。"""
    k = normalize_key(key)
    _ak, _sk, region, bucket, _endpoint, domain = _load_conf(scope_path)
    client = _get_client(scope_path)
    resp = client.createSignedUrl("GET", bucketName=bucket, objectKey=k, expires=int(expires))
    signed = getattr(resp, "signedUrl", None)
    if not signed:
        raise ObsApiError(resp)
    return {
        "key": k, "bucket": bucket, "region": region,
        "public_url": f"{domain}/{quote(k, safe='/')}",
        "signed_url": signed, "expires_in": int(expires),
    }


def read_text(scope_path, key, max_bytes=MAX_PREVIEW_BYTES):
    """对象文本预览。

    返回 None = 对象不存在；{"binary": True, ...} = 不可预览（二进制后缀
    /超限/非 UTF-8），调用方 422 指引签名 URL 下载；否则内容条目
    （dir/name/size/content_type/content，dir+name 拼回对象 key）。
    """
    from .artifacts import is_binary_file  # 二进制后缀约定与本地产物浏览同源

    k = normalize_key(key)
    _ak, _sk, region, bucket, _endpoint, domain = _load_conf(scope_path)
    client = _get_client(scope_path)
    head = client.headObject(bucketName=bucket, objectKey=k)
    if head.status == 404:
        return None
    if head.status >= 300:
        raise ObsApiError(head)
    hdrs = {str(n).lower(): v for n, v in (head.header or [])}
    size = int(hdrs.get("content-length", 0) or 0)
    cut = k.rfind("/")
    entry_dir, name = (k[:cut], k[cut + 1:]) if cut > 0 else ("", k)
    if is_binary_file(name) or size > max_bytes:
        return {"binary": True, "dir": entry_dir, "name": name, "size": size}
    resp = client.getObject(bucketName=bucket, objectKey=k, loadStreamInMemory=True)
    if resp.status >= 300:
        raise ObsApiError(resp)
    data = getattr(resp.body, "buffer", None) or b""
    try:
        content = bytes(data).decode("utf-8")
    except (UnicodeDecodeError, TypeError, ValueError):
        return {"binary": True, "dir": entry_dir, "name": name, "size": size}
    return {
        "dir": entry_dir, "name": name, "size": size,
        "content_type": hdrs.get("content-type"), "content": content,
    }


def upload_paths(scope_path, items):
    """本地产物批量归档到 OBS：items = [(key, 本地路径)]，逐个 putFile
    （对象名 = 本地产物相对路径，目录结构原样保留，与本地面板/桶内既有
    key 同构）。单个失败不阻断其余；返回逐项结果（端点再汇总 ok 标志）。
    """
    _ak, _sk, region, bucket, _endpoint, domain = _load_conf(scope_path)
    client = _get_client(scope_path)
    archived, failed = [], []
    for key, local in items:
        k = normalize_key(key)
        size = None
        try:
            size = Path(local).stat().st_size
        except OSError:
            pass  # stat 失败不阻断——上传由 SDK 如实报错
        try:
            resp = client.putFile(bucketName=bucket, objectKey=k, file_path=str(local))
        except (OSError, ValueError) as exc:  # 本地文件消失/读不了
            failed.append({"key": k, "local": str(local), "error": str(exc)})
            continue
        if resp.status < 300:
            archived.append({
                "key": k, "size": size if size is not None else 0,
                "url": f"{domain}/{quote(k, safe='/')}",
                "etag": (getattr(resp.body, "etag", None) if resp.body is not None else None),
            })
        else:
            failed.append({"key": k, "local": str(local), "error": ObsApiError(resp).error})
    return {
        "bucket": bucket, "region": region, "domain": domain,
        "archived": archived, "failed": failed,
        "ok": not failed, "count": len(archived),
    }


def upload_bytes(scope_path, key, data):
    """内存字节直传（zip 等打包产物不落盘）：putContent，contentType 由
    SDK 按扩展名填充（.zip → application/zip）。返回单对象成功项。"""
    _ak, _sk, region, bucket, _endpoint, domain = _load_conf(scope_path)
    client = _get_client(scope_path)
    k = normalize_key(key)
    resp = client.putContent(bucketName=bucket, objectKey=k, content=data)
    if resp.status >= 300:
        raise ObsApiError(resp)
    return {
        "bucket": bucket, "key": k, "size": len(data),
        "url": f"{domain}/{quote(k, safe='/')}",
        "etag": (getattr(resp.body, "etag", None) if resp.body is not None else None),
    }


# ---------------------------------------------------------------------------
# OBS 配置查看 / 保存 —— 面板「配置」按钮的后端
# ---------------------------------------------------------------------------
# 原则：明文 AK/SK 只进内存不进任何返回面（视图一律脱敏 + 来源标注，
# 长度单列）；保存路径只落密文（enc:v1，与 tools/obs_secret.py、obs-skill
# 同格式，三处互验钉死），obs 段明文 ak/sk 键自动删除；scope.yaml 其余
# 内容（注释、ECS 顶层凭据等）逐行保留——整文件 yaml 重写会抹掉注释，
# 故对 obs 块做行级编辑。


def _masked(value, head=4, tail=4):
    """凭据明文 → 头尾各露 head/tail 字符的脱敏形；短值全遮（防拼出大半）。"""
    v = str(value)
    if len(v) <= head + tail + 2:
        return "***"
    return f"{v[:head]}***{v[-tail:]}"


def _read_scope_dict(path):
    """scope.yaml → dict（缺失/坏 YAML/非 dict 一律 {}，不抛）。"""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _cred_view(env_val, plain, enc, sdk_env, top):
    """单条凭据（ak 或 sk）的视图 (脱敏值, 长度, 来源)，永不抛。

    优先级与 _obs_ak_sk/_load_conf 一致：OBS env > obs 明文 > obs 密文 >
    通用 env > 顶层明文 > 无。密文尽力解密只为取脱敏形——解密失败不抛
    （配置面板正要在密钥缺失/不匹配的坏状态下打开修配置），来源串里
    如实带原因。"""
    if env_val:
        return _masked(env_val), len(env_val), "env"
    if plain:
        return _masked(plain), len(plain), "scope-plain"
    if enc:
        try:
            v = _decrypt_token(enc)
            return _masked(v), len(v), "scope-enc"
        except ObsNotConfigured as exc:
            return None, None, f"scope-enc（解密失败：{exc}）"
    if sdk_env:
        return _masked(sdk_env), len(sdk_env), "env-sdk"
    if top:
        return _masked(top), len(top), "scope-top"
    return None, None, "none"


def get_config(scope_path):
    """scope.yaml + env → OBS 配置脱敏视图（面板「配置」打开即拉）。

    未配置/半配置/密文解密失败都正常返回（这些正是要打开面板修的状态）；
    configured 汇总凭据+region+bucket 四要素齐备与否。endpoint/domain 缺省
    按 region（+bucket）推导展示，与 _load_conf 的运行时推导同规则。"""
    path = Path(scope_path)
    data = _read_scope_dict(path)
    obs = data.get("obs") if isinstance(data.get("obs"), dict) else {}
    ak_v = _cred_view(
        (os.getenv(ENV_OBS_AK) or "").strip(), str(obs.get("ak", "") or "").strip(),
        str(obs.get("ak_enc", "") or "").strip(), (os.getenv(ENV_AK) or "").strip(),
        str(data.get("ak", "") or "").strip(),
    )
    sk_v = _cred_view(
        (os.getenv(ENV_OBS_SK) or "").strip(), str(obs.get("sk", "") or "").strip(),
        str(obs.get("sk_enc", "") or "").strip(), (os.getenv(ENV_SK) or "").strip(),
        str(data.get("sk", "") or "").strip(),
    )
    region = (str(obs.get("region", "")).strip()
              or (os.getenv(ENV_REGION) or str(data.get("region", ""))).strip())
    bucket = str(obs.get("bucket", "")).strip()
    endpoint = (str(obs.get("endpoint", "")).strip()
                or (f"https://obs.{region}.myhuaweicloud.com" if region else ""))
    domain = (str(obs.get("domain", "")).strip()
              or (f"https://{bucket}.obs.{region}.myhuaweicloud.com"
                  if bucket and region else ""))
    return {
        "scope_path": str(path),
        "bucket": bucket or None,
        "region": region or None,
        "endpoint": endpoint.rstrip("/") or None,
        "domain": domain.rstrip("/") or None,
        "configured": bool(region and bucket and ak_v[0] and sk_v[0]),
        "ak": {"masked": ak_v[0], "length": ak_v[1], "source": ak_v[2]},
        "sk": {"masked": sk_v[0], "length": sk_v[1], "source": sk_v[2]},
        "enc_key": {
            "source": ("env" if os.environ.get(ENV_ENC_KEY, "").strip()
                       else ("file" if ENC_KEY_FILE.is_file() else "missing")),
            "file": ENC_KEY_FILE.name,
        },
    }


def _encrypt_token(plaintext, hexkey):
    """明文 → enc:v1:<b64url nonce>:<b64url ct>（AES-256-GCM）。

    与 tools/obs_secret.py encrypt 同格式（第四处实现，格式由单测互验
    钉死：本处产的密文 tools/skill 要能解）。hexkey 由 _ensure_enc_key
    保证 64 hex。cryptography 缺失抛 ObsNotConfigured（503 语义）。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ObsNotConfigured(f"cryptography not installed: {exc}") from exc
    key = bytes.fromhex(hexkey)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    b64u = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")  # noqa: E731
    return f"{ENC_PREFIX}:{b64u(nonce)}:{b64u(ct)}"


def _ensure_enc_key_hex():
    """加密密钥就绪：env > 密钥文件 > 现场生成（600，同 tools keygen，
    已存在不覆盖）。返回 64 hex；生成后写进程内缓存（本进程立即可解密）。"""
    global _enc_key_cached
    hexkey = _load_enc_key()
    if hexkey:
        return hexkey
    hexkey = secrets.token_bytes(32).hex()
    ENC_KEY_FILE.write_text(hexkey + "\n", encoding="utf-8")
    os.chmod(ENC_KEY_FILE, 0o600)
    _enc_key_cached = hexkey
    return hexkey


# obs 段受管键（行级编辑只碰这些；其余键行/注释原样保留）
_OBS_MANAGED_KEYS = re.compile(r"^(ak|sk|ak_enc|sk_enc|bucket|region|endpoint|domain)$")


def _update_obs_block(text, updates):
    """scope.yaml 文本级 obs 段编辑：updates {键: 新值 | None(删除)}。

    只重写受管键的行、其余行（含注释、未知键、顶层其它段）逐字保留；
    段内没有的键补在段尾；顶层无 obs 段则文末追加。受管键行上的行尾
    注释会被丢弃（仅该行，独立注释行不动）。值经 json.dumps 双引号转义
    （YAML 兼容 JSON 字符串）。返回新文本（结尾保证一个换行）。"""
    lines = text.splitlines()
    obs_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "obs:" and not line[:1].isspace():
            obs_idx = i
            break
    pending = dict(updates)
    if obs_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("obs:")
        obs_idx = len(lines) - 1
        seg, tail = [], []
    else:
        end = obs_idx + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() and not line[:1].isspace():
                break
            end += 1
        seg, tail = lines[obs_idx + 1:end], lines[end:]
    new_seg = []
    for line in seg:
        m = re.match(r"^\s+([A-Za-z0-9_-]+)\s*:", line)
        if m and _OBS_MANAGED_KEYS.match(m.group(1)) and m.group(1) in pending:
            key = m.group(1)
            val = pending.pop(key)
            if val is not None:
                new_seg.append(f"  {key}: {json.dumps(val, ensure_ascii=False)}")
            # None → 该行整体删除
        else:
            new_seg.append(line)
    for key, val in pending.items():
        if val is not None:
            new_seg.append(f"  {key}: {json.dumps(val, ensure_ascii=False)}")
    return "\n".join(lines[:obs_idx + 1] + new_seg + tail) + "\n"


def clean_config_input(ak=None, sk=None, region=None, bucket=None, endpoint=None):
    """配置保存的入口清洗（端点预检与 save_config 共用同一份校验）：
    None 跳过、字符串去空白、空值剔除；非字符串或全空抛 ValueError
    （端点 422 语义）。返回 {字段: 清洗后值}。"""
    clean = {}
    for name, val in (("ak", ak), ("sk", sk), ("region", region),
                      ("bucket", bucket), ("endpoint", endpoint)):
        if val is None:
            continue
        if not isinstance(val, str):
            raise ValueError(f"{name} must be a string")
        val = val.strip()
        if val:
            clean[name] = val
    if not clean:
        raise ValueError("nothing to save (at least one of ak/sk/region/bucket/endpoint)")
    return clean


def save_config(scope_path, ak=None, sk=None, region=None, bucket=None, endpoint=None):
    """配置保存（面板「配置」按钮的写路径）：ak/sk 一律密文落盘
    （ak_enc/sk_enc），obs 段明文 ak/sk 键自动删除（无论本次是否提交新
    凭据——历史明文一并清掉）；bucket/region/endpoint 明文，endpoint
    未提交时按（新）region 推导重写，domain 按 桶+region 重写，避免
    改 region 后旧值残留生效。

    保存后：该 scope 的客户端缓存失效（新配置即时生效无需重启）；新
    凭据明文登记进脱敏已知清单（此后任何事件流文本出现即被遮蔽——
    明文已不落盘，这是内存里的最后一道防线）。返回脱敏配置视图（同
    get_config，明文永不回传）。输入全空/非字符串 → ValueError（422）。"""
    clean = clean_config_input(ak=ak, sk=sk, region=region, bucket=bucket, endpoint=endpoint)
    path = Path(scope_path)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""

    data = _read_scope_dict(path)
    obs = data.get("obs") if isinstance(data.get("obs"), dict) else {}
    updates = {"ak": None, "sk": None}  # 明文键一律删除
    if "ak" in clean or "sk" in clean:
        hexkey = _ensure_enc_key_hex()
        if "ak" in clean:
            updates["ak_enc"] = _encrypt_token(clean["ak"], hexkey)
        if "sk" in clean:
            updates["sk_enc"] = _encrypt_token(clean["sk"], hexkey)
    if "bucket" in clean:
        updates["bucket"] = clean["bucket"]
    if "region" in clean:
        updates["region"] = clean["region"]

    final_bucket = clean.get("bucket", str(obs.get("bucket", "")).strip())
    final_region = clean.get("region", str(obs.get("region", "")).strip()
                             or str(data.get("region", "")).strip())
    if "endpoint" in clean:
        final_endpoint = clean["endpoint"]
    elif "region" in clean:
        # region 刚被改：旧 endpoint 属于别的 region，按新 region 重推
        final_endpoint = f"https://obs.{final_region}.myhuaweicloud.com" if final_region else ""
    else:
        final_endpoint = (str(obs.get("endpoint", "")).strip()
                          or (f"https://obs.{final_region}.myhuaweicloud.com" if final_region else ""))
    updates["endpoint"] = final_endpoint.rstrip("/") or None
    updates["domain"] = (f"https://{final_bucket}.obs.{final_region}.myhuaweicloud.com"
                         if final_bucket and final_region else None)

    new_text = _update_obs_block(text, updates)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
    os.replace(tmp, path)

    _clients.pop(str(path.resolve()), None)  # 客户端缓存失效：新配置即时生效
    if "ak" in clean or "sk" in clean:
        from .redact import register_secrets  # 惰性导入（redact 无反向依赖）
        register_secrets([v for k, v in clean.items() if k in ("ak", "sk")])
    return get_config(scope_path)
