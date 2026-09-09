#!/usr/bin/env python
"""obs-skill 测试 —— 两条缝。

- 缝 A：纯逻辑（对象名规范 / 上传计划 / URL 编码 / obs 段解析）。
- 缝 B：命令行入口的 dry-run 端到端（argv → stdout JSON），不触网。

纯 assert，无 pytest（契合 ecs-skill / ims-skill 最小依赖调性）。
运行：python .claude/skills/obs-skill/tests/test_obs_ops.py
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
from argparse import Namespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "tools"))
import obscli as obs  # noqa: E402 —— 入口刻意不叫 obs.py（与 SDK 包名 `obs` 同名会自我遮蔽）
from obs_client import (  # noqa: E402
    Credentials,
    _decrypt_token,
    resolve_credentials,
    resolve_obs_target,
)
from obs_ops import (  # noqa: E402
    build_upload_plan,
    join_prefix,
    normalize_key,
    public_url,
)


# ---- 固件 ----
def make_scope(**obs_overrides):
    scope = {"ak": "AK-PLACEHOLDER", "sk": "SK-PLACEHOLDER", "region": "ap-southeast-1"}
    if obs_overrides:
        scope["obs"] = obs_overrides
    return scope


def make_creds():
    return Credentials(ak="AK-PLACEHOLDER", sk="SK-PLACEHOLDER", region="ap-southeast-1")


# ---- 缝 A：对象名规范 ----
def test_normalize_key_strips_leading_slash():
    assert normalize_key("/a/b.png") == "a/b.png", normalize_key("/a/b.png")
    assert normalize_key("//a.png") == "a.png"


def test_normalize_key_backslash_to_slash():
    assert normalize_key("a\\b.png") == "a/b.png"


def test_normalize_key_rejects_empty():
    for bad in ("", "   ", "/"):
        try:
            normalize_key(bad)
            raise AssertionError(f"{bad!r} 应抛 ValueError")
        except ValueError:
            pass


def test_normalize_key_rejects_over_1024():
    try:
        normalize_key("x" * 1025)
        raise AssertionError("超长对象名应抛 ValueError")
    except ValueError as e:
        assert "1024" in str(e)


def test_join_prefix():
    assert join_prefix("", "a.png") == "a.png"
    assert join_prefix(None, "a.png") == "a.png"
    assert join_prefix("rel", "a.png") == "rel/a.png"
    assert join_prefix("rel/", "a.png") == "rel/a.png"
    assert join_prefix("/rel/sub/", "a.png") == "rel/sub/a.png"


def test_public_url_quotes_path():
    url = public_url("https://b.obs.r.myhuaweicloud.com/", "dir/文件 name.png")
    assert url == "https://b.obs.r.myhuaweicloud.com/dir/%E6%96%87%E4%BB%B6%20name.png", url
    assert public_url("https://d", "a/b.png") == "https://d/a/b.png"  # '/' 不被编码


# ---- 缝 A：上传计划 ----
def test_plan_file_with_explicit_key():
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "app.tar.gz"
        f.write_bytes(b"12345")
        plan = build_upload_plan(f, key="releases/v1/app.tar.gz", prefix=None)
        assert len(plan) == 1
        assert plan[0]["key"] == "releases/v1/app.tar.gz"
        assert plan[0]["size"] == 5


def test_plan_file_default_key_is_basename():
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "app.tar.gz"
        f.write_bytes(b"x")
        assert build_upload_plan(f, key=None, prefix=None)[0]["key"] == "app.tar.gz"
        assert build_upload_plan(f, key=None, prefix="out")[0]["key"] == "out/app.tar.gz"


def test_plan_dir_default_prefix_is_dirname():
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td) / "bundle"
        (d / "sub").mkdir(parents=True)
        (d / "a.txt").write_bytes(b"aa")
        (d / "sub" / "b.png").write_bytes(b"bbb")
        keys = [it["key"] for it in build_upload_plan(d, key=None, prefix=None)]
        assert keys == ["bundle/a.txt", "bundle/sub/b.png"], keys


def test_plan_dir_prefix_empty_lands_at_root():
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td) / "bundle"
        d.mkdir()
        (d / "a.txt").write_bytes(b"aa")
        keys = [it["key"] for it in build_upload_plan(d, key=None, prefix="")]
        assert keys == ["a.txt"], keys


def test_plan_dir_rejects_explicit_key():
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td) / "bundle"
        d.mkdir()
        (d / "a.txt").write_bytes(b"a")
        try:
            build_upload_plan(d, key="x", prefix=None)
            raise AssertionError("目录 + --key 应抛 ValueError")
        except ValueError as e:
            assert "--prefix" in str(e)


def test_plan_missing_path_raises():
    try:
        build_upload_plan(pathlib.Path("/nonexistent-xyz"), key=None, prefix=None)
        raise AssertionError("路径不存在应抛 FileNotFoundError")
    except FileNotFoundError:
        pass


# ---- 缝 A：obs 段解析 ----
def test_target_requires_bucket():
    try:
        resolve_obs_target(make_scope(), make_creds())
        raise AssertionError("无桶名应抛 ValueError")
    except ValueError as e:
        assert "桶" in str(e)


def test_target_bucket_from_scope_and_cli():
    t = resolve_obs_target(make_scope(bucket="test-image-gen"), make_creds())
    assert t.bucket == "test-image-gen"
    assert t.endpoint == "https://obs.ap-southeast-1.myhuaweicloud.com"
    assert t.domain == "https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com"
    t2 = resolve_obs_target(make_scope(bucket="test-image-gen"), make_creds(), bucket="other-b")
    assert t2.bucket == "other-b"
    assert t2.domain == "https://other-b.obs.ap-southeast-1.myhuaweicloud.com"


def test_target_explicit_endpoint_domain_win():
    t = resolve_obs_target(
        make_scope(bucket="b1-test", endpoint="https://obs.example.com/",
                   domain="https://cdn.example.com/"),
        make_creds(),
    )
    assert t.endpoint == "https://obs.example.com"
    assert t.domain == "https://cdn.example.com"


def test_target_rejects_bad_bucket_name():
    try:
        resolve_obs_target(make_scope(bucket="Bad_Bucket"), make_creds())
        raise AssertionError("非法桶名应抛 ValueError")
    except ValueError as e:
        assert "桶名不合法" in str(e)


def test_credentials_env_priority():
    old = {k: os.environ.get(k) for k in ("HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK", "HUAWEICLOUD_SDK_REGION")}
    try:
        os.environ.update(HUAWEICLOUD_SDK_AK="ENV-AK", HUAWEICLOUD_SDK_SK="ENV-SK",
                          HUAWEICLOUD_SDK_REGION="ap-southeast-3")
        c = resolve_credentials(make_scope())
        assert (c.ak, c.sk, c.region) == ("ENV-AK", "ENV-SK", "ap-southeast-3")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---- 缝 B：CLI dry-run 端到端（不触网）----
def run_cli(tmp_scope: pathlib.Path, *argv: str) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = obs.main(["--scope", str(tmp_scope), *argv])
    return rc, json.loads(buf.getvalue())


def write_scope(path: pathlib.Path) -> pathlib.Path:
    import yaml
    path.write_text(yaml.safe_dump({
        "ak": "AK-PLACEHOLDER", "sk": "SK-PLACEHOLDER",
        "region": "ap-southeast-1",
        "obs": {"bucket": "test-image-gen"},
    }), encoding="utf-8")
    return path


def test_cli_upload_dry_run_file():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        f = td / "app.tar.gz"
        f.write_bytes(b"12345678")
        rc, out = run_cli(write_scope(td / "scope.yaml"), "upload", str(f), "--dry-run")
        assert rc == 0 and out["ok"] is True and out["dry_run"] is True
        assert out["bucket"] == "test-image-gen"
        assert out["total_count"] == 1 and out["total_size"] == 8
        assert out["objects"][0]["key"] == "app.tar.gz"
        assert out["objects"][0]["local"] == str(f)
        # 域名按桶+region 推导
        assert out["domain"] == "https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com"


def test_cli_upload_dry_run_dir_with_prefix():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        d = td / "out"
        (d / "sub").mkdir(parents=True)
        (d / "a.txt").write_bytes(b"aa")
        (d / "sub" / "b.png").write_bytes(b"bbb")
        rc, out = run_cli(write_scope(td / "scope.yaml"), "upload", str(d),
                          "--prefix", "artifacts/x", "--dry-run")
        assert rc == 0 and out["total_count"] == 2
        assert [o["key"] for o in out["objects"]] == ["artifacts/x/a.txt", "artifacts/x/sub/b.png"]


def test_cli_upload_missing_path_fails_cleanly():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        rc, out = run_cli(write_scope(td / "scope.yaml"), "upload", str(td / "nope"), "--dry-run")
        assert rc == 1 and out["ok"] is False
        assert "不存在" in out["error"]


def test_cli_delete_without_targets_fails():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        rc, out = run_cli(write_scope(td / "scope.yaml"), "delete")
        assert rc == 1 and out["ok"] is False
        assert "--key" in out["error"] and "--prefix" in out["error"]


def test_cli_upload_requires_bucket_config():
    import yaml
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        scope = td / "scope.yaml"
        scope.write_text(yaml.safe_dump({"ak": "A", "sk": "S", "region": "ap-southeast-1"}),
                         encoding="utf-8")
        f = td / "a.txt"
        f.write_bytes(b"a")
        rc, out = run_cli(scope, "upload", str(f), "--dry-run")
        assert rc == 1 and out["ok"] is False and "桶" in out["error"]


# ---- OBS 独立凭据 + 加密（resolve_credentials / _decrypt_token） ----

OBS_ENV_KEYS = ("OBS_AK", "OBS_SK", "OBS_ENC_KEY",
                "HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK", "HUAWEICLOUD_SDK_REGION")


def _env_ctx(key_hex):
    """清掉相关 env 并设 OBS_ENC_KEY；返回 (saved, restore)。"""
    saved = {k: os.environ.get(k) for k in OBS_ENV_KEYS}
    for k in OBS_ENV_KEYS:
        os.environ.pop(k, None)
    if key_hex:
        os.environ["OBS_ENC_KEY"] = key_hex

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


def test_credentials_obs_plaintext_wins_over_top():
    restore = _env_ctx(None)
    try:
        scope = {"ak": "TOP-AK", "sk": "TOP-SK", "region": "ap-southeast-1",
                 "obs": {"bucket": "b-test", "ak": "OBS-AK", "sk": "OBS-SK"}}
        c = resolve_credentials(scope)
        assert (c.ak, c.sk) == ("OBS-AK", "OBS-SK"), (c.ak, c.sk)
    finally:
        restore()


def test_credentials_obs_encrypted_with_env_key():
    import secrets as pysecrets

    key_hex = pysecrets.token_bytes(32).hex()
    import obs_secret  # tools/obs_secret.py（sys.path 已加）

    restore = _env_ctx(key_hex)
    try:
        scope = {"ak": "TOP-AK", "sk": "TOP-SK", "region": "ap-southeast-1",
                 "obs": {"bucket": "b", "ak_enc": obs_secret.encrypt("OBS-AK"),
                         "sk_enc": obs_secret.encrypt("OBS-SK")}}
        c = resolve_credentials(scope)
        assert (c.ak, c.sk) == ("OBS-AK", "OBS-SK"), (c.ak, c.sk)
        # region 覆盖：obs.region > 顶层
        scope["obs"]["region"] = "cn-north-4"
        assert resolve_credentials(scope).region == "cn-north-4"
    finally:
        restore()


def test_credentials_fallback_to_top_when_obs_absent():
    restore = _env_ctx(None)
    try:
        c = resolve_credentials(make_scope())
        assert (c.ak, c.sk, c.region) == ("AK-PLACEHOLDER", "SK-PLACEHOLDER", "ap-southeast-1")
    finally:
        restore()


def test_decrypt_token_bad_format_and_wrong_key():
    import secrets as pysecrets

    key_hex = pysecrets.token_bytes(32).hex()
    import obs_secret

    restore = _env_ctx(key_hex)
    try:
        token = obs_secret.encrypt("明文")
        for bad in ("", "plain", "enc:v1:only", "enc:v2:YQ:YQ"):
            try:
                _decrypt_token(bad)
                raise AssertionError(f"{bad!r} 应报格式错误")
            except ValueError as e:
                assert "格式非法" in str(e)
        # 换错密钥 → 解密失败
        os.environ["OBS_ENC_KEY"] = pysecrets.token_bytes(32).hex()
        try:
            _decrypt_token(token)
            raise AssertionError("错密钥应报解密失败")
        except ValueError as e:
            assert "解密失败" in str(e)
        # 缺密钥 → 报缺密钥
        os.environ.pop("OBS_ENC_KEY", None)
        try:
            _decrypt_token(token)
            raise AssertionError("缺密钥应报错")
        except ValueError as e:
            assert "密钥" in str(e)
    finally:
        restore()


def test_enc_key_first_read_survives_key_file_deletion():
    """密钥首读驻内存：读到后删除密钥文件，本进程解密仍成功；模拟新
    进程（清缓存 + 无文件）才报缺密钥。"""
    import secrets as pysecrets

    import obs_client as oc  # 模块属性（ENC_KEY_FILE/缓存）可注入替换

    key_hex = pysecrets.token_bytes(32).hex()
    saved_env = os.environ.get("OBS_ENC_KEY")
    saved_file = oc.ENC_KEY_FILE
    saved_cache = oc._enc_key_cached
    try:
        os.environ.pop("OBS_ENC_KEY", None)
        oc._enc_key_cached = None
        with tempfile.TemporaryDirectory() as td:
            kf = pathlib.Path(td) / "key"
            kf.write_text(key_hex + "\n", encoding="utf-8")
            oc.ENC_KEY_FILE = kf
            # 用测试密钥造密文，随后清 env——让解密走文件路径
            os.environ["OBS_ENC_KEY"] = key_hex
            import obs_secret
            token = obs_secret.encrypt("驻留明文-√")
            os.environ.pop("OBS_ENC_KEY", None)
            assert oc._decrypt_token(token) == "驻留明文-√"  # 首读 → 驻内存
            kf.unlink()  # 运行期删密钥文件
            assert oc._decrypt_token(token) == "驻留明文-√"  # 仍成功（内存密钥）
            oc._enc_key_cached = None  # 模拟新进程
            try:
                oc._decrypt_token(token)
                raise AssertionError("清缓存且无文件应报缺密钥")
            except ValueError as e:
                assert "密钥" in str(e)
    finally:
        os.environ.pop("OBS_ENC_KEY", None)
        if saved_env is not None:
            os.environ["OBS_ENC_KEY"] = saved_env
        oc.ENC_KEY_FILE = saved_file
        oc._enc_key_cached = saved_cache


def main() -> int:
    tests = sorted(
        (fn, getattr(sys.modules[__name__], fn))
        for fn in dir(sys.modules[__name__])
        if fn.startswith("test_") and callable(getattr(sys.modules[__name__], fn))
    )
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
