#!/usr/bin/env python
"""obs_ops —— 上传计划构造 + 对象名/URL 工具（纯逻辑，不触网）。

对应官方文档「上传对象-文件上传(Python SDK)」obs_22_0903：
- putFile 单次上传对象大小范围 [0, 5GB]，超过需多段上传（本 skill 不做）。
- 目录上传官方建议逐文件并发（putFile 自身无并发）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

# putFile 单文件上限（官方接口约束）
MAX_PUTFILE_BYTES = 5 * 1024 ** 3

# HeadPermission.PUBLIC_READ 的原始值（esdk-obs-python 以字符串头域下发）
ACL_PUBLIC_READ = "public-read"
STORAGE_CLASSES = ("STANDARD", "WARM", "COLD")

MAX_KEY_LEN = 1024  # 官方约束：对象名 >0 且 ≤1024 字符


def normalize_key(key: str) -> str:
    """对象名规范：去首部 '/'，Windows 反斜杠转 '/'。"""
    k = (key or "").strip().replace("\\", "/")
    while k.startswith("/"):
        k = k[1:]
    if not k:
        raise ValueError("对象名不能为空。")
    if len(k) > MAX_KEY_LEN:
        raise ValueError(f"对象名长度 {len(k)} 超过上限 {MAX_KEY_LEN}。")
    return k


def normalize_prefix(prefix: str | None) -> str:
    """前缀规范：允许空串（桶根）；输出不以 '/' 结尾也不以 '/' 开头。"""
    p = (prefix or "").strip().replace("\\", "/").strip("/")
    return p


def join_prefix(prefix: str | None, name: str) -> str:
    """前缀 + 文件名 → 对象名。prefix 可为空串（落桶根）。"""
    p = normalize_prefix(prefix)
    return normalize_key(f"{p}/{name}") if p else normalize_key(name)


def public_url(domain: str, key: str) -> str:
    """公开访问 URL：域名 + URL 编码后的对象路径（/ 保留为分隔符）。"""
    return f"{domain.rstrip('/')}/{quote(key, safe='/')}"


def build_upload_plan(path: Path, *, key: str | None, prefix: str | None) -> list[dict[str, Any]]:
    """本地路径 → [{local, key, size}]（上传前就确定全部映射，可 dry-run 预览）。

    - 文件：--key 指定对象名；否则 <prefix>/<basename>（prefix 空则 basename）。
    - 目录：递归收集文件；--key 不适用（对象名逐文件生成，给 --key 报错）；
      默认落到 <目录名>/ 前缀下，--prefix 可覆盖（--prefix "" 落桶根平铺）。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"本地路径不存在：{path}")
    if path.is_file():
        dest = normalize_key(key) if key else join_prefix(prefix, path.name)
        return [{"local": str(path), "key": dest, "size": path.stat().st_size}]

    if key:
        raise ValueError("目录上传不支持 --key（对象名逐文件生成）；请用 --prefix 指定目标前缀。")
    p = path.name if prefix is None else prefix  # 默认 <目录名>/；显式 --prefix（含空串）生效
    plan: list[dict[str, Any]] = []
    for f in sorted(path.rglob("*")):
        if f.is_file():
            rel = f.relative_to(path).as_posix()
            plan.append({"local": str(f), "key": join_prefix(p, rel), "size": f.stat().st_size})
    return plan


def err_from_resp(resp: Any) -> dict[str, Any]:
    """SDK GetResult 失败面 → 可 JSON dict（不含任何凭证）。"""
    return {
        "status": getattr(resp, "status", None),
        "reason": getattr(resp, "reason", None),
        "error_code": getattr(resp, "errorCode", None),
        "error_message": getattr(resp, "errorMessage", None),
        "request_id": getattr(resp, "requestId", None),
    }
