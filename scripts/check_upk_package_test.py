#!/usr/bin/env python3
"""UPK 校验器的**变异自测**：证明每条规则都真的能判红。

为什么必须做：判据「看起来对」不等于「有区分力」。只有证明
「把包改坏 ⇒ 校验器必红」，才能确定这道门禁不是摆设。

用法：python3 scripts/check_upk_package_test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PKG = REPO / "upk"
CHECKER = HERE / "check_upk_package.py"


def run_checker(pkg_dir: Path) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(CHECKER), str(pkg_dir)],
                       capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout + p.stderr)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def write(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")


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
    import re
    s2 = re.sub(r"(image: docker\.io/pigeonbox/server:)[^\s]+", r"\1latest", s)
    if s2 == s:
        raise AssertionError("m_latest_tag 变异未生效：compose 里没匹配到版本号")
    write(f, s2)


def m_version_drift(pkg: Path) -> None:
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f)
    import re
    s2 = re.sub(r"(image: docker\.io/pigeonbox/server:)[^\s]+", r"\g<1>0.0.1", s)
    if s2 == s:
        raise AssertionError("m_version_drift 变异未生效：compose 里没匹配到版本号")
    write(f, s2)


def m_undeclared_var(pkg: Path) -> None:
    f = pkg / "rootfs_common" / "docker-compose.yaml"
    s = read(f).replace("- PB_ADMIN_PASSWORD=${PB_ADMIN_PASSWORD:-}",
                        "- PB_ADMIN_PASSWORD=${PB_ADMIN_PASSWORD:-}\n      - PB_SOMETHING=${PB_SOMETHING:-}")
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
    import re
    s = re.sub(r"^app_id:.*$", "", s, flags=re.M)
    write(f, s)


def m_bad_port(pkg: Path) -> None:
    f = pkg / "project.yaml"
    s = read(f).replace("port: 12345", "port: 80")
    write(f, s)


def m_missing_tar(pkg: Path) -> None:
    """compose 引用的镜像在 images/ 下**没有对应**的 tar。

    ⚠️ 变异形态要点：不能只是「造一个别的版本的 tar」——若 images/ 里本来
    就有正确命名的 tar，变异就没有造成缺失 ⇒ 判据测不出。
    ⇒ 必须**清空** images/ 后只造一个错版本的 tar。
    （另有一条独立判据：「tar 全空 ⇒ 黄灯而非红灯」，见 check_all_tars_missing_is_yellow。）
    """
    for arch in ("rootfs_amd64", "rootfs_arm64"):
        d = pkg / arch / "images"
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.tar"):
            old.unlink()
        write(d / "pigeonbox-server-0.0.1.tar", "dummy")


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


CASES = [
    ("镜像源是 ghcr.io（应用中心拒收）", m_ghcr_image, "Docker Hub"),
    ("镜像源是 quay.io（应用中心拒收）", m_quay_image, "Docker Hub"),
    ("镜像用 :latest", m_latest_tag, "latest"),
    ("镜像 tag 与 VERSION 漂移", m_version_drift, "VERSION"),
    ("compose 用未声明变量", m_undeclared_var, "未在 project.yaml"),
    ("privileged: true", m_privileged, "privileged"),
    ("network_mode: host", m_host_network, "network_mode"),
    ("project.yaml 缺 app_id", m_missing_appid, "app_id"),
    ("project.yaml 的 port 越界", m_bad_port, "port"),
    ("内嵌 tar 对不上 compose", m_missing_tar, "缺"),
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
    for name, mutate, keyword in CASES:
        tmp = tempfile.mkdtemp(prefix="upk-mut-")
        pkg = Path(tmp) / "upk"
        shutil.copytree(PKG, pkg)
        try:
            mutate(pkg)
            code, out = run_checker(pkg)
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

    # ── 反向判据：tar 全空 ⇒ **黄灯**而非红灯 ─────────────────────────
    # ⚠️ tar 不入 git ⇒ 仓库 checkout 里必然为空 ⇒ 若判成红灯，CI 永远红
    # ⇒ 门禁变噪声 ⇒ 没人看 ⇒ 等于没有门禁。
    tmp = tempfile.mkdtemp(prefix="upk-yellow-")
    pkg = Path(tmp) / "upk"
    shutil.copytree(PKG, pkg)
    try:
        m_all_tars_missing(pkg)
        code, out = run_checker(pkg)
        if code != 0:
            print("  ✗ tar 全空被判红灯（应判黄灯：tar 不入 git ⇒ 仓库里必然为空）")
            failures.append("tar 全空被判红灯")
        elif "为空" not in out:
            print("  ✗ tar 全空未给黄灯提示")
            failures.append("tar 全空未给黄灯提示")
        else:
            print("  ✓ tar 全空: 判黄灯且给出提示（不阻断 CI）")
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
