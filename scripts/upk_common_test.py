#!/usr/bin/env python3
"""`upk_common.py` 的行为自测：共享小工具的判据必须有效。

为什么必须做：`upk_common.py` 是**四处共用**的底层（导出 / 校验 / 变异自测 /
官方门禁）⇒ 一处写错，四处一起错。

⚠️ 这条自测是被**一个真 bug** 逼出来的（2026-11-02）：
  `swap_registry()` 原实现按 "/" 直接切 ⇒ `docker.io/pigeonbox/server:4.7.1`
  被切成仓库名 `pigeonbox/server:4.7.1` ⇒ 拉取源变成
  `ghcr.io/pigeonbox/server:4.7.1` ⇒ registry 侧 scope 非法 ⇒ **HTTP 403**
  ⇒ 导出全失败。
  ⇒ 这类「切字符串」的坑**必须有判据**，否则只能靠真跑 260MB 导出才发现。

用法：python3 scripts/upk_common_test.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from upk_common import (  # noqa: E402
    compose_images,
    image_name,
    image_tag,
    make_image_tar,
    split_ref,
    stage_images,
    swap_registry,
    tar_architecture,
    tar_repo_tags,
)

COMPOSE = """services:
  pigeonbox:
    image: docker.io/pigeonbox/server:4.7.1
    environment:
      - TZ=${TZ}
  frontend:
    image: docker.io/pigeonbox/frontend:4.7.1
    ports:
      - "${PB_API_PORT}:8080"
"""


def main() -> int:
    failures: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: 实得 {got!r}，期望 {want!r}")
            print(f"  ✗ {name}: 实得 {got!r}，期望 {want!r}")
        else:
            print(f"  ✓ {name} = {got!r}")

    # ── compose 解析 ───────────────────────────────────────────────────
    check("compose_images", compose_images(COMPOSE),
          [("pigeonbox", "docker.io/pigeonbox/server:4.7.1"),
           ("frontend", "docker.io/pigeonbox/frontend:4.7.1")])
    check("image_name", image_name("docker.io/pigeonbox/server:4.7.1"), "server")
    check("image_tag", image_tag("docker.io/pigeonbox/server:4.7.1"), "4.7.1")

    # ── swap_registry：换 registry，**tag 不许混进仓库名** ─────────────
    check("split_ref(带 tag)", split_ref("docker.io/pigeonbox/server:4.7.1"),
          ("docker.io/pigeonbox/server", "4.7.1"))
    check("split_ref(无 tag)", split_ref("docker.io/pigeonbox/server"),
          ("docker.io/pigeonbox/server", ""))
    check("swap_registry(带 tag)", swap_registry("docker.io/pigeonbox/server:4.7.1", "ghcr.io"),
          "ghcr.io/pigeonbox/server:4.7.1")
    check("swap_registry(无 tag)", swap_registry("docker.io/pigeonbox/server", "ghcr.io"),
          "ghcr.io/pigeonbox/server")
    check("swap_registry(无命名空间)", swap_registry("nginx:1.25", "ghcr.io"),
          "ghcr.io/nginx:1.25")
    # ⚠️ 回归判据：换完 registry 后，**仓库名部分不得含 ':'**
    #    （含了 ⇒ registry 侧 scope 非法 ⇒ HTTP 403）
    swapped = swap_registry("docker.io/pigeonbox/server:4.7.1", "ghcr.io")
    repo_part = swapped.rsplit("/", 1)[-1].split(":", 1)[0]
    if ":" in repo_part:
        failures.append(f"swap_registry 把 tag 混进了仓库名：{swapped}")
        print(f"  ✗ swap_registry 把 tag 混进了仓库名：{swapped}")
    else:
        print(f"  ✓ swap_registry 后仓库名不含 ':'（scope 合法 ⇒ 不会 403）")
    # ⚠️ 回归判据：**拉取源**必须用「不含 tag 的仓库引用」——
    #    `--image ghcr.io/pigeonbox/server:4.7.1` ⇒ registry 仓库名变成
    #    `pigeonbox/server:4.7.1` ⇒ scope 非法 ⇒ 403（实测踩过）。
    repo_ref, ref_tag = split_ref("docker.io/pigeonbox/server:4.7.1")
    pull = swap_registry(repo_ref, "ghcr.io")
    if ":" in pull:
        failures.append(f"拉取源仍带 tag：{pull}（scope 会非法 ⇒ 403）")
        print(f"  ✗ 拉取源仍带 tag：{pull}")
    elif ref_tag != "4.7.1":
        failures.append(f"split_ref 的 tag 不对：{ref_tag}")
        print(f"  ✗ split_ref 的 tag 不对：{ref_tag}")
    else:
        print(f"  ✓ 拉取源 = {pull}（不含 tag）／tag 单独走 --tag = {ref_tag}")

    # ── 镜像 tar 往返 ──────────────────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="upk-common-") as tmp:
        p = Path(tmp) / "img.tar"
        make_image_tar(p, ["docker.io/pigeonbox/server:4.7.1"], "arm64")
        check("tar_repo_tags", tar_repo_tags(p), ["docker.io/pigeonbox/server:4.7.1"])
        check("tar_architecture", tar_architecture(p), "arm64")

    # ── stage_images：**只**放本车 tar（旧的残留会让官方打包失败）──────
    # ⚠️ 官方 ugcli 会**逐个校验** images/ 里的每个 tar ⇒ 旧版残留的 tar
    #    ⇒ `image 'ghcr.io/...:4.6.4' not referenced in docker-compose.yaml`
    #    ⇒ 打包直接失败（2026-11-02 实测）。
    with tempfile.TemporaryDirectory(prefix="upk-stage-") as tmp:
        tmp = Path(tmp)
        src = tmp / "src"
        pkg = tmp / "pkg"
        (pkg / "rootfs_common").mkdir(parents=True)
        (pkg / "rootfs_common" / "docker-compose.yaml").write_text(
            COMPOSE, encoding="utf-8")
        for arch in ("amd64", "arm64"):
            d = src / f"rootfs_{arch}" / "images"
            d.mkdir(parents=True)
            for name in ("server", "frontend"):
                make_image_tar(d / f"pigeonbox-{name}-4.7.1.tar",
                               [f"docker.io/pigeonbox/{name}:4.7.1"], arch)
            # 旧版残留（不该进包）
            make_image_tar(d / "pigeonbox-server-4.6.4.tar",
                           ["docker.io/pigeonbox/server:4.6.4"], arch)
        rc = stage_images(pkg, src, "4.7.1")
        check("stage_images 返回码", rc, 0)
        staged = sorted(p.name for p in (pkg / "rootfs_amd64" / "images").iterdir())
        if staged != ["pigeonbox-frontend-4.7.1.tar", "pigeonbox-server-4.7.1.tar"]:
            failures.append(f"stage_images 把不该进包的 tar 带进来了：{staged}")
            print(f"  ✗ stage_images 带进了旧版残留：{staged}")
        else:
            print(f"  ✓ stage_images 只放本车 tar：{staged}")
        # 缺 tar ⇒ 必须失败（不产出半成品包）
        rc2 = stage_images(pkg, src, "9.9.9")
        if rc2 == 0:
            failures.append("stage_images 缺本车 tar 时仍返回成功")
            print("  ✗ stage_images 缺 tar 时仍返回成功")
        else:
            print(f"  ✓ stage_images 缺本车 tar ⇒ 返回 {rc2}（不产出半成品包）")

    if failures:
        print(f"\n✗ {len(failures)} 条判据无效：")
        for f in failures:
            print("   -", f)
        return 1
    print("\n✓ 共享小工具的判据全部有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
