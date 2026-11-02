#!/usr/bin/env python3
"""为 UPK 应用包导出全部架构的镜像 tar（rootfs_<arch>/images/）。

**为什么 UPK 要内嵌镜像 tar**：UPK 安装时若靠拉 ghcr，国内网络会失败
（且安装体验依赖外网）⇒ 内嵌 tar 后安装**不触网**、天然规避网络问题。

**为什么不用 `docker pull --platform` + `docker save`**：见
export_image_tar.py 的说明（daemon 代理脆 + 只能存当前架构）。

**标签与拉取源分离**（2026-11-02 官方 ugcli v1.1.0.13 实测）：
  官方**按 tar 内 manifest.json 的 RepoTags 匹配 compose 的 image 引用**
  （不是按文件名）⇒ compose 写的是**送审形态**（Docker Hub 引用）时，
  tar 就必须打**同一个**标签，否则官方报
  `missing image tar for service 'pigeonbox'` ⇒ **打包失败**。
  而镜像**字节**只存在于 ghcr ⇒ 两者必须分离，本脚本统一编排：
      标签（RepoTags） = compose 里的 image 引用（逐字，一字不改）
      拉取源           = 该引用换掉 registry 段（--registry，默认 ghcr.io）
  ⇒ compose 与 tar 天然一致，不靠人记着改两处。

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
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from upk_common import (  # noqa: E402
    compose_images,
    image_name,
    image_tag,
    split_ref,
    swap_registry,
)

ARCH_DIR = {"linux/amd64": "amd64", "linux/arm64": "arm64"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True,
                    help="镜像版本（必须与 compose 里的 image tag 一致）")
    ap.add_argument("--registry", default="ghcr.io",
                    help="**拉取源** registry（镜像字节从这儿拉；默认 ghcr.io）"
                         "。标签始终取 compose 里的 image 引用，与本参数无关")
    ap.add_argument("--compose", default=None,
                    help="compose 路径（默认 upk/rootfs_common/docker-compose.yaml）")
    ap.add_argument("--only-arch", default="",
                    help="只导某个架构（linux/amd64 或 linux/arm64）")
    ap.add_argument("--wait-minutes", type=float, default=0.0,
                    help="镜像尚未发布（manifest 404）时的等待预算（分钟）")
    ap.add_argument("--base-url", default=None,
                    help="registry 基址覆盖（仅测试用）")
    args = ap.parse_args()

    compose_path = Path(args.compose) if args.compose else \
        REPO / "upk" / "rootfs_common" / "docker-compose.yaml"
    if not compose_path.is_file():
        print(f"✗ 缺 compose：{compose_path}", file=sys.stderr)
        return 1
    pairs = compose_images(compose_path.read_text(encoding="utf-8"))
    if not pairs:
        print(f"✗ {compose_path} 里没有任何 image 引用", file=sys.stderr)
        return 1

    # 版本一致性：compose 的 tag 与 --version 必须一致
    # ⇒ compose 与 tar 的标签/文件名天然同版（否则官方匹配不到 / 校验器判红）。
    for svc, ref in pairs:
        tag = image_tag(ref)
        if tag != args.version:
            print(f"✗ compose 里 {svc} 的镜像 tag 是 {tag or '(无)'}，"
                  f"与 --version {args.version} 不一致"
                  f"（⇒ 导出的 tar 与 compose 对不上 ⇒ 官方匹配不到 image）",
                  file=sys.stderr)
            return 1

    arches = [args.only_arch] if args.only_arch else list(ARCH_DIR)
    failed = []
    for platform in arches:
        arch_dir = ARCH_DIR.get(platform)
        if not arch_dir:
            print(f"✗ 未知平台 {platform}", file=sys.stderr)
            return 1
        outdir = REPO / "upk" / f"rootfs_{arch_dir}" / "images"
        outdir.mkdir(parents=True, exist_ok=True)
        for svc, ref in pairs:
            name = image_name(ref)
            # ⚠️ 拉取源**不带 tag**（registry 的仓库名里带 tag ⇒ scope 非法 ⇒ 403）；
            #    tag 单独走 --tag；标签（RepoTags）取 compose 里的引用**逐字**。
            repo_ref, ref_tag = split_ref(ref)
            pull = swap_registry(repo_ref, args.registry)
            out = outdir / f"pigeonbox-{name}-{args.version}.tar"
            print(f"→ {svc}: 拉取 {pull}:{ref_tag}（{platform}）"
                  f"／标签 {ref} → {out.relative_to(REPO)}")
            cmd = [sys.executable, str(HERE / "export_image_tar.py"),
                   "--image", pull, "--tag", ref_tag,
                   "--repo-tag", ref,
                   "--platform", platform, "--out", str(out),
                   "--wait-minutes", str(args.wait_minutes)]
            if args.base_url:
                cmd += ["--base-url", args.base_url]
            rc = subprocess.run(cmd).returncode
            if rc != 0:
                failed.append(f"{pull}:{ref_tag} ({platform})")
    if failed:
        print(f"\n✗ {len(failed)} 个镜像导出失败：", file=sys.stderr)
        for f in failed:
            print(f"   - {f}", file=sys.stderr)
        return 1
    print("\n✓ 全部镜像 tar 导出完成（标签与 compose 的 image 引用逐字一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
