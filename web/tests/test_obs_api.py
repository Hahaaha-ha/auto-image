#!/usr/bin/env python3
"""OBS 端点主缝测试 —— 清单 / 签名链接 / 文本预览三端点 + 错误面映射。

缝：同 test_artifacts 的 ASGI 测试客户端；obs 函数注入假实现（不触网、
不装 SDK）。默认（未注入）路径绑定测试用空 scope → ObsNotConfigured →
503，不阻断其余端点。纯 assert，无 pytest。

运行：python web/tests/test_obs_api.py
"""
import asyncio
import base64
import os
import secrets as pysecrets
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.obs import ObsApiError, normalize_key  # noqa: E402
from web.tests.support import async_client, make_test_app  # noqa: E402

FIXTURE_LIST = {
    "bucket": "test-image-gen", "region": "ap-southeast-1",
    "domain": "https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com",
    "count": 2, "truncated": False,
    "objects": [
        {"key": "deploy/lobechat/1.143.3/lobechat-install-result.md", "size": 5659,
         "etag": '"abc"', "last_modified": "2026/09/09 15:35:07", "storage_class": None},
        {"key": "readme.md", "size": 12, "etag": '"def"',
         "last_modified": "2026/09/09 10:00:00", "storage_class": None},
    ],
}


class StubResp:
    """ObsApiError 的错误面来源（SDK GetResult 失败形状的最小桩）。"""

    status = 403
    reason = "Forbidden"
    errorCode = "AccessDenied"
    errorMessage = "denied"
    requestId = "req-1"


async def test_default_scope_reports_not_configured():
    """未注入假函数时默认绑真实实现：空 scope → 三端点一律 503 不配置。"""
    async with async_client(make_test_app()) as client:
        for path in ("/api/obs/objects", "/api/obs/url?key=x.md", "/api/obs/content?key=x.md"):
            r = await client.get(path)
            assert r.status_code == 503, (path, r.text)
            assert "not configured" in r.json()["detail"], (path, r.text)


async def test_list_objects_shape_and_limit_clamp():
    seen = {}

    def fake_list(limit=1000):
        seen["limit"] = limit
        return FIXTURE_LIST

    async with async_client(make_test_app(obs_list_fn=fake_list)) as client:
        r = await client.get("/api/obs/objects?limit=99999")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["bucket"] == "test-image-gen"
        assert data["count"] == 2
        assert data["objects"][0]["key"].startswith("deploy/")
        assert seen["limit"] == 5000  # 上限钳制透传给实现


async def test_url_requires_key():
    def fake_url(key="", expires=3600):
        return {"key": normalize_key(key), "signed_url": f"s:{key}"}

    async with async_client(make_test_app(obs_url_fn=fake_url)) as client:
        r = await client.get("/api/obs/url")
        assert r.status_code == 422, r.text


async def test_url_shape_and_expires_clamp():
    def fake_url(key="", expires=3600):
        return {"key": key, "public_url": f"https://d/{key}",
                "signed_url": f"https://s/{key}", "expires_in": expires}

    async with async_client(make_test_app(obs_url_fn=fake_url)) as client:
        r = await client.get("/api/obs/url?key=deploy/a%20b.md&expires=999999")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["key"] == "deploy/a b.md"  # 查询参数自动百分号解码
        assert data["expires_in"] == 7 * 24 * 3600  # 有效期上限钳制（7 天）


async def test_content_text_entry():
    def fake_read(key=""):
        return {"dir": "deploy", "name": "a.md", "size": 3,
                "content_type": "text/plain", "content": "abc"}

    async with async_client(make_test_app(obs_read_fn=fake_read)) as client:
        r = await client.get("/api/obs/content?key=deploy/a.md")
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "abc"


