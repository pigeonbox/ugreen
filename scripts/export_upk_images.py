#!/usr/bin/env python3
"""为 UPK 应用包导出全部架构的镜像 tar（rootfs_<arch>/images/）。

**为什么 UPK 要内嵌镜像 tar**：UPK 安装时若靠拉 ghcr，国内网络会失败
（且安装体验依赖外网）⇒ 内嵌 tar 后安装**不触网**、天然规避网络问题。

**为什么不用 `docker pull --platform` + `docker save`**：见
export_image_tar.py 的说明（daemon 代理脆 + 只能存当前架构）。

产物：
  upk/rootfs_amd64/images/pigeonbox-server-<ver>.tar
  upk/rootfs_amd64/images/pigeonbox-frontend-<ver>.tar
  upk/rootfs_arm64/images/...

用法：
  python3 scripts/export_upk_images.py --version 4.5.2
  python3 scripts/export_upk_images.py --version 4.5.2 --registry docker.io
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

# 镜像名 → compose 里的服务名（compose 内的 image 行必须与此处一致）。
IMAGES = [
    ("server", "server"),
    ("frontend", "frontend"),
]
ARCH_DIR = {"linux/amd64": "amd64", "linux/arm64": "arm64"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True,
                    help="镜像版本（与 compose 内 tag 一致）")
    ap.add_argument("--registry", default="ghcr.io",
                    help="镜像 registry（送审用 docker.io；本地验证用 ghcr.io）")
    ap.add_argument("--namespace", default="pigeonbox")
    ap.add_argument("--only-arch", default="",
                    help="只导某个架构（linux/amd64 或 linux/arm64）")
    args = ap.parse_args()

    arches = [args.only_arch] if args.only_arch else list(ARCH_DIR)
    failed = []
    for platform in arches:
        arch_dir = ARCH_DIR.get(platform)
        if not arch_dir:
            print(f"✗ 未知平台 {platform}", file=sys.stderr)
            return 1
        outdir = REPO / "upk" / f"rootfs_{arch_dir}" / "images"
        outdir.mkdir(parents=True, exist_ok=True)
        for svc, name in IMAGES:
            image = f"{args.registry}/{args.namespace}/{name}"
            out = outdir / f"pigeonbox-{svc}-{args.version}.tar"
            print(f"→ {image}:{args.version} ({platform}) → {out.relative_to(REPO)}")
            rc = subprocess.run(
                [sys.executable, str(HERE / "export_image_tar.py"),
                 "--image", image, "--tag", args.version,
                 "--platform", platform, "--out", str(out)],
            ).returncode
            if rc != 0:
                failed.append(f"{image}:{args.version} ({platform})")
    if failed:
        print(f"\n✗ {len(failed)} 个镜像导出失败：", file=sys.stderr)
        for f in failed:
            print(f"   - {f}", file=sys.stderr)
        return 1
    print("\n✓ 全部镜像 tar 导出完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
