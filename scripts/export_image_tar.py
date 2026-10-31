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
    pass


def http(url: str, headers: dict[str, str] | None = None, binary: bool = False):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    return data if binary else data.decode("utf-8")


def token_for(registry: str, repo: str) -> str:
    """取 registry 的匿名 pull token（GHCR 允许匿名拉公开包）。"""
    url = (f"https://{registry}/token?scope=repository:{repo}:pull"
           f"&service={registry}")
    try:
        return json.loads(http(url))["token"]
    except Exception as exc:
        raise RegistryError(f"取 {registry} 的 pull token 失败：{exc}") from exc


def get_manifest(registry: str, repo: str, ref: str, tok: str) -> dict:
    url = f"https://{registry}/v2/{repo}/manifests/{ref}"
    hdrs = {
        "Authorization": f"Bearer {tok}",
        "Accept": MANIFEST_TYPES,
    }
    try:
        raw = http(url, hdrs)
    except urllib.error.HTTPError as exc:
        raise RegistryError(
            f"取 manifest 失败（HTTP {exc.code}）：{url}") from exc
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


def blob(registry: str, repo: str, digest: str, tok: str) -> bytes:
    url = f"https://{registry}/v2/{repo}/blobs/{digest}"
    hdrs = {"Authorization": f"Bearer {tok}"}
    try:
        return http(url, hdrs, binary=True)
    except urllib.error.HTTPError as exc:
        raise RegistryError(f"取 blob {digest[:19]} 失败（HTTP {exc.code}）") from exc


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
           out: Path) -> None:
    registry, repo = split_image(image)
    tok = token_for(registry, repo)
    top = get_manifest(registry, repo, tag, tok)
    if "manifests" in top:
        digest = pick_platform(top, os_name, arch)
        manifest = get_manifest(registry, repo, digest, tok)
        image_digest = digest
    else:
        manifest = top
        image_digest = "sha256:" + hashlib.sha256(
            json.dumps(top, sort_keys=True).encode()).hexdigest()

    cfg_digest = manifest["config"]["digest"]
    cfg_bytes = blob(registry, repo, cfg_digest, tok)

    layers = manifest["layers"]
    print(f"  平台 {os_name}/{arch}：{len(layers)} 层，config {cfg_digest[:19]}")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tarfile.open(tmp, "w") as tar:
        # config
        cfg_name = cfg_digest.replace(":", "_") + ".json"
        _add_bytes(tar, cfg_name, cfg_bytes)
        # layers（docker save 的命名惯例）
        layer_names = []
        for i, layer in enumerate(layers):
            d = layer["digest"]
            data = blob(registry, repo, d, tok)
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
    os.replace(tmp, out)
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"  ✓ {out}（{size_mb:.1f} MB）")


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
    args = ap.parse_args()

    os_name, _, arch = args.platform.partition("/")
    out = Path(args.out)

    last: Exception | None = None
    for i in range(args.retries):
        try:
            export(args.image, args.tag, os_name, arch, out)
            return 0
        except Exception as exc:
            last = exc
            print(f"  · 导出失败（{exc}），重试 {i + 1}/{args.retries}",
                  file=sys.stderr)
            time.sleep(2 * (i + 1))
    print(f"✗ 导出 {args.image}:{args.tag} 失败：{last}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