async def test_content_binary_422_and_missing_404():
    def fake_binary(key=""):
        return {"binary": True, "dir": "rpm", "name": "x.rpm", "size": 9}

    def fake_missing(key=""):
        return None

    async with async_client(make_test_app(obs_read_fn=fake_binary)) as client:
        r = await client.get("/api/obs/content?key=rpm/x.rpm")
        assert r.status_code == 422, r.text
        assert "signed" in r.json()["detail"]  # 指引签名链接下载
    async with async_client(make_test_app(obs_read_fn=fake_missing)) as client:
        r = await client.get("/api/obs/content?key=gone.md")
        assert r.status_code == 404, r.text


async def test_api_error_maps_502():
    def boom(limit=1000):
        raise ObsApiError(StubResp())

    async with async_client(make_test_app(obs_list_fn=boom)) as client:
        r = await client.get("/api/obs/objects")
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error_code"] == "AccessDenied"


async def test_normalize_key_rules():
    assert normalize_key("/a/b.md") == "a/b.md"
    assert normalize_key("  x.md ") == "x.md"
    for bad in ("", "   ", "/"):
        try:
            normalize_key(bad)
            raise AssertionError(f"{bad!r} 应抛 ValueError")
        except ValueError:
            pass
    try:
        normalize_key("k" * 1025)
        raise AssertionError("超长 key 应抛 ValueError")
    except ValueError:
        pass


async def test_archive_shape_and_path_resolution():
    """归档端点：paths 经产物路径约束 resolve 后交给实现，返回逐项结果。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out = root / "deploy" / "lobechat"
        out.mkdir(parents=True)
        (out / "result.md").write_text("# 结果\n", encoding="utf-8")
        seen = {}

        def fake_archive(items):
            seen["items"] = items
            return {
                "bucket": "test-image-gen", "region": "ap-southeast-1",
                "domain": "https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com",
                "archived": [{"key": k, "size": 1, "url": f"https://d/{k}"} for k, _p in items],
                "failed": [], "ok": True, "count": len(items),
            }

        overrides = {
            "artifact_roots": {"deploy": root / "deploy"},
            "obs_archive_fn": fake_archive,
        }
        async with async_client(make_test_app(**overrides)) as client:
            # 缺失项如实跳过（skipped 计数），有效项照传
            r = await client.post("/api/obs/archive", json={
                "paths": ["deploy/lobechat/result.md", "deploy/absent.md"],
            })
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["ok"] is True and data["count"] == 1
            assert data["requested"] == 2 and data["skipped"] == 1
            key, path = seen["items"][0]
            assert key == "deploy/lobechat/result.md"
            assert Path(path).is_file() and Path(path).name == "result.md"


async def test_archive_rejects_bad_requests_and_all_missing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "deploy").mkdir()
        overrides = {
            "artifact_roots": {"deploy": root / "deploy"},
            "obs_archive_fn": lambda items: {"ok": True, "archived": [], "failed": [], "count": 0},
        }
        async with async_client(make_test_app(**overrides)) as client:
            for body in (None, {}, {"paths": []}, {"paths": [1]}, {"paths": [""]}):
                r = await client.post("/api/obs/archive", json=body)
                assert r.status_code == 422, (body, r.text)
            # 合法请求但一个有效产物都没有 → 404（不触云）
            r = await client.post("/api/obs/archive", json={"paths": ["deploy/absent.md"]})
            assert r.status_code == 404, r.text


async def test_archive_not_configured_502_and_503():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out = root / "deploy"
        out.mkdir()
        (out / "a.md").write_text("x", encoding="utf-8")
        # 默认实现绑定空 scope：resolve 命中后 ObsNotConfigured → 503
        async with async_client(make_test_app(artifact_roots={"deploy": out})) as client:
            r = await client.post("/api/obs/archive", json={"paths": ["deploy/a.md"]})
            assert r.status_code == 503, r.text
            assert "not configured" in r.json()["detail"]

        def boom(items):
            raise ObsApiError(StubResp())

        async with async_client(make_test_app(
            artifact_roots={"deploy": out}, obs_archive_fn=boom,
        )) as client:
            r = await client.post("/api/obs/archive", json={"paths": ["deploy/a.md"]})
            assert r.status_code == 502, r.text
            assert r.json()["detail"]["error_code"] == "AccessDenied"


async def test_archive_zip_name_validation():
    """包名清洗：空/路径分隔/遍历段/超长 → 422；合法名自动补 .zip。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out = root / "deploy"
        out.mkdir()
        (out / "a.md").write_text("x", encoding="utf-8")
        overrides = {
            "artifact_roots": {"deploy": out},
            "obs_upload_zip_fn": lambda key, data: {"key": key, "size": len(data)},
        }
        async with async_client(make_test_app(**overrides)) as client:
            for name in ("", "   ", "a/b.zip", "x\\y.zip", "..zip", "." * 101):
                r = await client.post("/api/obs/archive-zip", json={"paths": ["deploy/a.md"], "name": name})
                assert r.status_code == 422, (name, r.text)
            # 合法名（不带后缀）→ 正常 200
            r = await client.post("/api/obs/archive-zip", json={"paths": ["deploy/a.md"], "name": "my-bundle"})
            assert r.status_code == 200, r.text
            assert r.json()["key"] == "zip/my-bundle.zip", r.text


