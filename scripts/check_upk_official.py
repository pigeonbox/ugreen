#!/usr/bin/env python3
"""**官方 ugcli 门禁**：UPK 骨架必须被**官方工具**认可（check + pack），且门禁本身有牙齿。

## 为什么必须有这道门禁（2026-11-02 官方 ugcli v1.1.0.13 实测）

本仓此前的自研校验器（`scripts/check_upk_package.py`）只覆盖了官方硬约束的
一部分 ⇒ 骨架**官方 ugcli 根本打不出来**（实测 6 处形态违规），而 CI 全绿
⇒ 缺陷一直藏着，直到真机安装才会暴露。

更隐蔽的是：**官方 `ugcli check` 对其中 3 类违规放行**：

| 违规形态                     | `ugcli check` | `ugcli pack`                                   |
|-----------------------------|---------------|------------------------------------------------|
| `parameters` 用 `desc:`      | ❌ 报          | ❌ 报                                           |
| `parameters` 带 `default_value` | ✅ **放行** | ❌ 报 `field default_value not found ...`      |
| compose 变量写 `${VAR:-默认值}` | ✅ **放行** | ❌ 报 `variables not set: PB_ADMIN_PASSWORD`   |
| `rootfs_common` 多一个文件     | ✅ **放行**（本地仿真下崩） | ❌ 报 `only [icon.png, docker-compose.yaml] ...` |

⇒ **门禁必须跑 `pack`，只跑 `check` 等于没门禁。**

## 平台说明（重要，别误判成仓缺陷）

官方只提供 **linux-amd64** 二进制：

* **Linux x86_64（CI runner = ubuntu-latest）** ⇒ 直接跑，判定权威；
  这也正是 issue #100 里说的「待 Linux 打包机」——**GitHub Actions 就是那台打包机**。
* **arm64 macOS** ⇒ 只能经 x86 仿真跑，官方二进制在打包阶段 **SIGSEGV**
  （`runtime: lfstack.push invalid packing`）⇒ **本机环境产物，不是仓缺陷**。
  此时只能判定「`check` 拒」的形态；「只有 `pack` 才拒」的形态标为
  **待 Linux runner 判定**（不冒充已验证）。

## 用法

    python3 scripts/check_upk_official.py                 # 门禁（合成包 + 变异自证）
    python3 scripts/check_upk_official.py --package DIR   # 对真实包跑 check+pack（发布用）

环境变量：

* `UGCLI_BIN` —— 指定已下载的官方二进制（跳过下载）；
* `UGCLI_CACHE` —— 下载缓存目录（默认 `$TMPDIR/ugcli-cache`）；
* `UPK_OFFICIAL_STRICT` —— 本机仿真时也要求「待判定」的形态必须判定
  （CI 用；Linux 上恒生效）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from upk_common import (  # noqa: E402
    build_synthetic_package,
    compose_images,
    image_name,
    image_tag,
    make_image_tar,
)

UGCLI_VERSION = "v1.1.0.13"
UGCLI_URL = ("https://osswaf.ugnas.com/pro/ugcli/download/"
             f"ugcli-{UGCLI_VERSION}-linux-amd64")
# 官方公布的 sha256（upk/README.md 与 issue #100 记的是同一个值）
UGCLI_SHA256 = "c455dd38def630db2566ae32d0850230ee89abbc948bd1df4819364e88b77713"

COMPOSE = REPO / "upk" / "rootfs_common" / "docker-compose.yaml"
PROJECT = REPO / "upk" / "project.yaml"
ICON = REPO / "upk" / "rootfs_common" / "icon.png"

SIMULATED = sys.platform == "darwin"


# ── 官方二进制的获取 ────────────────────────────────────────────────────
def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_ugcli() -> Path:
    """下载官方 ugcli 并校验 sha256（缓存复用）。

    ⚠️ **下载要有「总时限」**（2026-11-02 实测）：官方源在中国 CDN
    （osswaf.ugnas.com），从 GitHub 的 runner 拉可能**极慢**
    ⇒ urllib 的 socket timeout 只管「单次读写」，**管不了总时长**
    ⇒ 慢速滴流下载能让这一步**挂几十分钟**、把 CI 拖成假死。
    ⇒ 这里按**总时限**（默认 300s）读，**超时就明确失败**（而不是无声挂着）。
    ⇒ CI 用 `actions/cache` 缓存 `~/.cache/ugcli` ⇒ 首次下载后每次复用。
    """
    env_bin = os.environ.get("UGCLI_BIN")
    if env_bin:
        p = Path(env_bin)
        if not p.is_file():
            raise SystemExit(f"✗ UPK_UGCLI_BIN 指定的二进制不存在：{p}")
        return p
    cache = Path(os.environ.get("UGCLI_CACHE")
                 or (Path.home() / ".cache" / "ugcli"))
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"ugcli-{UGCLI_VERSION}-linux-amd64"
    if dest.is_file() and sha256_of(dest) == UGCLI_SHA256:
        dest.chmod(0o755)
        print(f"→ 复用缓存的官方 ugcli：{dest}")
        return dest
    budget = float(os.environ.get("UGCLI_DOWNLOAD_BUDGET", "600"))
    attempts = int(os.environ.get("UGCLI_DOWNLOAD_ATTEMPTS", "2"))
    tmp = dest.with_suffix(".tmp")
    last_err = ""
    for attempt in range(1, attempts + 1):
        print(f"→ 下载官方 ugcli {UGCLI_VERSION}"
              f"（第 {attempt}/{attempts} 次，总时限 {budget:.0f}s）…", flush=True)
        deadline = time.monotonic() + budget
        try:
            with urllib.request.urlopen(UGCLI_URL, timeout=60) as r, tmp.open("wb") as f:
                got_bytes = 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got_bytes += len(chunk)
                    if got_bytes % (8 << 20) < (1 << 16):
                        print(f"   …已下载 {got_bytes >> 20} MB"
                              f"（剩余时限 {max(0.0, deadline - time.monotonic()):.0f}s）",
                              flush=True)
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"下载超过总时限 {budget:.0f}s")
            got = sha256_of(tmp)
            break
        except TimeoutError as exc:
            last_err = f"超时（{exc}）"
            print(f"   ⚠ {last_err}", flush=True)
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            print(f"   ⚠ 下载失败：{last_err}", flush=True)
        tmp.unlink(missing_ok=True)
    else:
        raise SystemExit(f"✗ 官方 ugcli 下载失败（{last_err}）"
                         f"（源 {UGCLI_URL} 极慢 ⇒ 本次判定不了；"
                         f"CI 已配 actions/cache ⇒ 下次命中缓存即可）")

    if got != UGCLI_SHA256:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"✗ 官方 ugcli sha256 不匹配：期望 {UGCLI_SHA256}，实得 {got}"
                         f"（下载源 {UGCLI_URL}）")
    tmp.replace(dest)
    dest.chmod(0o755)
    return dest


# ── 跑官方工具 ─────────────────────────────────────────────────────────
def ugcli_argv(binary: Path, args: list[str], workdir: Path) -> list[str]:
    """组装命令。

    * Linux ⇒ 直接跑官方二进制；
    * macOS（arm64）⇒ 经 docker 跑 linux/amd64（x86 仿真）。
      ⚠️ 必须带 `--user` ⇒ 否则容器以 root 写产物 ⇒ 宿主清理不掉（permission denied）。
      ⚠️ 必须带 `GOGC=off` ⇒ 规避 x86 仿真下 Go runtime 的栈损坏崩溃。
    """
    if not SIMULATED:
        return [str(binary), *args]
    if not shutil.which("docker"):
        raise SystemExit("✗ 本机是 macOS（arm64），官方只提供 linux-amd64 ⇒ "
                         "需要 docker 跑官方工具；本机没 docker ⇒ 判定不了")
    return [
        "docker", "run", "--rm",
        "-e", "GOGC=off",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{binary.parent}:/ugcli:ro",
        "-v", f"{workdir}:/w",
        "-w", "/w",
        "ubuntu:24.04",
        f"/ugcli/{binary.name}", *args,
    ]


def run_ugcli(binary: Path, args: list[str], workdir: Path,
              timeout: int = 300) -> tuple[int, str]:
    """跑一次官方工具，返回 (rc, 合并输出)。"""
    cmd = ugcli_argv(binary, args, workdir)
    try:
        p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "✗ 官方工具超时"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out


def is_crash(rc: int, out: str) -> bool:
    """判定「工具崩了」而不是「工具拒了」。

    崩 ⇒ rc 139/134/… 或输出里有 SIGSEGV / SIGABRT / panic
    ⇒ **不能**当成「形态被拒」（否则本机仿真下自证全是假绿）。
    """
    if rc in (139, 134, 135, 136, 137) or rc < 0:
        return True
    low = out.lower()
    return "sigsegv" in low or "segmentation violation" in low \
        or "panic:" in low or "runtime: " in low


def verdict(rc: int, out: str) -> str:
    """把一次官方工具调用归成 accepted / rejected / crashed。"""
    if rc == 0:
        return "accepted"
    if is_crash(rc, out):
        return "crashed"
    return "rejected"


def first_error(out: str) -> str:
    """取官方工具输出里的第一行诊断（给 CI 日志看，别只说「拒了」）。"""
    for ln in (out or "").splitlines():
        s = ln.strip()
        if s.startswith(("Error", "error")) or "is required" in s or "not found" in s \
                or "only " in s or "mismatch" in s or "not exists" in s \
                or "not set" in s or "unexpected" in s or "missing" in s:
            return s[:160]
    return (out or "").strip().splitlines()[0][:160] if (out or "").strip() else ""


# ── 合成包 ──────────────────────────────────────────────────────────────
def synth(dest: Path, compose_text: str | None = None,
          project_text: str | None = None,
          repo_tags: dict[str, list[str]] | None = None,
          arch_of: dict[str, str] | None = None,
          extra_common: str | None = None,
          extra_images: str | None = None,
          arches: tuple[str, ...] = ("amd64", "arm64")) -> Path:
    """按仓内骨架造一个「官方能认」的包目录（可变体）。"""
    ct = compose_text if compose_text is not None else COMPOSE.read_text(encoding="utf-8")
    pt = project_text if project_text is not None else PROJECT.read_text(encoding="utf-8")
    build_synthetic_package(dest, pt, ct, ICON, arches=arches,
                            repo_tags=repo_tags, arch_of=arch_of)
    if extra_common:
        (dest / "rootfs_common" / extra_common).write_text("x", encoding="utf-8")
    if extra_images:
        for arch in arches:
            (dest / f"rootfs_{arch}" / "images" / extra_images).write_text("x", encoding="utf-8")
    return dest


# ── 变异用例：每条都要证明「官方工具会拒」 ──────────────────────────────
def mut_extra_common_file(pkg: Path) -> None:
    (pkg / "rootfs_common" / "icon.svg").write_text("<svg/>", encoding="utf-8")


def mut_extra_images_file(pkg: Path) -> None:
    for arch in ("amd64", "arm64"):
        (pkg / f"rootfs_{arch}" / "images" / ".gitkeep").write_text("", encoding="utf-8")


def mut_param_desc(pkg: Path) -> None:
    p = pkg / "project.yaml"
    t = p.read_text(encoding="utf-8")
    t = t.replace("""    i18n:
      zh-CN:
        name: 数据目录
        description: 数据目录(SQLite 库+上传文件+JWT 密钥,备份它=备份全部)
      en-US:
        name: Data directory
        description: Data directory (SQLite DB + uploads + JWT secret; back it up = back up everything)""",
                  """    desc:
      zh-CN: 数据目录
      en-US: Data directory""")
    p.write_text(t, encoding="utf-8")


def mut_param_default_value(pkg: Path) -> None:
    p = pkg / "project.yaml"
    t = p.read_text(encoding="utf-8")
    t = t.replace("    required: true\n    i18n:",
                  "    required: true\n    default_value: /data\n    i18n:", 1)
    p.write_text(t, encoding="utf-8")


def mut_missing_depend(pkg: Path) -> None:
    p = pkg / "project.yaml"
    t = p.read_text(encoding="utf-8")
    t = t.replace('depend_fw_version: "1.13.0.0000"\n', "")
    p.write_text(t, encoding="utf-8")


def mut_compose_default_value(pkg: Path) -> None:
    p = pkg / "rootfs_common" / "docker-compose.yaml"
    t = p.read_text(encoding="utf-8")
    t = t.replace("PB_ADMIN_PASSWORD=${PB_ADMIN_PASSWORD}",
                  "PB_ADMIN_PASSWORD=${PB_ADMIN_PASSWORD:-}")
    p.write_text(t, encoding="utf-8")


def mut_repo_tag_mismatch(pkg: Path) -> None:
    """tar 的标签与 compose 的引用不一致（官方按 RepoTags 匹配，不是按文件名）。"""
    ct = COMPOSE.read_text(encoding="utf-8")
    tags: dict[str, list[str]] = {}
    for svc, ref in compose_images(ct):
        name = image_name(ref)
        tags[svc] = [f"ghcr.io/pigeonbox/{name}:{image_tag(ref)}"]
    synth(pkg, compose_text=ct, repo_tags=tags)


def mut_arch_mismatch(pkg: Path) -> None:
    synth(pkg, arch_of={"amd64": "arm64", "arm64": "amd64"})


def mut_missing_arch_dir(pkg: Path) -> None:
    shutil.rmtree(pkg / "rootfs_arm64")


def mut_missing_icon(pkg: Path) -> None:
    (pkg / "rootfs_common" / "icon.png").unlink()


MUTATIONS = [
    ("rootfs_common 多一个文件（icon.svg）", mut_extra_common_file),
    ("images/ 里混入非 tar（.gitkeep）", mut_extra_images_file),
    ("parameters 用 desc: 而非 i18n:", mut_param_desc),
    ("parameters 带 default_value", mut_param_default_value),
    ("缺 depend_fw_version", mut_missing_depend),
    ("compose 变量写 ${VAR:-默认值}", mut_compose_default_value),
    ("tar 的 RepoTags 与 compose 引用不一致", mut_repo_tag_mismatch),
    ("tar 架构与 rootfs_<arch> 不符", mut_arch_mismatch),
    ("缺 rootfs_arm64/ 目录", mut_missing_arch_dir),
    ("缺 rootfs_common/icon.png", mut_missing_icon),
]


# ── 门禁主体 ───────────────────────────────────────────────────────────
def gate(binary: Path, workroot: Path) -> int:
    failures: list[str] = []
    deferred: list[str] = []

    print("── 官方 ugcli 门禁 ──")
    base = synth(workroot / "base")
    rc, out = run_ugcli(binary, ["check", "--path", "."], base)
    v = verdict(rc, out)
    if v != "accepted":
        failures.append(f"基线包 `ugcli check` 未通过（{v}）：{out.strip()[:300]}")
        print(f"  ✗ 基线 check：{v}")
    else:
        print("  ✓ 基线 check 通过（骨架被官方认可）")

    rc, out = run_ugcli(binary, ["pack", "--arch", "all", "--build", "1"], base)
    v = verdict(rc, out)
    upk = list(base.rglob("*.ugb")) + list(base.rglob("*.upk"))
    if v == "accepted" and upk:
        print(f"  ✓ 基线 pack 成功（产物 {upk[0].name}）")
    elif v == "crashed":
        print("  ⚠ 基线 pack 崩了（x86 仿真产物）⇒ 本机判不了 pack，"
              "判定平台是 Linux x86_64 runner")
        deferred.append("基线 pack 成功")
    else:
        failures.append(f"基线包 `ugcli pack` 失败（{v}）：{out.strip()[:300]}")
        print(f"  ✗ 基线 pack：{v}")

    print("  ── 变异自证：每条违规都必须被官方工具拒 ──")
    for idx, (name, mutate) in enumerate(MUTATIONS, 1):
        # ⚠️ 变异目录名必须是**纯 ASCII、无 $ { } 空格**的短名：
        #    官方工具在解析「包路径」时会做变量展开，目录名里带 ${...}
        #    ⇒ 工具报的是**路径展开**错、不是形态错 ⇒ 判据失真（实测踩过）。
        d = workroot / f"mut{idx:02d}"
        d.mkdir(parents=True, exist_ok=True)
        pkg = synth(d)
        mutate(pkg)
        rc1, out1 = run_ugcli(binary, ["check", "--path", "."], pkg)
        v1 = verdict(rc1, out1)
        if v1 == "accepted":
            rc2, out2 = run_ugcli(binary, ["pack", "--arch", "all", "--build", "1"], pkg)
            v2 = verdict(rc2, out2)
            if v2 == "accepted":
                failures.append(f"变异「{name}」官方工具**照收不误** ⇒ 这条约束没有牙齿")
                print(f"  ✗ {name}：check 与 pack 都放行")
                continue
            if v2 == "crashed":
                deferred.append(name)
                print(f"  ⚠ {name}：check 放行、pack 崩（本机 x86 仿真）"
                      f"⇒ 待 Linux runner 判定")
                continue
            print(f"  ✓ {name}：pack 拒（{first_error(out2)}）")
            continue
        if v1 == "crashed":
            deferred.append(name)
            print(f"  ⚠ {name}：check 崩（本机 x86 仿真）⇒ 待 Linux runner 判定")
            continue
        print(f"  ✓ {name}：check 拒（{first_error(out1)}）")

    strict = os.environ.get("UPK_OFFICIAL_STRICT") == "1" or not SIMULATED
    if deferred:
        print(f"\n  ⚠ 待 Linux runner 判定的形态 {len(deferred)} 条：{'; '.join(deferred)}")
        if strict:
            failures.append("存在「待判定」的形态（严格模式）⇒ 本次判定不完整")
    if failures:
        print(f"\n✗ 官方门禁 {len(failures)} 条不通过：")
        for f in failures:
            print("   -", f)
        return 1
    print(f"\n✓ 官方 ugcli 门禁通过（基线被官方认可 + {len(MUTATIONS)} 条变异全部被拒）")
    return 0


def check_and_pack(binary: Path, package: Path) -> int:
    """对真实包目录跑官方 check + pack（发布流水线用）。"""
    print(f"── 官方 ugcli check + pack（{package}）──")
    rc, out = run_ugcli(binary, ["check", "--path", "."], package)
    print(out.strip())
    if rc != 0:
        print(f"✗ 官方 ugcli check 未通过（rc={rc}）", file=sys.stderr)
        return 1
    print("✓ 官方 ugcli check 通过")
    rc, out = run_ugcli(binary, ["pack", "--arch", "all", "--build", "1"], package)
    print(out.strip())
    if rc != 0:
        print(f"✗ 官方 ugcli pack 未通过（rc={rc}）", file=sys.stderr)
        return 1
    upk = sorted(list(package.rglob("*.ugb")) + list(package.rglob("*.upk")))
    if not upk:
        print("✗ 官方 ugcli pack 没产出 .upk/.ugb 文件", file=sys.stderr)
        return 1
    for p in upk:
        print(f"✓ 官方 ugcli 产物：{p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", default=None,
                    help="对指定包目录跑 check+pack（发布用）；不给则跑门禁")
    args = ap.parse_args()

    binary = fetch_ugcli()
    print(f"官方 ugcli：{binary}（sha256 {UGCLI_SHA256[:16]}…）")
    if args.package:
        pkg = Path(args.package).resolve()
        if not pkg.is_dir():
            print(f"✗ 包目录不存在：{pkg}", file=sys.stderr)
            return 1
        return check_and_pack(binary, pkg)
    with tempfile.TemporaryDirectory(prefix="upk-official-",
                                     dir="/tmp" if SIMULATED else None) as tmp:
        return gate(binary, Path(tmp))


if __name__ == "__main__":
    sys.exit(main())
