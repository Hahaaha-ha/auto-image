#!/usr/bin/env python
"""OBS 凭据加密工具 —— scope.yaml obs 段 ak_enc/sk_enc 的配套。

OBS 的 AK/SK 可与 ECS 顶层独立配置，且要求不以明文落盘：明文经
AES-256-GCM 加密成 `enc:v1:<b64url nonce>:<b64url ciphertext+tag>` 写入
scope.yaml 的 obs 段（ak_enc / sk_enc），密钥不进 scope。

密钥（按序）：OBS_ENC_KEY 环境变量（64 hex）> 仓库根 .obs-secret.key
（hex 文本一行，600 权限，已 gitignore）。web/obs.py 与
.claude/skills/obs-skill/scripts/obs_client.py 按同一格式解密（实现各自
独立、格式由两侧单测互验钉死）。

用法：
  python tools/obs_secret.py keygen            # 生成 .obs-secret.key（已存在不覆盖）
  python tools/obs_secret.py encrypt <明文>    # 输出 enc:v1:... （写入 obs.ak_enc/sk_enc）
  python tools/obs_secret.py decrypt <enc:v1:...>  # 解密（调试用；密文从 stdin 读可避 shell 历史）
  python tools/obs_secret.py selftest          # 加解密回环 + 错误面自测
"""
from __future__ import annotations

import base64
import os
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REPO_ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = REPO_ROOT / ".obs-secret.key"
ENV_KEY = "OBS_ENC_KEY"
PREFIX = "enc:v1"
NONCE_LEN = 12


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_key() -> bytes:
    hexkey = os.environ.get(ENV_KEY, "").strip()
    if not hexkey and KEY_FILE.is_file():
        hexkey = KEY_FILE.read_text(encoding="utf-8").strip()
    if not hexkey:
        raise SystemExit(f"缺加密密钥：设置 {ENV_KEY} 环境变量，或先 keygen 生成 {KEY_FILE}")
    try:
        key = bytes.fromhex(hexkey)
    except ValueError:
        raise SystemExit("密钥内容非法：必须是 hex 文本") from None
    if len(key) != 32:
        raise SystemExit("密钥必须 32 字节（64 hex 字符）")
    return key


def keygen(force: bool = False) -> None:
    if KEY_FILE.exists() and not force:
        print(f"密钥已存在 {KEY_FILE}（不覆盖；换钥需 force 并重新加密全部密文）")
        return
    KEY_FILE.write_text(secrets.token_bytes(32).hex() + "\n", encoding="utf-8")
    os.chmod(KEY_FILE, 0o600)
    print(f"已生成 {KEY_FILE}（权限 600，gitignore；换机器迁移时复制该文件）")


def encrypt(plaintext: str) -> str:
    key = load_key()
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return f"{PREFIX}:{_b64u(nonce)}:{_b64u(ct)}"


def decrypt(token: str) -> str:
    parts = str(token).strip().split(":")
    if len(parts) != 4 or parts[0] != "enc" or parts[1] != "v1":
        raise ValueError("密文格式非法（应为 enc:v1:<nonce>:<ct>）")
    key = load_key()
    try:
        pt = AESGCM(key).decrypt(_unb64u(parts[2]), _unb64u(parts[3]), None)
    except Exception as exc:  # noqa: BLE001 —— 密钥不匹配/损坏统一报这一面
        raise ValueError(f"解密失败（密钥不匹配或密文损坏）：{type(exc).__name__}") from exc
    return pt.decode("utf-8")


def selftest() -> None:
    load_key()  # 先确保密钥可用
    token = encrypt("回环明文-√-123")
    assert token.startswith("enc:v1:"), token
    assert decrypt(token) == "回环明文-√-123"
    for bad in ("", "plain", "enc:v1:only", "enc:v2:YQ:YQ", "enc:v1:!!:??"):
        try:
            decrypt(bad)
            raise AssertionError(f"{bad!r} 应报格式/解密错误")
        except ValueError:
            pass
    print(f"selftest OK（格式 {PREFIX}:<b64url nonce>:<b64url ct>，AES-256-GCM）")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd == "keygen":
        keygen(force="--force" in args)
        return 0
    if cmd == "encrypt":
        if not args:
            print("用法：encrypt <明文>（或 echo 明文 | encrypt -）", file=sys.stderr)
            return 2
        plain = sys.stdin.read().strip() if args[0] == "-" else args[0]
        print(encrypt(plain))
        return 0
    if cmd == "decrypt":
        token = sys.stdin.read().strip() if not args or args[0] == "-" else args[0]
        try:
            print(decrypt(token))
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        return 0
    if cmd == "selftest":
        selftest()
        return 0
    print(f"未知命令：{cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
