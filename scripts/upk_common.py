#!/usr/bin/env python3
"""UPK 相关的共享小工具：compose 解析 + 镜像 tar 的读/写。

**为什么独立成文件**：同一件事在四处都要做，且必须**逐字一致**：
  · `export_upk_images.py` —— 导出镜像 tar 时，**按 compose 的 image 引用**
    给 tar 打 RepoTags 标签（官方 ugcli 按 RepoTags 匹配 compose，见下）；
  · `check_upk_package.py` —— 校验时读 tar 的 RepoTags / 架构；
  · `check_upk_package_test.py` —— 变异自测时造合成 tar；
  · `check_upk_official.sh` —— 官方门禁用合成 tar 驱动官方 ugcli。
⇒ 放一处，避免各写一份、各漂一份（跨仓同源脚本踩过的坑）。

## 官方 ugcli 的镜像匹配口径（2026-11-02 实测，v1.1.0.13）

官方**不按文件名找镜像**，而是按 **tar 内 manifest.json 的 RepoTags** 去匹配
compose 的 `image:` 引用：
  · RepoTags 与 compose 的引用**逐字不一致** ⇒ 报
    `rootfs_<arch>/images: missing image tar for service '<svc>' (image: <compose 里的引用>)`；
  · tar 的 config 架构与所在 `rootfs_<arch>` 不符 ⇒ 报
    `image architecture mismatch (expected: amd64, actual: arm64)`。
⇒ 所以「compose 写什么，tar 就必须打什么标签」是**硬约束**，不是风格问题。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path

# ── compose 解析（零依赖，按行解析，与本仓其它脚本同风格）───────────────
def compose_images(compose_text: str) -> list[tuple[str, str]]:
    """解析 compose，返回 [(服务名, image 引用)]（按出现顺序）。

    只认 `services:` 块下的 `image:` 行；顶层其它块（networks/volumes）
    的同级键会被 `indent <= services 缩进` 截断掉。
    """
    out: list[tuple[str, str]] = []
    in_services = False
    services_indent = 0
    svc = ""
    for raw in compose_text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        s = raw.strip()
        if not in_services:
            if s.rstrip(":") == "services":
                in_services = True
                services_indent = indent
            continue
        if indent <= services_indent:
            break
        m = re.match(r"^([\w.\-]+)\s*:\s*$", s)
        if m and indent == services_indent + 2:
            svc = m.group(1)
            continue
        m = re.match(r"^image\s*:\s*(\S+)", s)
        if m:
            out.append((svc, m.group(1).strip("\"'")))
    return out


def image_name(ref: str) -> str:
    """镜像引用的**名字段**（`docker.io/pigeonbox/server:4.7.1` ⇒ `server`）。"""
    tail = ref.rsplit("/", 1)[-1]
    return tail.split(":", 1)[0]


def image_tag(ref: str) -> str:
    """镜像引用的 **tag 段**（没有 tag ⇒ 空串）。"""
    tail = ref.rsplit("/", 1)[-1]
    return tail.split(":", 1)[1] if ":" in tail else ""


def split_ref(ref: str) -> tuple[str, str]:
    """拆镜像引用 → (不含 tag 的仓库引用, tag)。

    `docker.io/pigeonbox/server:4.7.1` ⇒
    (`docker.io/pigeonbox/server`, `4.7.1`)

    ⚠️ **为什么必须拆**（2026-11-02 实测踩过）：registry 的**仓库名**里不能带
    tag —— `scope=repository:pigeonbox/server:4.7.1:pull` 是**非法 scope**
    ⇒ 取 token 直接 **HTTP 403** ⇒ 导出全失败。
    ⇒ 拉取源用「不含 tag 的仓库引用」，tag 单独走 `--tag`。
    """
    head, sep, tail = ref.rpartition("/")
    name, tag_sep, tag = tail.partition(":")
    if not sep:
        return name, tag
    return f"{head}/{name}", tag


def swap_registry(ref: str, registry: str) -> str:
    """把镜像引用的 registry 段换成 `registry`（仓库名/名/tag 原样保留）。

    `docker.io/pigeonbox/server:4.7.1` + `ghcr.io` ⇒
    `ghcr.io/pigeonbox/server:4.7.1`

    ⚠️ **必须先把 tag 摘掉再换 registry**（2026-11-02 实测踩过）：
    直接按 "/" 切会把 `server:4.7.1` 整段当成**仓库名的一部分** ⇒
    拉取源变成 `ghcr.io/pigeonbox/server:4.7.1` ⇒ registry 认为仓库叫
    `pigeonbox/server:4.7.1` ⇒ 取 token 时 scope 非法 ⇒ **HTTP 403**。
    ⚠️ 首段是**命名空间**（无点号）时不是 registry ⇒ 换 registry 要**保留**
    命名空间并前插新 registry（与 export_image_tar.split_image 同一口径）。
    """
    head, sep, tail = ref.rpartition("/")
    name, tag_sep, tag = tail.partition(":")
    tag_part = f"{tag_sep}{tag}"
    if not sep:
        return f"{registry}/{name}{tag_part}"
    parts = head.split("/") + [name]
    if "." in parts[0] or parts[0] == "localhost":
        parts[0] = registry
    else:
        parts.insert(0, registry)
    return "/".join(parts) + tag_part


# ── 镜像 tar（docker-save 同构）────────────────────────────────────────
def make_image_tar(path: Path, repo_tags: list[str], arch: str,
                   os_name: str = "linux") -> None:
    """造一个 docker-save 同构的镜像 tar（manifest.json + config + layer）。

    官方 ugcli 只读 manifest.json 的 RepoTags 与 config 的 architecture
    ⇒ 合成 tar 足以驱动官方门禁（不必拉真镜像）。
    """
    layer = b"pigeonbox-upk-fixture-layer"
    lhash = hashlib.sha256(layer).hexdigest()
    config = json.dumps({
        "architecture": arch,
        "os": os_name,
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + lhash]},
        "config": {},
    }).encode("utf-8")
    chash = hashlib.sha256(config).hexdigest()
    manifest = json.dumps([{
        "Config": chash + ".json",
        "RepoTags": list(repo_tags),
        "Layers": [lhash + "/layer.tar"],
    }]).encode("utf-8")
    repositories = json.dumps({
        r.split(":", 1)[0]: {r.split(":", 1)[1]: "sha256:" + chash}
        for r in repo_tags if ":" in r
    }).encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as t:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            t.addfile(info, io.BytesIO(data))
        add("manifest.json", manifest)
        add(chash + ".json", config)
        add(lhash + "/layer.tar", layer)
        add("repositories", repositories)


def tar_manifest(path: Path) -> list[dict]:
    """读 tar 内 manifest.json 的条目列表；读不出来 ⇒ 空列表。"""
    try:
        with tarfile.open(path, "r") as t:
            f = t.extractfile("manifest.json")
            if f is None:
                return []
            return json.loads(f.read().decode("utf-8"))
    except Exception:
        return []


def tar_repo_tags(path: Path) -> list[str]:
    """tar 内 manifest.json 的全部 RepoTags（扁平列表）。"""
    tags: list[str] = []
    for entry in tar_manifest(path):
        tags.extend(entry.get("RepoTags") or [])
    return tags


def tar_architecture(path: Path) -> str | None:
    """tar 内镜像 config 的 architecture；读不出来 ⇒ None。"""
    try:
        with tarfile.open(path, "r") as t:
            entries = tar_manifest(path)
            if not entries:
                return None
            cfg = entries[0].get("Config")
            if not cfg:
                return None
            f = t.extractfile(cfg)
            if f is None:
                return None
            return json.loads(f.read().decode("utf-8")).get("architecture")
    except Exception:
        return None


# ── 合成 UPK 包（官方门禁 / 校验器变异自测共用）─────────────────────────
def stage_images(pkg_dir: Path, src_root: Path, version: str) -> int:
    """把**本车**的镜像 tar 拷进组装目录（只拷 compose 引用的那些）。

    ⚠️ 为什么必须「只拷本车」（2026-11-02 官方 ugcli 实测）：
      官方**逐个校验** images/ 里的每个 tar ⇒ 旧版残留的 tar 也会被拒：
      `image 'ghcr.io/pigeonbox/frontend:4.6.4' not referenced in
      docker-compose.yaml` ⇒ **打包直接失败**。
      ⇒ 组装时**只**放 compose 引用、且版本等于本车的 tar。
      ⇒ 缺任何一个 ⇒ 报错退出（不产出半成品包）。

    返回 0 = 成功；非 0 = 失败（原因已打到 stderr）。
    """
    compose_file = pkg_dir / "rootfs_common" / "docker-compose.yaml"
    if not compose_file.is_file():
        print(f"✗ 组装目录缺 rootfs_common/docker-compose.yaml：{pkg_dir}",
              file=sys.stderr)
        return 1
    pairs = compose_images(compose_file.read_text(encoding="utf-8"))
    if not pairs:
        print(f"✗ {compose_file} 里没有任何 image 引用", file=sys.stderr)
        return 1
    missing: list[str] = []
    for svc, ref in pairs:
        name = image_name(ref)
        tag = image_tag(ref)
        if tag != version:
            print(f"✗ compose 里 {svc} 的镜像 tag 是 {tag}，与本车 {version} 不一致"
                  f"（⇒ 导出的 tar 与 compose 对不上）", file=sys.stderr)
            return 1
        for arch in ("amd64", "arm64"):
            src = (src_root / f"rootfs_{arch}" / "images"
                   / f"pigeonbox-{name}-{version}.tar")
            dst_dir = pkg_dir / f"rootfs_{arch}" / "images"
            dst_dir.mkdir(parents=True, exist_ok=True)
            if not src.is_file():
                missing.append(str(src))
                continue
            shutil.copy2(src, dst_dir / src.name)
    if missing:
        print("✗ 缺本车镜像 tar：\n  " + "\n  ".join(missing), file=sys.stderr)
        return 1
    return 0


def build_synthetic_package(dest: Path, project_yaml: str, compose_text: str,
                             icon_png: Path, arches: tuple[str, ...] = ("amd64", "arm64"),
                             repo_tags: dict[str, list[str]] | None = None,
                             arch_of: dict[str, str] | None = None) -> None:
    """按 compose 的 image 引用造一个「官方能认」的 UPK 包目录。

    · `repo_tags`：{服务名: [RepoTags...]}，默认取 compose 里该服务的 image 引用；
    · `arch_of`：{架构目录名: tar 内 config 的 architecture}，默认同名。
    """
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "project.yaml").write_text(project_yaml, encoding="utf-8")
    common = dest / "rootfs_common"
    common.mkdir(parents=True, exist_ok=True)
    (common / "docker-compose.yaml").write_text(compose_text, encoding="utf-8")
    if icon_png.is_file():
        (common / "icon.png").write_bytes(icon_png.read_bytes())

    for svc, ref in compose_images(compose_text):
        name = image_name(ref)
        tag = image_tag(ref)
        tags = (repo_tags or {}).get(svc, [ref])
        for arch in arches:
            images = dest / f"rootfs_{arch}" / "images"
            images.mkdir(parents=True, exist_ok=True)
            out = images / f"pigeonbox-{name}-{tag}.tar"
            tar_arch = (arch_of or {}).get(arch, arch)
            make_image_tar(out, tags, tar_arch)
