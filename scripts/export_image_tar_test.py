#!/usr/bin/env python3
"""export_image_tar.py 的**行为自测**：证明「镜像尚未发布 ⇒ 等待」这条判据有效。

## 为什么必须做

2026-10-31 实测的真缺陷：4.6.4 的 ugreen release 流水线里 `upk` job 起跑时，
ghcr 上的多架构镜像**还在推送**（manifest 索引是 push 末尾才写的）⇒ 匿名查
manifest 返 **404** ⇒ 4 个 tar 全部导出失败 ⇒ UPK 产物没出来。
根因是导出脚本「404 只重试 3 次、共约 12 秒」⇒ **等不到镜像出现**。

⇒ 故本自测用**本地假 registry** 真跑脚本，证明三件事：
  ① 「404 ⇒ 等待」：假 registry 先返几次 404 再返 200 ⇒ 脚本应当**等到并成功**；
  ② 「等不到就放弃」：一直返 404 ⇒ 在预算内放弃并给出明确失败信息；
  ③ 「鉴权错不空等」：返 401 ⇒ 立刻失败（等也不会好）。

## 纪律

- **真跑真脚本**（子进程跑 `export_image_tar.py`），不测「另写一份的副本」；
- 假 registry **必须真按 HTTP 语义应答**（含 Content-Length），
  否则测的是「脚本连不上」而不是「脚本会不会等」；
- 子进程**清掉代理环境变量** ⇒ 否则 127.0.0.1 的请求被代理走 ⇒ 请求到不了本地。

用法：python3 scripts/export_image_tar_test.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPT = HERE / "export_image_tar.py"

CONFIG_BYTES = b'{"architecture":"amd64","os":"linux","config":{}}'
LAYER_BYTES = b"layer-payload"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


CONFIG_DIGEST = _sha256(CONFIG_BYTES)
LAYER_DIGEST = _sha256(LAYER_BYTES)

PLATFORM_MANIFEST = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": CONFIG_DIGEST,
        "size": len(CONFIG_BYTES),
    },
    "layers": [{
        "mediaType": "application/vnd.oci.image.layer.v1.tar",
        "digest": LAYER_DIGEST,
        "size": len(LAYER_BYTES),
    }],
}
PLATFORM_DIGEST = _sha256(json.dumps(PLATFORM_MANIFEST, sort_keys=True).encode())

INDEX = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [{
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": PLATFORM_DIGEST,
        "platform": {"os": "linux", "architecture": "amd64"},
    }],
}


class FakeRegistry:
    """最小 registry 假服务：token + manifest + blob。

    `fail_manifest_times` > 0 ⇒ 前 N 次 manifest 请求返回 `manifest_status`，
    之后返回 200（模拟「镜像刚发布出来」）。
    `manifest_status` 恒定 ⇒ 模拟「一直拿不到」（404 / 401 等）。
    """

    def __init__(self, fail_manifest_times: int = 0, manifest_status: int = 404):
        self.fail_manifest_times = fail_manifest_times
        self.manifest_status = manifest_status
        self.manifest_hits = 0
        self.blob_hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            # ⚠️ 默认是 HTTP/1.0 ⇒ 每个响应后断连 ⇒ urllib 复用连接会报
            #    RemoteDisconnected ⇒ 测的就不是「脚本会不会等」而是「能不能连上」。
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # 静音
                pass

            def _send(self, code: int, body: bytes, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/token":
                    self._send(200, json.dumps({"token": "anon-token"}).encode())
                    return
                if path == "/v2/pigeonbox/server/manifests/" + PLATFORM_DIGEST:
                    self._send(200, json.dumps(PLATFORM_MANIFEST).encode())
                    return
                if path == "/v2/pigeonbox/server/manifests/9.9.9":
                    outer.manifest_hits += 1
                    if outer.manifest_hits <= outer.fail_manifest_times:
                        self._send(outer.manifest_status,
                                   b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
                        return
                    if outer.manifest_status != 404 and outer.fail_manifest_times == 0:
                        # 恒定失败模式
                        self._send(outer.manifest_status,
                                   b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
                        return
                    self._send(200, json.dumps(INDEX).encode())
                    return
                if path.startswith("/v2/pigeonbox/server/blobs/"):
                    outer.blob_hits += 1
                    digest = path.rsplit("/", 1)[-1]
                    data = CONFIG_BYTES if digest == CONFIG_DIGEST else LAYER_BYTES
                    self._send(200, data, "application/octet-stream")
                    return
                self._send(404, b'{"errors":[{"code":"NOT_FOUND"}]}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def run_export(base_url: str, wait_minutes: float, retries: int = 3):
    """真跑 export_image_tar.py（子进程），返回 (exit_code, 合并输出, 产物存在, 秒数)。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    with tempfile.TemporaryDirectory(prefix="upk-export-") as tmp:
        out = Path(tmp) / "img.tar"
        t0 = time.monotonic()
        try:
            p = subprocess.run(
                [sys.executable, str(SCRIPT),
                 "--image", "ghcr.io/pigeonbox/server", "--tag", "9.9.9",
                 "--platform", "linux/amd64", "--out", str(out),
                 "--wait-minutes", str(wait_minutes),
                 "--retries", str(retries),
                 "--base-url", base_url],
                capture_output=True, text=True, timeout=90, env=env)
        except subprocess.TimeoutExpired:
            # 超时 = 脚本没在预算内收手 ⇒ 判「没通过」，而不是让自测整体崩掉。
            # ⚠️ 必须与「正常放弃」区分开：两者 exit code 都非零 ⇒ 只看退出码
            #    会把「挂死」当成「正确放弃」⇒ 判据失去区分力（2026-10-31 实测）。
            return 124, "✗ 超时：脚本未在预算内结束（疑似无限等待）", out.exists(), \
                time.monotonic() - t0
        merged = p.stdout + p.stderr
        produced = out.exists()
        return p.returncode, merged, produced, time.monotonic() - t0


