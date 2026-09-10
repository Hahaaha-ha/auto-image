#!/usr/bin/env python3
"""OBS 配置端点主缝测试 —— 查看脱敏视图 / 保存密文化（明文自动删除）。

缝：同 test_obs_api 的 ASGI 测试客户端；scope 指向临时文件（make_test_app
注入后重写内容），加密密钥走 OBS_ENC_KEY env（不触仓库根密钥文件；keygen
路径单测把 ENC_KEY_FILE 常量指到临时路径）。响应面反复断言「明文不出
服务」。纯 assert，无 pytest。

运行：python web/tests/test_obs_config.py
"""
import asyncio
import os
import secrets as pysecrets
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web import obs as web_obs  # noqa: E402
from web import redact  # noqa: E402
from web.obs import ObsApiError  # noqa: E402
from web.tests.support import async_client, make_test_app  # noqa: E402

ENV_KEYS = ("OBS_AK", "OBS_SK", "OBS_ENC_KEY",
            "HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK", "HUAWEICLOUD_SDK_REGION")

TEST_KEY = pysecrets.token_bytes(32).hex()
PLAIN_AK = "AKTEST" + "A" * 14   # 20 位，形状同真 AK
PLAIN_SK = "SKTEST" + "b" * 32   # 38 位，形状同真 SK


