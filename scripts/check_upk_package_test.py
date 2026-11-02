#!/usr/bin/env python3
"""UPK 校验器的**变异自测**：证明每条规则都真的能判红。

为什么必须做：判据「看起来对」不等于「有区分力」。只有证明
「把包改坏 ⇒ 校验器必红」，才能确定这道门禁不是摆设。

用法：python3 scripts/check_upk_package_test.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PKG = REPO / "upk"
CHECKER = HERE / "check_upk_package.py"
sys.path.insert(0, str(HERE))

from upk_common import (  # noqa: E402
    compose_images,
    image_name,
    image_tag,
    make_image_tar,
)


def run_checker(pkg_dir: Path, strict: bool = False) -> tuple[int, str]:
    cmd = [sys.executable, str(CHECKER)]
    if strict:
        cmd.append("--strict")
    cmd.append(str(pkg_dir))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout + p.stderr)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def write(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")


def stage_tars(pkg: Path, repo_tags: dict[str, list[str]] | None = None,
               arch_of: dict[str, str] | None = None) -> None:
    """在包里摆出「官方口径」的镜像 tar。

    RepoTags 取 compose 里该服务的 image 引用（官方就是按它匹配 compose），
    架构与所在 rootfs_<arch> 一致 ⇒ 摆完之后 R3/R12/R13 都应该是绿的。
    """
    ct = read(pkg / "rootfs_common" / "docker-compose.yaml")
    for svc, ref in compose_images(ct):
        name = image_name(ref)
        tag = image_tag(ref)
        tags = (repo_tags or {}).get(svc, [ref])
        for arch in ("amd64", "arm64"):
            d = pkg / f"rootfs_{arch}" / "images"
            d.mkdir(parents=True, exist_ok=True)
            a = (arch_of or {}).get(arch, arch)
            make_image_tar(d / f"pigeonbox-{name}-{tag}.tar", tags, a)


# ── 变异用例：(名称, 怎么改包, 期望输出里出现的关键词) ──────────────────
def m_ghcr_image(pkg: Path) -> None:
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f).replace("image: docker.io/pigeonbox/server:",
                        "image: ghcr.io/pigeonbox/server:")
    write(f, s)


def m_quay_image(pkg: Path) -> None:
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f).replace("image: docker.io/pigeonbox/frontend:",
                        "image: quay.io/pigeonbox/frontend:")
    write(f, s)


def m_latest_tag(pkg: Path) -> None:
    """把镜像 tag 改成 :latest。

    ⚠️ 用**正则**匹配任意版本号（版本无关）——写死版本号的话，
    列车 bump 后 replace 什么也没替换 ⇒ 变异没生效 ⇒ 判据「测不出」
    ⇒ 门禁变摆设（且静默）。2026-10-31 在 terramaster 仓踩过这个坑。
    """
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f)
    s2 = re.sub(r"(image: docker\.io/pigeonbox/server:)[^\s]+", r"\1latest", s)
    if s2 == s:
        raise AssertionError("m_latest_tag 变异未生效：compose 里没匹配到版本号")
    write(f, s2)


def m_version_drift(pkg: Path) -> None:
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f)
    s2 = re.sub(r"(image: docker\.io/pigeonbox/server:)[^\s]+", r"\g<1>0.0.1", s)
    if s2 == s:
        raise AssertionError("m_version_drift 变异未生效：compose 里没匹配到版本号")
    write(f, s2)


def m_undeclared_var(pkg: Path) -> None:
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f).replace("- PB_ADMIN_PASSWORD=${PB_ADMIN_PASSWORD}",
                        "- PB_ADMIN_PASSWORD=${PB_ADMIN_PASSWORD}\n      - PB_SOMETHING=${PB_SOMETHING}")
    write(f, s)


def m_privileged(pkg: Path) -> None:
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f).replace("    container_name: pigeonbox-server",
                        "    container_name: pigeonbox-server\n    privileged: true")
    write(f, s)


def m_host_network(pkg: Path) -> None:
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f).replace("    container_name: pigeonbox-frontend",
                        "    container_name: pigeonbox-frontend\n    network_mode: host")
    write(f, s)


def m_missing_appid(pkg: Path) -> None:
    f = pkg / "project.yaml"
    s = read(f)
    s = re.sub(r"^app_id:.*$", "", s, flags=re.M)
    write(f, s)


def m_bad_port(pkg: Path) -> None:
    f = pkg / "project.yaml"
    s = read(f).replace("port: 12345", "port: 80")
    write(f, s)


def m_missing_tar(pkg: Path) -> None:
    """compose 引用的镜像在 images/ 下**没有对应**的 tar。

    ⚠️ 变异形态要点（2026-10-31 调整）：
    造一个**版本号不同**的 tar 现在判**黄灯**（旧版残留 ⇒ 还没导出本车），
    所以这条红灯判据要用「**命名完全不对**」的 tar（如 wrong-name.tar）——
    那才是「导出流程与 compose 漂移」的真形态。
    （「版本不同 ⇒ 黄灯」由另一条反向判据覆盖。）
    """
    for arch in ("rootfs_amd64", "rootfs_arm64"):
        d = pkg / arch / "images"
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.tar"):
            old.unlink()
        write(d / "wrong-name.tar", "dummy")


def m_all_tars_missing(pkg: Path) -> None:
    """两个架构的 images/ 都清空 ⇒ 应判**黄灯**（不是红灯）。

    ⚠️ 这条是**反向**判据：tar 不入 git ⇒ 仓库 checkout 里必然为空
    ⇒ 若把它判成红灯，CI 永远红 ⇒ 门禁变成噪声 ⇒ 没人看 ⇒ 等于没有门禁。
    """
    for arch in ("rootfs_amd64", "rootfs_arm64"):
        d = pkg / arch / "images"
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.tar"):
            old.unlink()


def m_stale_tars_only(pkg: Path) -> None:
    """images/ 里只有**旧版**的 tar（缺本车）⇒ 应判**黄灯**而非红灯。

    ⚠️ 为什么这条重要：镜像 tar **不入 git** ⇒ 本地跑过导出脚本后目录里
    会留**上一版**的 tar，而 compose 已随列车 bump 到本车 ⇒ 「缺本车 tar」
    是**还没导出本车**（黄灯），不是「导出流程与 compose 漂移」（红灯）。
    ⇒ 若判成红灯，列车 bump 后本地必然红 ⇒ 门禁变噪声。
    ⚠️ 旧版 tar 必须是**合法的镜像 tar**（带 RepoTags）⇒ 否则会撞上
    「不是 docker-save 同构的镜像 tar」那条红灯 ⇒ 形态串味。
    """
    ct = read(pkg / "rootfs_common" / "docker-compose.yaml")
    for arch in ("rootfs_amd64", "rootfs_arm64"):
        d = pkg / arch / "images"
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.tar"):
            old.unlink()
        for svc, ref in compose_images(ct):
            name = image_name(ref)
            old_tag = "0.0.1"
            make_image_tar(d / f"pigeonbox-{name}-{old_tag}.tar",
                           [f"docker.io/pigeonbox/{name}:{old_tag}"],
                           arch.replace("rootfs_", ""))


# ── 2026-11-02 新增：官方 ugcli 口径的规则 ──────────────────────────────
def m_extra_common_file(pkg: Path) -> None:
    """rootfs_common 里多一个官方不允许的文件（官方只允许 icon.png + compose）。"""
    write(pkg / "rootfs_common" / "icon.svg", "<svg/>")


def m_non_tar_in_images(pkg: Path) -> None:
    """images/ 里混入非 tar 文件（官方只允许 tar）。"""
    for arch in ("rootfs_amd64", "rootfs_arm64"):
        d = pkg / arch / "images"
        d.mkdir(parents=True, exist_ok=True)
        write(d / ".gitkeep", "")


def m_param_desc_form(pkg: Path) -> None:
    """parameters 用 desc: 而不是 i18n:（官方要求 i18n + 每语言 name/description）。"""
    f = pkg / "project.yaml"
    s = read(f)
    s2 = re.sub(r"(?m)^(\s*)i18n:\s*$", r"\1desc:", s)
    if s2 == s:
        raise AssertionError("m_param_desc_form 变异未生效：project.yaml 里没有 i18n:")
    write(f, s2)


def m_param_default_value(pkg: Path) -> None:
    """parameters 带 default_value（官方 ParamItem 无此字段 ⇒ 严格解码拒绝）。"""
    f = pkg / "project.yaml"
    s = read(f)
    s2 = re.sub(r"(?m)^(\s*)required:\s*true\s*$", r"\1required: true\n\1default_value: /data", s, count=1)
    if s2 == s:
        raise AssertionError("m_param_default_value 变异未生效：没有 required: true")
    write(f, s2)


def m_missing_depend(pkg: Path) -> None:
    """缺 depend_fw_version（官方 docker 应用必填）。"""
    f = pkg / "project.yaml"
    s = read(f)
    s2 = re.sub(r"(?m)^depend_fw_version:.*$\n?", "", s)
    if s2 == s:
        raise AssertionError("m_missing_depend 变异未生效：没有 depend_fw_version")
    write(f, s2)


def m_compose_default_value(pkg: Path) -> None:
    """compose 变量写成 ${VAR:-默认值}（官方 pack 报 variables not set）。"""
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f)
    s2 = s.replace("PB_ADMIN_PASSWORD=${PB_ADMIN_PASSWORD}",
                   "PB_ADMIN_PASSWORD=${PB_ADMIN_PASSWORD:-}")
    if s2 == s:
        raise AssertionError("m_compose_default_value 变异未生效：没匹配到变量写法")
    write(f, s2)


def m_repo_tag_mismatch(pkg: Path) -> None:
    """内嵌 tar 的 RepoTags 与 compose 的引用不一致（官方按 RepoTags 匹配）。"""
    ct = read(pkg / "rootfs_common" / "docker-compose.yaml")
    tags = {svc: [f"ghcr.io/pigeonbox/{image_name(ref)}:{image_tag(ref)}"]
            for svc, ref in compose_images(ct)}
    stage_tars(pkg, repo_tags=tags)


def m_arch_mismatch(pkg: Path) -> None:
    """内嵌 tar 的架构与所在 rootfs_<arch> 不符。"""
    stage_tars(pkg, arch_of={"amd64": "arm64", "arm64": "amd64"})


def m_extra_tar(pkg: Path) -> None:
    """images/ 里多一个 compose **不引用**的 tar（旧版残留）。

    ⚠️ 官方 ugcli **逐个校验**每个 tar ⇒ 多余的 tar ⇒
    `image '...' not referenced in docker-compose.yaml` ⇒ 打包直接失败
    ⇒ 组装好的包（严格模式）必须判红。
    """
    ct = read(pkg / "rootfs_common" / "docker-compose.yaml")
    for svc, ref in compose_images(ct):
        name = image_name(ref)
        make_image_tar(pkg / "rootfs_amd64" / "images" / f"pigeonbox-{name}-0.0.1.tar",
                       [f"docker.io/pigeonbox/{name}:0.0.1"], "amd64")
        break


def m_missing_arch_dir(pkg: Path) -> None:
    """组装好的包里缺 rootfs_<arch>/（官方 ⇒ 打包报 not exists）。"""
    shutil.rmtree(pkg / "rootfs_arm64")


CASES = [
    ("镜像源是 ghcr.io（应用中心拒收）", m_ghcr_image, "Docker Hub", False),
    ("镜像源是 quay.io（应用中心拒收）", m_quay_image, "Docker Hub", False),
    ("镜像用 :latest", m_latest_tag, "latest", False),
    ("镜像 tag 与 VERSION 漂移", m_version_drift, "VERSION", False),
    ("compose 用未声明变量", m_undeclared_var, "未在 project.yaml", False),
    ("privileged: true", m_privileged, "privileged", False),
    ("network_mode: host", m_host_network, "network_mode", False),
    ("project.yaml 缺 app_id", m_missing_appid, "app_id", False),
    ("project.yaml 的 port 越界", m_bad_port, "port", False),
    ("内嵌 tar 对不上 compose", m_missing_tar, "缺", False),
    ("rootfs_common 多官方不允许的文件", m_extra_common_file, "rootfs_common", False),
    ("images/ 里混入非 tar", m_non_tar_in_images, "tar", False),
    ("参数用 desc: 而非 i18n", m_param_desc_form, "i18n", False),
    ("参数带 default_value", m_param_default_value, "default_value", False),
    ("缺 depend_fw_version", m_missing_depend, "depend_fw_version", False),
    ("compose 变量写成 ${VAR:-默认值}", m_compose_default_value, "裸", False),
    ("内嵌 tar 的 RepoTags 与 compose 不一致", m_repo_tag_mismatch, "RepoTags", False),
    ("内嵌 tar 架构与目录不符", m_arch_mismatch, "架构", False),
    ("组装包里混入 compose 不引用的 tar", m_extra_tar, "not referenced", True),
    ("组装包里缺 rootfs_<arch>/", m_missing_arch_dir, "not exists", True),
]


def main() -> int:
    # 基线：当前包必须绿（黄灯允许）
    code, out = run_checker(PKG)
    if code != 0:
        print("✗ 基线就红（当前 upk/ 不合规），先修包：")
        print(out)
        return 1
    print("✓ 基线：当前 upk/ 规则全绿")

    failures = []
    for name, mutate, keyword, strict in CASES:
        tmp = tempfile.mkdtemp(prefix="upk-mut-")
        pkg = Path(tmp) / "upk"
        shutil.copytree(PKG, pkg)
        try:
            # 先摆出**官方口径**的镜像 tar ⇒ R3/R12/R13 这些规则处在「该绿」的
            # 状态，变异才能证明「是这条违规让它转红」。
            stage_tars(pkg)
            mutate(pkg)
            code, out = run_checker(pkg, strict=strict)
            if code == 0:
                failures.append(f"{name}: 变异后仍报绿（该规则判据无效）")
                print(f"  ✗ {name}: 变异后仍报绿")
            elif keyword not in out:
                failures.append(f"{name}: 报红了但没提到 {keyword!r}")
                print(f"  ✗ {name}: 报红了但没提到 {keyword!r}")
            else:
                print(f"  ✓ {name}: 判红且提到 {keyword!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n✗ {len(failures)} 条规则判据无效：")
        for f in failures:
            print("   -", f)
        return 1

    # ── 反向判据：tar 全空 / 只有旧版残留 ⇒ **黄灯**而非红灯 ──────────
    # ⚠️ tar 不入 git ⇒ 仓库 checkout 里必然为空；本地跑过导出脚本后会留
    #    旧版残留 ⇒ 这两种情况都必须判黄灯，否则列车 bump 后本地必然红
    #    ⇒ 门禁变噪声 ⇒ 没人看。
    for label, mut in (("tar 全空", m_all_tars_missing),
                        ("只有旧版残留", m_stale_tars_only)):
        tmp = tempfile.mkdtemp(prefix="upk-yellow-")
        pkg = Path(tmp) / "upk"
        shutil.copytree(PKG, pkg)
        try:
            stage_tars(pkg)
            mut(pkg)
            code, out = run_checker(pkg)
            if code != 0:
                print(f"  ✗ {label}被判红灯（应判黄灯：tar 不入 git ⇒ 仓库/残留场景必然发生）")
                failures.append(f"{label}被判红灯")
            elif "为空" not in out and "没有本车" not in out and "旧版残留" not in out:
                print(f"  ✗ {label}未给黄灯提示")
                failures.append(f"{label}未给黄灯提示")
            else:
                print(f"  ✓ {label}: 判黄灯且给出提示（不阻断 CI）")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n✗ {len(failures)} 条判据无效：")
        for f in failures:
            print("   -", f)
        return 1
    print(f"\n✓ {len(CASES)} 条规则的判据全部有效（每条都能判红）+ 反向判据通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
