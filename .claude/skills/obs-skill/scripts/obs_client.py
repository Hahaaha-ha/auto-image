#!/usr/bin/env python
"""obs_client —— 华为云 OBS 客户端构造 + scope/凭证解析。

OBS 凭据可与 ECS 顶层独立（OBS_AK/OBS_SK env > scope obs.ak/sk 明文 >
obs.ak_enc/sk_enc 加密 > HUAWEICLOUD_SDK_* env > 顶层 ak/sk 回落）；
加密形态 enc:v1:...（AES-256-GCM，密钥 OBS_ENC_KEY env 或仓库根
.obs-secret.key，配套工具 tools/obs_secret.py；格式与 web/obs.py 一致、
实现独立，单测互验）。桶/端点/域名：CLI --bucket > scope.obs.bucket；
endpoint/domain 可省略按 region 推导。不跨 skill import——独立维护。
"""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
# 仓库根 = .claude/skills/obs-skill/scripts/ 起 4 级上 —— 共享 scope.yaml 所在。
REPO_ROOT = SCRIPT_DIR.parents[3]
DEFAULT_SCOPE_PATH = REPO_ROOT / "scope.yaml"

# 通用华为云通道（ECS/IMS 同套）——OBS 无独立凭据时回落
ENV_AK = "HUAWEICLOUD_SDK_AK"
ENV_SK = "HUAWEICLOUD_SDK_SK"
ENV_REGION = "HUAWEICLOUD_SDK_REGION"

# OBS 专用通道
ENV_OBS_AK = "OBS_AK"
ENV_OBS_SK = "OBS_SK"
ENV_ENC_KEY = "OBS_ENC_KEY"
ENC_KEY_FILE = REPO_ROOT / ".obs-secret.key"

# 密钥文件首读缓存（hex）：读到后驻内存，运行期文件被删不影响本进程
# （CLI 单次运行内 ak/sk 解密共用）；新进程仍需文件或 env。
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

# 桶命名约束（官方：3~63 字符，小写字母/数字/中划线/点，字母或数字开头结尾，
# 禁 IP、禁 -/. 开头结尾、禁 .. / -. / .- 相邻——此处只做长度+字符集粗检）
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


@dataclass
class Credentials:
    """华为云凭证（OBS 只需 ak/sk/region，无 project_id）。"""

    ak: str
    sk: str
    region: str


@dataclass
class ObsTarget:
    """上传目标：桶 + 服务端点 + 公开访问域名。"""

    bucket: str
    endpoint: str  # SDK server 参数：https://obs.<region>.myhuaweicloud.com
    domain: str    # 公开 URL 前缀：https://<bucket>.obs.<region>.myhuaweicloud.com