class _Env:
    """env 存取上下文：进保存快照、出恢复；用例内改 env 的统一缝。"""

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        return self

    def __exit__(self, *exc_info):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _make_scope_app(obs=None, top=None, **overrides):
    """临时 scope（含顶层 ECS 凭据与可选 obs 段）+ 绑定它的测试应用。"""
    app = make_test_app(**overrides)
    root = app.state.test_root
    data = {"ak": "TOPLEVELAKXXXXX", "sk": "TOPLEVELSKXXXXXXXXXXXXXXXXXXXX",
            "region": "ap-southeast-1", **(top or {})}
    if obs is not None:
        data["obs"] = obs
    (root / "scope.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return app


def _enc(plain, key=TEST_KEY):
    return web_obs._encrypt_token(plain, key)


# ---- GET /api/obs/config：脱敏视图 ----

async def test_get_config_masks_enc_credentials():
    with _Env():
        os.environ["OBS_ENC_KEY"] = TEST_KEY
        for k in ENV_KEYS:
            if k != "OBS_ENC_KEY":
                os.environ.pop(k, None)
        app = _make_scope_app(obs={
            "bucket": "cfg-bucket",
            "ak_enc": _enc(PLAIN_AK), "sk_enc": _enc(PLAIN_SK),
            "endpoint": "https://obs.ap-southeast-1.myhuaweicloud.com",
        })
        async with async_client(app) as client:
            r = await client.get("/api/obs/config")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["bucket"] == "cfg-bucket"
            assert data["configured"] is True, data
            assert data["ak"]["source"] == "scope-enc", data["ak"]
            assert data["ak"]["masked"].startswith("AKTE") and "***" in data["ak"]["masked"]
            assert data["ak"]["length"] == len(PLAIN_AK)
            assert data["enc_key"]["source"] == "env"
            # 明文永不出服务（整响应体检索）
            assert PLAIN_AK not in r.text and PLAIN_SK not in r.text


async def test_get_config_reports_plain_env_top_sources():
    with _Env():
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        # obs 明文 → scope-plain
        app = _make_scope_app(obs={"bucket": "b", "ak": PLAIN_AK, "sk": PLAIN_SK})
        async with async_client(app) as client:
            r = await client.get("/api/obs/config")
            assert r.json()["ak"]["source"] == "scope-plain", r.json()["ak"]
            assert PLAIN_AK not in r.text
        # 全缺回落顶层 → scope-top；顶层凭据可用即视为已配置（与 _load_conf 同语义）
        app = _make_scope_app(obs={"bucket": "b"})
        async with async_client(app) as client:
            r = await client.get("/api/obs/config")
            data = r.json()
            assert data["ak"]["source"] == "scope-top", data["ak"]
            assert data["configured"] is True, data
        # OBS env 最高优先
        os.environ["OBS_AK"], os.environ["OBS_SK"] = "ENVAKXXXX", "ENVSKXXXXXXXX"
        async with async_client(app) as client:
            r = await client.get("/api/obs/config")
            assert r.json()["ak"]["source"] == "env", r.json()["ak"]
        # 空 scope（未配置）同样可打开：全 none 不抛
        async with async_client(make_test_app()) as client:
            r = await client.get("/api/obs/config")
            assert r.status_code == 200, r.text
            assert r.json()["configured"] is False


# ---- POST /api/obs/config：密文落盘 + 明文自动删除 ----

async def test_save_config_encrypts_and_deletes_plaintext():
    with _Env():
        os.environ["OBS_ENC_KEY"] = TEST_KEY
        for k in ENV_KEYS:
            if k != "OBS_ENC_KEY":
                os.environ.pop(k, None)
        # 起始态：obs 段带明文 ak/sk（历史遗留）——保存后必须被删
        app = _make_scope_app(obs={
            "bucket": "old-bucket", "ak": "OLDPLAINAK0000000X", "sk": "OLDPLAINSK000000000000000000000000X",
        })
        async with async_client(app) as client:
            r = await client.post("/api/obs/config", json={
                "ak": PLAIN_AK, "sk": PLAIN_SK, "bucket": "new-bucket",
            })
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["saved"] is True
            assert data["ak"]["source"] == "scope-enc", data["ak"]
            # 响应面无明文
            assert PLAIN_AK not in r.text and PLAIN_SK not in r.text
            # 落盘态：密文在、明文删、顶层 ECS 凭据原样
            scope = yaml.safe_load((app.state.test_root / "scope.yaml").read_text(encoding="utf-8"))
            obs = scope["obs"]
            assert obs["ak_enc"].startswith("enc:v1:"), obs
            assert obs["sk_enc"].startswith("enc:v1:")
            assert "ak" not in obs and "sk" not in obs, obs  # 明文键已删
            assert obs["bucket"] == "new-bucket"
            assert scope["ak"] == "TOPLEVELAKXXXXX"  # 顶层 ECS 凭据不动
            # 密文回环：运行时解密即用户输入
            assert web_obs._decrypt_token(obs["ak_enc"]) == PLAIN_AK
            assert web_obs._decrypt_token(obs["sk_enc"]) == PLAIN_SK
            # domain 随新桶重算
            assert obs["domain"] == f"https://new-bucket.obs.ap-southeast-1.myhuaweicloud.com"
            # 再拉一次视图与保存结果一致
            r2 = await client.get("/api/obs/config")
            assert r2.json()["bucket"] == "new-bucket"
            assert r2.json()["configured"] is True


async def test_save_config_preserves_comments_and_other_sections():
    with _Env():
        os.environ["OBS_ENC_KEY"] = TEST_KEY
        app = _make_scope_app()
        root = app.state.test_root
        old_ak_enc = _enc("OLDSECRETAK0000000X")
        (root / "scope.yaml").write_text(
            "# 顶层凭据（ECS 用，勿动）\n"
            "ak: TOPLEVELAKXXXXX\n"
            "sk: TOPLEVELSKXXXXXXXXXXXXXXXXXXXX\n"
            "region: ap-southeast-1\n"
            "# ECS 拉起默认规格\n"
            "ecs_create:\n"
            "  flavor: kc1.xlarge.2\n"
            "obs:\n"
            "  bucket: old-bucket              # 目标桶\n"
            "  # 凭据密文（enc:v1）\n"
            f"  ak_enc: {old_ak_enc}\n"
            "  endpoint: https://obs.ap-southeast-1.myhuaweicloud.com\n"
            "  domain: https://old-bucket.obs.ap-southeast-1.myhuaweicloud.com\n",
            encoding="utf-8",
        )
        async with async_client(app) as client:
            r = await client.post("/api/obs/config", json={"bucket": "new-bucket"})
            assert r.status_code == 200, r.text
        text = (root / "scope.yaml").read_text(encoding="utf-8")
        # 注释与其余段逐行保留；未提交的 ak_enc 原串不动
        assert "# 顶层凭据（ECS 用，勿动）" in text
        assert "# ECS 拉起默认规格" in text
        assert "# 凭据密文（enc:v1）" in text
        assert "flavor: kc1.xlarge.2" in text
        assert f"ak_enc: {old_ak_enc}" in text
        assert 'bucket: "new-bucket"' in text  # 值经 json 转义（YAML 兼容）
        # 解析态正确（文本编辑不破坏 YAML）
        data = yaml.safe_load(text)
        assert data["obs"]["bucket"] == "new-bucket"
        assert data["ecs_create"]["flavor"] == "kc1.xlarge.2"
        assert data["ak"] == "TOPLEVELAKXXXXX"


async def test_save_config_region_recomputes_endpoint_domain():
    with _Env():
        os.environ["OBS_ENC_KEY"] = TEST_KEY
        app = _make_scope_app(obs={
            "bucket": "b1", "region": "cn-north-4",
            "endpoint": "https://obs.cn-north-4.myhuaweicloud.com",
            "domain": "https://b1.obs.cn-north-4.myhuaweicloud.com",
        })
        async with async_client(app) as client:
            r = await client.post("/api/obs/config", json={"region": "ap-southeast-1"})
            assert r.status_code == 200, r.text
        obs = yaml.safe_load((app.state.test_root / "scope.yaml").read_text(encoding="utf-8"))["obs"]
        # 换 region：endpoint/domain 跟着重算，无旧 region 残留生效
        assert obs["endpoint"] == "https://obs.ap-southeast-1.myhuaweicloud.com"
        assert obs["domain"] == "https://b1.obs.ap-southeast-1.myhuaweicloud.com"


async def test_save_config_keygen_when_no_key_anywhere():
    """env 与密钥文件全缺：现场生成 .obs-secret.key（600）并立即可解密。

    ENC_KEY_FILE 常量指到临时路径——绝不能碰仓库根真密钥文件。"""
    real_key_file = web_obs.ENC_KEY_FILE
    real_cached = web_obs._enc_key_cached
    with _Env(), tempfile.TemporaryDirectory() as td:
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        web_obs.ENC_KEY_FILE = Path(td) / ".obs-secret.key"
        web_obs._enc_key_cached = None
        try:
            app = _make_scope_app(obs={"bucket": "b"})
            async with async_client(app) as client:
                r = await client.post("/api/obs/config", json={"ak": PLAIN_AK, "sk": PLAIN_SK})
                assert r.status_code == 200, r.text
                assert r.json()["enc_key"]["source"] == "file", r.json()
            key_file = web_obs.ENC_KEY_FILE
            assert key_file.is_file()
            assert (key_file.stat().st_mode & 0o777) == 0o600
            assert len(key_file.read_text(encoding="utf-8").strip()) == 64  # 32 字节 hex
            obs = yaml.safe_load((app.state.test_root / "scope.yaml").read_text(encoding="utf-8"))["obs"]
            assert web_obs._decrypt_token(obs["sk_enc"]) == PLAIN_SK  # 新钥即存即用
        finally:
            web_obs.ENC_KEY_FILE = real_key_file
            web_obs._enc_key_cached = real_cached


async def test_save_config_invalidates_client_cache():
    with _Env():
        os.environ["OBS_ENC_KEY"] = TEST_KEY
        app = _make_scope_app(obs={"bucket": "b"},
                              obs_health_fn=lambda: {"ok": True})  # 假检查：不触网不重建客户端
        scope = app.state.test_root / "scope.yaml"
        cache_key = str(scope.resolve())
        web_obs._clients[cache_key] = object()  # 预置旧客户端（模拟改配置前已建连）
        async with async_client(app) as client:
            r = await client.post("/api/obs/config", json={"bucket": "b2"})
            assert r.status_code == 200, r.text
        assert cache_key not in web_obs._clients  # 保存即失效：新配置无需重启生效


async def test_save_config_registers_plaintext_for_redaction():
    """新凭据明文进脱敏已知清单：此后事件流文本出现即被遮蔽（内存最后防线）。"""
    with _Env():
        os.environ["OBS_ENC_KEY"] = TEST_KEY
        app = _make_scope_app(obs={"bucket": "b"})
        known_before = list(redact.KNOWN_SECRETS)
        async with async_client(app) as client:
            r = await client.post("/api/obs/config", json={"ak": PLAIN_AK, "sk": PLAIN_SK})
            assert r.status_code == 200, r.text
        try:
            assert PLAIN_AK in redact.KNOWN_SECRETS and PLAIN_SK in redact.KNOWN_SECRETS
            assert redact.redact_text(f"泄漏面 {PLAIN_AK}") == f"泄漏面 {redact.MASK}"
        finally:
            redact.KNOWN_SECRETS[:] = known_before  # 不污染其余用例


# ---- 保存后的健康检查（尽力而为，结果如实带回） ----

async def test_save_config_returns_health_check_result():
    with _Env():
        os.environ["OBS_ENC_KEY"] = TEST_KEY
        healthy = {"ok": True, "bucket": "b", "region": "ap-southeast-1",
                   "endpoint": "https://obs.ap-southeast-1.myhuaweicloud.com",
                   "latency_ms": 42}
        app = _make_scope_app(obs={"bucket": "b"}, obs_health_fn=lambda: healthy)
        async with async_client(app) as client:
            r = await client.post("/api/obs/config", json={"bucket": "b2"})
            assert r.json()["check"] == healthy, r.json()

        class _Resp:
            status, reason = 403, "Forbidden"
            errorCode, errorMessage, requestId = "AccessDenied", "denied", "req-1"

        def boom():
            raise ObsApiError(_Resp())

        app = _make_scope_app(obs={"bucket": "b"}, obs_health_fn=boom)
        async with async_client(app) as client:
            r = await client.post("/api/obs/config", json={"bucket": "b2"})
            assert r.status_code == 200, r.text  # 保存已成功，检查失败如实带回
            assert r.json()["check"]["ok"] is False
            assert r.json()["check"]["error"]["error_code"] == "AccessDenied"


# ---- 入口校验 + 格式契约 ----

async def test_save_config_rejects_bad_requests():
    with _Env():
        os.environ["OBS_ENC_KEY"] = TEST_KEY
        app = _make_scope_app(obs={"bucket": "b"})
        async with async_client(app) as client:
            for body in (None, {}, {"bucket": "   "}, {"ak": 123}, {"region": ["x"]}):
                r = await client.post("/api/obs/config", json=body)
                assert r.status_code == 422, (body, r.text)


async def test_encrypt_format_agrees_with_tools_and_skill():
    """web 产的密文，tools 与 obs-skill 的解密器都能解（第四处实现不漂移）。"""
    repo = Path(__file__).resolve().parents[2]
    for p in (str(repo / "tools"), str(repo / ".claude" / "skills" / "obs-skill" / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import obs_client as skill_obs_client  # noqa: PLC0415
    import obs_secret  # noqa: PLC0415

    with _Env():
        os.environ["OBS_ENC_KEY"] = TEST_KEY
        token = web_obs._encrypt_token("web 侧产出-√", TEST_KEY)
        assert token.startswith("enc:v1:")
        assert obs_secret.decrypt(token) == "web 侧产出-√"
        assert skill_obs_client._decrypt_token(token) == "web 侧产出-√"


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
