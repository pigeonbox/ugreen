#!/usr/bin/env python3
"""从 registry 导出镜像 tar（docker-load 兼容格式），供 UPK 应用包内嵌。

**为什么不用 `docker pull --platform` + `docker save`**：
  ① `docker save` 只能存**当前 daemon 能加载的架构** ⇒ 多架构镜像
     （linux/amd64 + linux/arm64）要导两份，得在两个平台各跑一次；
  ② CI 里的 docker daemon 常带失效代理（实测本机 Docker Desktop 代理
     指向 127.0.0.1:3128，而真代理在 10.10.30.250:7890 ⇒ 拉取直接 refused）
     ⇒ 依赖 daemon 的方案在 CI 里脆。
  ⇒ 本脚本**直接走 registry HTTP API**（匿名 token + manifest + blob），
     零外部依赖（纯标准库），任意平台可跑，可精确指定目标架构。

产物格式：与 `docker save` 同构的 tar（manifest.json + config + layer tars）
⇒ 可直接 `docker load -i`，也可被 UPK 的 rootfs_<arch>/images/ 内嵌。

用法：
  python3 scripts/export_image_tar.py \\
      --image ghcr.io/pigeonbox/server --tag 4.5.2 \\
      --platform linux/amd64 --out upk/rootfs_amd64/images/pigeonbox-server-4.5.2.tar

**为什么需要 --wait-minutes（2026-10-31 实测的 CI 真缺陷）**：
  镜像 tar 来自 ghcr 上的 server/frontend 镜像，而那些镜像是由**另一个仓**
  （pigeonbox/server）的 release 流水线推的 ⇒ 两边**并发**，本仓无从用
  `needs:` 排序。更关键的是：多架构 push 耗时约 20 分钟，而 manifest **索引**
  是 buildx 在 push **末尾**才写的 ⇒ 镜像 tag 在整个推送期间都查不到
  （匿名查 manifest 返 **404**）。
  实测：4.6.4 的 ugreen release 流水线里 `upk` job 在 tag 推送后 1 分钟就起跑
  ⇒ 4 个 tar 全部「取 manifest 失败（HTTP 404）」⇒ **UPK 产物没出来**。
  ⇒ 故本脚本支持「镜像尚未发布 ⇒ 轮询等待」：404/5xx 视为「还没好」，
  在 `--wait-minutes` 预算内持续重试（401/403 属鉴权问题 ⇒ 立即失败，不空等）。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Docker registry 的两类清单媒体类型（都要认）。
MANIFEST_TYPES = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class RegistryError(RuntimeError):
    """registry 侧的错误。`http_status` 用于区分「还没发布」与「真错」。

    404 = 该 tag 尚未发布（多架构 push 期间 manifest 索引还没写）
    5xx = registry 瞬时故障
    401/403 = 鉴权/权限问题（重试无用 ⇒ 必须立即失败）
    """

    def __init__(self, msg: str, http_status: int | None = None):
        super().__init__(msg)
        self.http_status = http_status


def _base_url(registry: str, base: str | None) -> str:
    """registry 的基址。`base` 仅测试用（指向本地假 registry），生产为 None。"""
    return base or f"https://{registry}"


def http(url: str, headers: dict[str, str] | None = None, binary: bool = False):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    return data if binary else data.decode("utf-8")


def token_for(registry: str, repo: str, base: str | None = None) -> str:
    """取 registry 的匿名 pull token（GHCR 允许匿名拉公开包）。"""
    url = (f"{_base_url(registry, base)}/token?scope=repository:{repo}:pull"
           f"&service={registry}")
    try:
        return json.loads(http(url))["token"]
    except Exception as exc:
        raise RegistryError(f"取 {registry} 的 pull token 失败：{exc}") from exc


def get_manifest(registry: str, repo: str, ref: str, tok: str,
                 base: str | None = None) -> dict:
    url = f"{_base_url(registry, base)}/v2/{repo}/manifests/{ref}"
    hdrs = {
        "Authorization": f"Bearer {tok}",
        "Accept": MANIFEST_TYPES,
    }
    try:
        raw = http(url, hdrs)
    except urllib.error.HTTPError as exc:
        raise RegistryError(
            f"取 manifest 失败（HTTP {exc.code}）：{url}",
            http_status=exc.code) from exc
    return json.loads(raw)


def pick_platform(index: dict, want_os: str, want_arch: str) -> str:
    """在 manifest list 里挑出目标平台的 manifest digest。"""
    for m in index.get("manifests", []):
        p = m.get("platform", {})
        if p.get("os") == want_os and p.get("architecture") == want_arch:
            return m["digest"]
    have = sorted(
        f"{m.get('platform',{}).get('os')}/{m.get('platform',{}).get('architecture')}"
        for m in index.get("manifests", []))
    raise RegistryError(
        f"镜像不含 {want_os}/{want_arch}（现有：{', '.join(have)}）")


def blob(registry: str, repo: str, digest: str, tok: str,
         base: str | None = None) -> bytes:
    url = f"{_base_url(registry, base)}/v2/{repo}/blobs/{digest}"
    hdrs = {"Authorization": f"Bearer {tok}"}
    try:
        return http(url, hdrs, binary=True)
    except urllib.error.HTTPError as exc:
        raise RegistryError(f"取 blob {digest[:19]} 失败（HTTP {exc.code}）",
                            http_status=exc.code) from exc


def split_image(image: str) -> tuple[str, str]:
    """拆镜像引用 → (registry, repo 路径)。

    `ghcr.io/pigeonbox/server` ⇒ ("ghcr.io", "pigeonbox/server")
    `docker.io/pigeonbox/server` ⇒ ("docker.io", "pigeonbox/server")
    """
    parts = image.split("/")
    if len(parts) == 1:
        raise RegistryError(f"镜像引用需带仓库路径：{image!r}")
    if "." in parts[0] or parts[0] == "localhost":
        return parts[0], "/".join(parts[1:])
    # 无点号的首段 = docker.io 的命名空间（官方惯例）
    return "docker.io", "/".join(parts)


def export(image: str, tag: str, os_name: str, arch: str,
           out: Path, base: str | None = None) -> None:
    registry, repo = split_image(image)
    tok = token_for(registry, repo, base)
    top = get_manifest(registry, repo, tag, tok, base)
    if "manifests" in top:
        digest = pick_platform(top, os_name, arch)
        manifest = get_manifest(registry, repo, digest, tok, base)
        image_digest = digest
    else:
        manifest = top
        image_digest = "sha256:" + hashlib.sha256(
            json.dumps(top, sort_keys=True).encode()).hexdigest()

    cfg_digest = manifest["config"]["digest"]
    cfg_bytes = blob(registry, repo, cfg_digest, tok, base)

    layers = manifest["layers"]
    print(f"  平台 {os_name}/{arch}：{len(layers)} 层，config {cfg_digest[:19]}")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    try:
        with tarfile.open(tmp, "w") as tar:
            # config
            cfg_name = cfg_digest.replace(":", "_") + ".json"
            _add_bytes(tar, cfg_name, cfg_bytes)
            # layers（docker save 的命名惯例）
            layer_names = []
            for i, layer in enumerate(layers):
                d = layer["digest"]
                data = blob(registry, repo, d, tok, base)
                name = d.replace(":", "_") + "/layer.tar"
                if layer.get("mediaType", "").endswith("gzip"):
                    # docker save 的 layer.tar 是**未压缩**的 tar ⇒ 需解压
                    name = d.replace(":", "_") + "/layer.tar"
                    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
                    _add_bytes(tar, name, raw)
                else:
                    _add_bytes(tar, name, data)
                layer_names.append(name)
            # manifest.json（docker save 的索引格式）
            manifest_json = [{
                "Config": cfg_name,
                "RepoTags": [f"{image}:{tag}"],
                "Layers": layer_names,
            }]
            _add_bytes(tar, "manifest.json",
                       json.dumps(manifest_json).encode("utf-8"))
            # repositories（老格式，docker load 兼容）
            _add_bytes(tar, "repositories", json.dumps({image: {tag: image_digest}})
                       .encode("utf-8"))
    except BaseException:
        # 半截产物留在磁盘上会让「有没有本车 tar」的判据误判 ⇒ 失败即清
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, out)
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"  ✓ {out}（{size_mb:.1f} MB）")


def export_with_wait(image: str, tag: str, os_name: str, arch: str,
                     out: Path, base: str | None = None,
                     wait_minutes: float = 0.0, retries: int = 3,
                     sleep=time.sleep, now=time.monotonic) -> int:
    """导出镜像 tar；镜像**尚未发布**时在预算内等待。

    分流判据（决定「等」还是「立刻失败」）：
    - **404**（tag 还没发布）⇒ 等：多架构 push 期间 manifest 索引未写，
      这是**正常中间态**，等它出现即可；
    - **5xx**（registry 瞬时故障）⇒ 等；
    - **401/403**（鉴权/权限）⇒ **不空等**，立刻失败（等也不会好）；
    - 其它异常 ⇒ 按原语义有限重试（`retries` 次）。

    `wait_minutes=0` ⇒ 退化成旧行为（404 走有限重试，约 12 秒）。
    """
    deadline = now() + wait_minutes * 60
    hard_failures = 0
    last: Exception | None = None
    while True:
        try:
            export(image, tag, os_name, arch, out, base=base)
            return 0
        except Exception as exc:
            last = exc
            status = getattr(exc, "http_status", None)
            publishable = status == 404 or (status is not None and status >= 500)
            remaining = deadline - now()
            if publishable and remaining > 0:
                # 轮询间隔：上限 15s（别把 registry 打成筛子），下限 2s；
                # 预算剩余越少 ⇒ 间隔越短（保证预算内能试够多次，
                # 否则「预算 30s + 间隔 15s」只等得下 1~2 次 ⇒ 等不到就放弃）。
                nap = min(15.0, max(2.0, remaining / 5.0))
                print(f"  · {image}:{tag} 镜像尚未可用（{exc}）"
                      f" ⇒ 等待 {nap:.0f}s 后重试（等待预算剩 {remaining / 60:.1f} 分钟）",
                      file=sys.stderr)
                sleep(nap)
                continue
            hard_failures += 1
            if hard_failures >= retries:
                break
            print(f"  · 导出失败（{exc}），重试 {hard_failures}/{retries}",
                  file=sys.stderr)
            sleep(2 * hard_failures)
    print(f"✗ 导出 {image}:{tag} 失败：{last}", file=sys.stderr)
    return 1


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--platform", required=True,
                    help="如 linux/amd64")
    ap.add_argument("--out", required=True)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--wait-minutes", type=float, default=0.0,
                    help="镜像尚未发布（manifest 404）时的等待预算（分钟）。"
                         "0 = 旧行为（404 也只做有限重试）")
    ap.add_argument("--base-url", default=None,
                    help="registry 基址覆盖（仅测试用：指向本地假 registry）")
    args = ap.parse_args()

    os_name, _, arch = args.platform.partition("/")
    out = Path(args.out)

    return export_with_wait(args.image, args.tag, os_name, arch, out,
                            base=args.base_url,
                            wait_minutes=args.wait_minutes,
                            retries=args.retries)


if __name__ == "__main__":
    sys.exit(main())