def main() -> int:
    failures = []

    # ── ① 404 ⇒ 等待 ⇒ 最终成功（判据「等」有区分力的核心用例）──────────
    # ⚠️ 404 的次数必须**超过重试预算**（retries=3 ⇒ 至少 4 次），
    #    否则「旧的 3 次重试」本来就能过 ⇒ 判据区分不出「等」与「重试」。
    #    ⇒ 2026-10-31 首版这里只返 404 两次 ⇒ 变异 M1（去掉等待）仍报绿
    #       ⇒ 判据无区分力，已修正。
    with FakeRegistry(fail_manifest_times=4, manifest_status=404) as reg:
        code, out, produced, secs = run_export(reg.base_url, wait_minutes=0.5, retries=3)
        if code != 0 or not produced:
            failures.append(f"① 404 后应等到并导出成功，实际 exit={code} 产物={produced}")
            print(f"  ✗ ① 404 后应等到并导出成功：exit={code}\n{out}")
        elif reg.manifest_hits <= 3:
            failures.append(f"① manifest 只被请求 {reg.manifest_hits} 次"
                            f"（≤ 重试预算 3）⇒ 证明没在等")
            print(f"  ✗ ① manifest 只请求了 {reg.manifest_hits} 次 ⇒ 没在等")
        else:
            print(f"  ✓ ① 404×4 后等到并导出成功"
                  f"（manifest 被请求 {reg.manifest_hits} 次 > 重试预算 3，用时 {secs:.0f}s）")

    # ── ② 一直 404 ⇒ 预算内放弃（不能无限等）────────────────────────────
    # ⚠️ 必须区分「在预算内放弃」与「挂死到超时」：两者 exit code 都非零
    #    ⇒ 只看退出码会把「挂死」判成「正确放弃」⇒ 判据失去区分力
    #    （2026-10-31 首版就是这么漏的：变异 M4「不看预算无限等」仍报绿）。
    with FakeRegistry(fail_manifest_times=10**9, manifest_status=404) as reg:
        code, out, produced, secs = run_export(reg.base_url, wait_minutes=0.1, retries=2)
        if code == 124:
            failures.append("② 一直 404 时应**在预算内**放弃，实际是挂死到超时")
            print("  ✗ ② 一直 404 时挂死到超时（应在预算内放弃）")
        elif code == 0:
            failures.append("② 一直 404 时应失败，实际 exit=0")
            print("  ✗ ② 一直 404 时应失败，实际 exit=0")
        elif secs > 60:
            failures.append(f"② 放弃得太慢（{secs:.0f}s），不像在预算内收手")
            print(f"  ✗ ② 放弃得太慢（{secs:.0f}s）")
        elif produced:
            failures.append("② 失败时不该留下产物")
            print("  ✗ ② 失败时不该留下产物")
        elif "失败" not in out:
            failures.append("② 失败信息里没有说明失败")
            print("  ✗ ② 失败信息里没有说明失败")
        else:
            print(f"  ✓ ② 一直 404 ⇒ 预算内放弃并给出失败信息"
                  f"（manifest 被请求 {reg.manifest_hits} 次，用时 {secs:.0f}s）")

    # ── ③ 401 ⇒ 立刻失败，不空等（等也不会好）────────────────────────────
    with FakeRegistry(fail_manifest_times=10**9, manifest_status=401) as reg:
        code, out, produced, secs = run_export(reg.base_url, wait_minutes=5.0, retries=2)
        if code == 0:
            failures.append("③ 401 时应失败，实际 exit=0")
            print("  ✗ ③ 401 时应失败，实际 exit=0")
        elif reg.manifest_hits > 4:
            failures.append(f"③ 401 应立刻失败，却请求了 {reg.manifest_hits} 次 manifest")
            print(f"  ✗ ③ 401 应立刻失败，却请求了 {reg.manifest_hits} 次 manifest")
        else:
            print(f"  ✓ ③ 401 ⇒ 立刻失败不空等"
                  f"（manifest 被请求 {reg.manifest_hits} 次，用时 {secs:.0f}s）")

    # ── ④ 基线：不给等待预算（wait=0）时 404 应失败 ──────────────────────
    #    ⇒ 证明 ① 的成功不是因为「404 也能导出」，而真的是「等到了」
    with FakeRegistry(fail_manifest_times=10**9, manifest_status=404) as reg:
        code, out, produced, secs = run_export(reg.base_url, wait_minutes=0.0, retries=2)
        if code == 0:
            failures.append("④ wait=0 且一直 404 时应失败，实际 exit=0")
            print("  ✗ ④ wait=0 且一直 404 时应失败，实际 exit=0")
        else:
            print("  ✓ ④ 无等待预算 ⇒ 404 直接失败（旧行为不变）")

    if failures:
        print(f"\n✗ {len(failures)} 条判据无效：")
        for f in failures:
            print("   -", f)
        return 1
    print("\n✓ 4 条判据全部有效（等待/放弃/不空等/基线）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