async def test_archive_zip_shape_and_packing():
    """zip 归档：服务端内存打包（PK 头）后直传，key 固定 zip/ 前缀。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out = root / "deploy" / "lobechat"
        out.mkdir(parents=True)
        (out / "a.md").write_text("# a\n", encoding="utf-8")
        (out / "b.md").write_text("# b\n", encoding="utf-8")
        seen = {}

        def fake_upload(key, data):
            seen["key"], seen["data"] = key, data
            return {"bucket": "test-image-gen", "key": key, "size": len(data), "url": f"https://d/{key}"}

        async with async_client(make_test_app(
            artifact_roots={"deploy": root / "deploy"}, obs_upload_zip_fn=fake_upload,
        )) as client:
            r = await client.post("/api/obs/archive-zip", json={
                "paths": ["deploy/lobechat/a.md", "deploy/lobechat/b.md", "deploy/absent.md"],
                "name": "bundle.zip",
            })
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["key"] == "zip/bundle.zip"
            assert data["zipped"] == 2  # 缺失项跳过，计数如实
            assert data["sources"] == ["deploy/lobechat/a.md", "deploy/lobechat/b.md", "deploy/absent.md"]
            assert seen["key"] == "zip/bundle.zip"
            assert seen["data"][:2] == b"PK"  # 真 zip 字节流
            import io, zipfile
            zf = zipfile.ZipFile(io.BytesIO(seen["data"]))
            assert sorted(zf.namelist()) == ["a.md", "b.md"]  # 平铺文件名，不带目录树


async def test_archive_zip_missing_and_not_configured():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "deploy").mkdir()
        called = {"n": 0}

        def fake_upload(key, data):
            called["n"] += 1
            return {"key": key, "size": 0}

        async with async_client(make_test_app(
            artifact_roots={"deploy": root / "deploy"}, obs_upload_zip_fn=fake_upload,
        )) as client:
            # 无有效产物 → 404，且不触云（fake 未被调用）
            r = await client.post("/api/obs/archive-zip", json={"paths": ["deploy/absent.md"], "name": "x"})
            assert r.status_code == 404, r.text
            assert called["n"] == 0
        # 默认实现绑定空 scope → 503
        async with async_client(make_test_app(artifact_roots={"deploy": root / "deploy"})) as client:
            (root / "deploy" / "a.md").write_text("x", encoding="utf-8")
            r = await client.post("/api/obs/archive-zip", json={"paths": ["deploy/a.md"], "name": "x"})
            assert r.status_code == 503, r.text
            assert "not configured" in r.json()["detail"]


# ---- OBS 独立凭据 + 加密（_load_conf / _decrypt_token 纯单元，不触网） ----

ENV_KEYS = ("OBS_AK", "OBS_SK", "OBS_ENC_KEY",
            "HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK", "HUAWEICLOUD_SDK_REGION")


def _write_scope(td: Path, obs: dict) -> Path:
    scope = Path(td) / "scope.yaml"
    scope.write_text(yaml.safe_dump({
        "ak": "TOP-AK", "sk": "TOP-SK", "region": "ap-southeast-1", "obs": obs,
    }), encoding="utf-8")
    return scope


def _enc(test_key: str, plain: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = pysecrets.token_bytes(12)
    ct = AESGCM(bytes.fromhex(test_key)).encrypt(nonce, plain.encode(), None)
    b = lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")  # noqa: E731
    return f"enc:v1:{b(nonce)}:{b(ct)}"


async def test_obs_credential_priority_and_encryption():
    from web import obs as web_obs

    test_key = pysecrets.token_bytes(32).hex()
    bad_key = pysecrets.token_bytes(32).hex()
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    try:
        os.environ["OBS_ENC_KEY"] = test_key
        for k in ENV_KEYS:
            if k != "OBS_ENC_KEY":
                os.environ.pop(k, None)
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # 1) obs 段加密凭据生效（优先于顶层明文）
            scope = _write_scope(td, {"bucket": "b-test",
                                      "ak_enc": _enc(test_key, "OBS-AK"),
                                      "sk_enc": _enc(test_key, "OBS-SK")})
            ak, sk, region, bucket, _ep, _dm = web_obs._load_conf(scope)
            assert (ak, sk, bucket) == ("OBS-AK", "OBS-SK", "b-test")
            assert region == "ap-southeast-1"  # region 缺省回落顶层
            # 2) obs.region 覆盖（endpoint/domain 推导跟随）
            scope = _write_scope(td, {"bucket": "b-test", "region": "cn-north-4",
                                      "ak": "P", "sk": "Q"})
            ak, sk, region, bucket, endpoint, domain = web_obs._load_conf(scope)
            assert region == "cn-north-4"
            assert endpoint == "https://obs.cn-north-4.myhuaweicloud.com"
            assert domain == "https://b-test.obs.cn-north-4.myhuaweicloud.com"
            # 3) obs 段全缺 → 回落顶层（ECS 同套）
            scope = _write_scope(td, {"bucket": "b-test"})
            assert web_obs._load_conf(scope)[:3] == ("TOP-AK", "TOP-SK", "ap-southeast-1")
            # 4) OBS_AK/OBS_SK env 最高（胜过 obs 段密文）
            os.environ["OBS_AK"], os.environ["OBS_SK"] = "ENV-AK", "ENV-SK"
            scope = _write_scope(td, {"bucket": "b-test",
                                      "ak_enc": _enc(test_key, "OBS-AK"),
                                      "sk_enc": _enc(test_key, "OBS-SK")})
            assert web_obs._load_conf(scope)[:2] == ("ENV-AK", "ENV-SK")
            os.environ.pop("OBS_AK", None), os.environ.pop("OBS_SK", None)
            # 5) 密钥不匹配 → ObsNotConfigured（503 语义：配置问题非云故障）
            os.environ["OBS_ENC_KEY"] = bad_key
            scope = _write_scope(td, {"bucket": "b-test", "ak_enc": _enc(test_key, "X"),
                                      "sk": "plain-sk"})
            try:
                web_obs._load_conf(scope)
                raise AssertionError("错密钥应报 ObsNotConfigured")
            except web_obs.ObsNotConfigured as e:
                assert "解密失败" in str(e)
            os.environ["OBS_ENC_KEY"] = test_key
            # 6) 密文格式非法 → ObsNotConfigured
            scope = _write_scope(td, {"bucket": "b-test", "ak_enc": "not-encrypted",
                                      "sk": "plain-sk"})
            try:
                web_obs._load_conf(scope)
                raise AssertionError("坏格式应报 ObsNotConfigured")
            except web_obs.ObsNotConfigured as e:
                assert "格式非法" in str(e)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def test_encryption_format_agreement_across_implementations():
    """格式契约三路互验：tools/obs_secret.py 造的密文，web 与 obs-skill 的
    解密器都要能解——三处实现互不 import，靠这条测试钉住 enc:v1 不漂移。"""
    repo = Path(__file__).resolve().parents[2]
    for p in (str(repo / "tools"), str(repo / ".claude" / "skills" / "obs-skill" / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import obs_client as skill_obs_client  # noqa: PLC0415 —— skill 侧实现
    import obs_secret  # noqa: PLC0415 —— 工具侧实现
    from web import obs as web_obs

    test_key = pysecrets.token_bytes(32).hex()
    saved = os.environ.get("OBS_ENC_KEY")
    try:
        os.environ["OBS_ENC_KEY"] = test_key
        token = obs_secret.encrypt("契约明文-√")
        assert token.startswith("enc:v1:")
        assert web_obs._decrypt_token(token) == "契约明文-√"
        assert skill_obs_client._decrypt_token(token) == "契约明文-√"
    finally:
        if saved is None:
            os.environ.pop("OBS_ENC_KEY", None)
        else:
            os.environ["OBS_ENC_KEY"] = saved


async def test_health_endpoint_three_states():
    """健康检查三态：200 健康 / 503 未配置（密钥缺失等）/ 502 云侧失败。"""
    healthy = {
        "ok": True, "bucket": "test-image-gen", "region": "ap-southeast-1",
        "endpoint": "https://obs.ap-southeast-1.myhuaweicloud.com",
        "domain": "https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com",
        "latency_ms": 42,
    }
    async with async_client(make_test_app(obs_health_fn=lambda: healthy)) as client:
        r = await client.get("/api/obs/health")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True and r.json()["latency_ms"] == 42

    def boom():
        raise ObsApiError(StubResp())

    async with async_client(make_test_app(obs_health_fn=boom)) as client:
        r = await client.get("/api/obs/health")
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error_code"] == "AccessDenied"

    async with async_client(make_test_app()) as client:  # 空 scope → 默认实现 503
        r = await client.get("/api/obs/health")
        assert r.status_code == 503, r.text
        assert "not configured" in r.json()["detail"]


async def test_enc_key_first_read_survives_key_file_deletion():
    """密钥首读驻内存：读到后删除密钥文件，本进程解密仍成功；模拟新
    进程（清缓存 + 无文件）才如实报缺密钥。"""
    from web import obs as web_obs

    key_hex = pysecrets.token_bytes(32).hex()
    saved_env = os.environ.get("OBS_ENC_KEY")
    saved_file = web_obs.ENC_KEY_FILE
    saved_cache = web_obs._enc_key_cached
    try:
        os.environ.pop("OBS_ENC_KEY", None)
        web_obs._enc_key_cached = None
        with tempfile.TemporaryDirectory() as td:
            kf = Path(td) / "key"
            kf.write_text(key_hex + "\n", encoding="utf-8")
            web_obs.ENC_KEY_FILE = kf
            token = _enc(key_hex, "驻留明文-√")
            assert web_obs._decrypt_token(token) == "驻留明文-√"  # 首读 → 驻内存
            kf.unlink()  # 运行期删密钥文件
            assert web_obs._decrypt_token(token) == "驻留明文-√"  # 仍成功（内存密钥）
            web_obs._enc_key_cached = None  # 模拟新进程：缓存空 + 文件已删
            try:
                web_obs._decrypt_token(token)
                raise AssertionError("清缓存且无文件应报缺密钥")
            except web_obs.ObsNotConfigured as e:
                assert "密钥" in str(e)
    finally:
        os.environ.pop("OBS_ENC_KEY", None)
        if saved_env is not None:
            os.environ["OBS_ENC_KEY"] = saved_env
        web_obs.ENC_KEY_FILE = saved_file
        web_obs._enc_key_cached = saved_cache


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
