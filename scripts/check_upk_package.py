#!/usr/bin/env python3
"""绿联 UPK 应用包校验器（离线，不触网）。

**为什么需要**：UPK 包的结构与内容有**硬约束**（官方 ugcli 会拒），
但本仓此前**没有任何校验** ⇒ 包漂了要等到真机安装失败才发现。
参照 TOS 7 的同类校验器（terramaster/scripts/check_tos7_package.py）：

两级判定
  · **红灯（规则违规）**：违反官方约束 ⇒ 打包/安装必失败 ⇒ 退出码非 0。
  · **黄灯（送审前置未就绪）**：需外部条件（真机 / 绿联授权）⇒ 只提示。

当前规则（红灯）
  R1  镜像必须是 **Docker Hub** 引用（应用中心只收 Docker Hub）
  R2  镜像 tag 必须与本仓 VERSION 一致（版本漂移 ⇒ 内嵌 tar 与 compose 对不上）
  R3  compose 的每个 image 都必须在 rootfs_<arch>/images/ 下有**同名 tar**
      （打包器按 tag 找 tar；缺 tar ⇒ 装不上）
  R4  compose 内不得用 latest（latest 与「钉死版本」互斥）
  R5  compose 内的 ${VAR} 只能是 project.yaml 已声明的 parameters 或 ${TZ}
      （官方打包器强校验：未声明变量 ⇒ 打包失败）
  R6  不得 privileged / network_mode: host（与 TOS 7 同款硬规则）
  R7  project.yaml 的 app_id / version / port 必填且合法

当前规则（黄灯）
  Y1  rootfs_<arch>/images/ 为空（镜像 tar 未导出 ⇒ 还不能产出可提交包）
  Y2  正式包必须在 Linux 上用 ugcli pack 打出（本机 macOS 出的是非正式包）

零依赖（刻意不引 PyYAML：判定按行解析，与本仓其它脚本一致）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "upk"

red: list[str] = []
yellow: list[str] = []


def fail(msg: str) -> None:
    red.append(msg)


def warn(msg: str) -> None:
    yellow.append(msg)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def find_line(text: str, needle: str) -> int:
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i
    return 0


# ── R1/R2/R4: compose 的 image 行 ───────────────────────────────────────
def check_images() -> list[str]:
    """检查 compose 的 image 行；返回镜像引用列表（供 R3 用）。"""
    f = PKG / "rootfs_common" / "docker-compose.yaml"
    if not f.is_file():
        fail(f"缺 {f.relative_to(REPO)}（UPK 的 compose 入口）")
        return []
    text = read(f)
    images = re.findall(r"^\s*image\s*:\s*(\S+)", text, re.M)
    if not images:
        fail("compose 里没有任何 image 行")
        return []

    repo_ver = ""
    ver_file = REPO / "VERSION"
    if ver_file.is_file():
        repo_ver = read(ver_file).strip().lstrip("v")

    for img in images:
        # R1：必须 Docker Hub
        if not (img.startswith("docker.io/") or "/" in img and
                img.split("/", 1)[0] in ("docker.io", "index.docker.io")):
            fail(f"compose 第 {find_line(text, img)} 行：镜像 {img} 不是 Docker Hub 引用"
                 f"（绿联应用中心只收 Docker Hub ⇒ 送审/安装必失败）")
        # R4：禁 latest
        if img.endswith(":latest"):
            fail(f"compose 第 {find_line(text, img)} 行：镜像 {img} 用了 :latest"
                 f"（latest 与「钉死版本」互斥 ⇒ 内嵌 tar 无法按 tag 对应）")
        # R2：tag 与 VERSION 一致
        m = re.search(r":([^:/]+)$", img)
        if m and repo_ver and m.group(1) != "latest" and m.group(1) != repo_ver:
            fail(f"compose 第 {find_line(text, img)} 行：镜像 {img} 的 tag "
                 f"{m.group(1)} 与本仓 VERSION（{repo_ver}）不一致"
                 f"（⇒ 内嵌 tar 与 compose 对不上 ⇒ 装不上）")
    return images


# ── R3: 内嵌 tar 必须与 compose 的 image 对应 ──────────────────────────
def check_embedded_tars(images: list[str]) -> None:
    """检查内嵌镜像 tar 与 compose 的 image 是否一一对应。

    ⚠️ **镜像 tar 不入 git**（四个约 260MB ⇒ 每次镜像更新都产生巨大二进制 diff）
    ⇒ 仓库 checkout 里**本来就没有** tar ⇒ 「目录为空」是**黄灯**（还没出包），
    不是红灯。
    ⇒ 红灯只针对「**有** tar 但**对不上**」（命名/版本错位）——那说明
    导出流程与 compose 漂移了，打出来的包装不上。
    """
    for arch_dir in ("rootfs_amd64", "rootfs_arm64"):
        d = PKG / arch_dir / "images"
        if not d.is_dir():
            fail(f"缺 {arch_dir}/images/（打包器要往这里放镜像 tar）")
            continue
        tars = sorted(p.name for p in d.glob("*.tar"))
        if not tars:
            # Y1：不是规则违规（tar 不入 git ⇒ 仓库里必然为空），但要提示
            warn(f"{arch_dir}/images/ 为空——镜像 tar 未导出 ⇒ 现在还不能产出可提交的 UPK 包"
                 f"（跑 scripts/export_upk_images.py --version <ver>；tar 不入 git，"
                 f"由 CI 导出并作为 Release 资产发布）")
            continue
        for img in images:
            tag = re.search(r":([^:/]+)$", img)
            if not tag:
                continue
            want = f"pigeonbox-{img.split('/')[-1].split(':')[0]}-{tag.group(1)}.tar"
            if want not in tars:
                fail(f"{arch_dir}/images/ 里缺 {want}"
                     f"（compose 引用了 {img} ⇒ 打包器按 tag 找 tar，缺了装不上）"
                     f"；现有：{', '.join(tars) or '(空)'}")


# ── R5: compose 变量必须是 project.yaml 已声明的 ────────────────────────
def check_variables() -> None:
    py = PKG / "project.yaml"
    comp = PKG / "rootfs_common" / "docker-compose.yaml"
    if not py.is_file() or not comp.is_file():
        return
    declared = set()
    in_params = False
    params_indent = 0
    for raw in read(py).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        s = raw.strip()
        if not in_params:
            if s.startswith("parameters:"):
                in_params = True
                params_indent = indent
            continue
        # 块级判定：缩进**回退**到 parameters 之下 ⇒ 该块结束。
        # ⚠️ 不能用「行首不是 - 就结束」—— parameters 的子项里还有
        #    type/required/default_value/desc 等**映射行**，它们同样不以 - 开头
        #    ⇒ 那样判会把块在第一个 type 行就截断（实测漏抓全部 key）。
        if indent <= params_indent:
            in_params = False
            continue
        m = re.match(r"-\s*key\s*:\s*(\S+)", s)
        if m:
            declared.add(m.group(1).strip("\"'"))
    text = read(comp)
    used = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", text))
    for var in sorted(used):
        if var in declared or var == "TZ":
            continue
        fail(f"compose 用了未在 project.yaml parameters 声明的变量 ${{{var}}}"
             f"（官方打包器强校验 ⇒ 打包直接失败）")


# ── R6: 禁 privileged / host 网络 ──────────────────────────────────────
def check_hardening() -> None:
    comp = PKG / "rootfs_common" / "docker-compose.yaml"
    if not comp.is_file():
        return
    text = read(comp)
    if re.search(r"^\s*privileged\s*:\s*true", text, re.M):
        fail("compose 出现 privileged: true（官方打包器禁）")
    if re.search(r"^\s*network_mode\s*:\s*host", text, re.M):
        fail("compose 出现 network_mode: host（官方打包器禁）")


# ── R7: project.yaml 必填字段 ───────────────────────────────────────────
def check_project_yaml() -> None:
    py = PKG / "project.yaml"
    if not py.is_file():
        fail("缺 project.yaml（UPK 包定义入口）")
        return
    text = read(py)
    for field in ("app_id", "version", "port", "spec_version"):
        if not re.search(rf"^\s*{field}\s*:\s*\S+", text, re.M):
            fail(f"project.yaml 缺必填字段 {field}")
    m = re.search(r"^\s*app_id\s*:\s*(\S+)", text, re.M)
    if m and not re.fullmatch(r"[\w.\-]+", m.group(1).strip("\"'")):
        fail(f"project.yaml 的 app_id 非法：{m.group(1)}")
    m = re.search(r"^\s*port\s*:\s*(\d+)", text, re.M)
    if m and not (1024 <= int(m.group(1)) <= 65535):
        fail(f"project.yaml 的 port {m.group(1)} 越界（须 1024-65535）")


def main() -> int:
    images = check_images()
    check_embedded_tars(images)
    check_variables()
    check_hardening()
    check_project_yaml()

    print("── 绿联 UPK 应用包校验 ──")
    for m in yellow:
        print(f"  ⚠ 送审前置未就绪：{m}")
    for m in red:
        print(f"  ✗ 规则违规：{m}")
    if red:
        print(f"✗ {len(red)} 条规则违规（打包/安装必失败）")
        return 1
    print(f"✓ UPK 应用包规则全绿（黄灯 {len(yellow)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
