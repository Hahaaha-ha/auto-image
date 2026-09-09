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

import os
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
    """按 scope 路径缓存的 ObsClient（endpoint 随缓存固化，改 scope 需重启）。"""
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