def load_scope_config(scope_path: Path) -> dict[str, Any]:
    with open(scope_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        raise ValueError("scope 文件顶层必须是对象。")
    if isinstance(cfg.get("default"), dict):  # 兼容 {default: {...}} 包裹
        cfg = cfg["default"]
    return cfg


def _decrypt_token(token: Any) -> str:
    """enc:v1:<b64url nonce>:<b64url ct> → 明文（AES-256-GCM）。

    密钥按序：OBS_ENC_KEY env（64 hex）> 仓库根 .obs-secret.key（首读
    驻内存，运行期文件被删不影响本进程）。格式与 web/obs.py、
    tools/obs_secret.py 一致（三处实现互不 import，格式由单测互验钉死）；
    密钥缺失/不匹配/格式非法统一报 ValueError。
    """
    parts = str(token).strip().split(":")
    if len(parts) != 4 or parts[0] != "enc" or parts[1] != "v1":
        raise ValueError("obs 凭据密文格式非法（应为 enc:v1:<nonce>:<ct>）")
    hexkey = _load_enc_key()
    if not hexkey:
        raise ValueError("加密的 obs 凭据需要密钥：OBS_ENC_KEY 环境变量或 .obs-secret.key（tools/obs_secret.py keygen）")
    try:
        key = bytes.fromhex(hexkey)
        if len(key) != 32:
            raise ValueError("not 32 bytes")
    except ValueError:
        raise ValueError("OBS 加密密钥非法（需 64 hex 字符）") from None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        un = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        return AESGCM(key).decrypt(un(parts[2]), un(parts[3]), None).decode("utf-8")
    except ImportError as exc:
        raise ValueError(f"缺 cryptography 库：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 —— 密钥不匹配/密文损坏统一面
        raise ValueError(f"obs 凭据解密失败（密钥不匹配或密文损坏）：{type(exc).__name__}") from exc


def resolve_credentials(scope: dict[str, Any]) -> Credentials:
    """OBS 凭据优先级（OBS 可与 ECS 顶层独立）：

    OBS_AK/OBS_SK env > scope obs.ak/sk 明文 > obs.ak_enc/sk_enc 解密 >
    HUAWEICLOUD_SDK_AK/_SK env > scope 顶层 ak/sk。
    region：obs.region > HUAWEICLOUD_SDK_REGION env > 顶层 region。
    """
    obs_cfg = scope.get("obs") if isinstance(scope.get("obs"), dict) else {}
    ak = (os.getenv(ENV_OBS_AK) or str(obs_cfg.get("ak", ""))).strip()
    sk = (os.getenv(ENV_OBS_SK) or str(obs_cfg.get("sk", ""))).strip()
    if not ak and str(obs_cfg.get("ak_enc", "")).strip():
        ak = _decrypt_token(obs_cfg["ak_enc"])
    if not sk and str(obs_cfg.get("sk_enc", "")).strip():
        sk = _decrypt_token(obs_cfg["sk_enc"])
    if not ak:
        ak = (os.getenv(ENV_AK) or str(scope.get("ak", ""))).strip()
    if not sk:
        sk = (os.getenv(ENV_SK) or str(scope.get("sk", ""))).strip()
    region = (str(obs_cfg.get("region", "")).strip()
              or (os.getenv(ENV_REGION) or str(scope.get("region", ""))).strip())
    missing = [k for k, v in (("ak", ak), ("sk", sk), ("region", region)) if not v]
    if missing:
        raise ValueError(
            "缺少 OBS 凭证/区域：" + ", ".join(missing)
            + "。独立配置走 scope obs 段（ak/sk 明文或 ak_enc/sk_enc 加密）"
            "或 OBS_AK/_SK 环境变量；回落通道为 HUAWEICLOUD_SDK_AK/_SK/_REGION"
            " 或 scope 顶层 ak/sk。"
        )
    return Credentials(ak=ak, sk=sk, region=region)


def resolve_obs_target(scope: dict[str, Any], creds: Credentials,
                       bucket: str | None = None) -> ObsTarget:
    """桶：CLI --bucket > scope.obs.bucket；endpoint/domain 缺省按 region 推导。"""
    obs_cfg = scope.get("obs") if isinstance(scope.get("obs"), dict) else {}
    b = (bucket or str(obs_cfg.get("bucket", ""))).strip()
    if not b:
        raise ValueError(
            "缺少桶名：请在 scope.yaml 的 obs.bucket 提供，或用 CLI --bucket 指定。"
        )
    if not BUCKET_RE.match(b):
        raise ValueError(
            f"桶名不合法：{b!r}（3~63 字符，小写字母/数字/中划线/点，字母或数字开头结尾）"
        )
    region = creds.region
    endpoint = str(obs_cfg.get("endpoint", "")).strip() or f"https://obs.{region}.myhuaweicloud.com"
    domain = str(obs_cfg.get("domain", "")).strip() or f"https://{b}.obs.{region}.myhuaweicloud.com"
    return ObsTarget(bucket=b, endpoint=endpoint.rstrip("/"), domain=domain.rstrip("/"))


def build_client(creds: Credentials, target: ObsTarget):
    """官方 ObsClient（esdk-obs-python）。延迟导入——dry-run/纯单测无需装 SDK。"""
    from obs import ObsClient  # noqa: PLC0415

    return ObsClient(
        access_key_id=creds.ak,
        secret_access_key=creds.sk,
        server=target.endpoint,
    )
