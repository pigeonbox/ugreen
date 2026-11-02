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
  ── 以下 R8-R13 是 2026-11-02 按**官方 ugcli 实测口径**补齐的 ──────────
  R8  rootfs_common 只允许 icon.png + docker-compose.yaml，且 icon.png 必须存在
      （官方：`only [icon.png, docker-compose.yaml] are allowed for docker app`）
  R9  每个 support_arch 的 rootfs_<arch> 目录必须存在；其 images/ 只允许 *.tar
      （官方：`only tar files are allowed for docker app` / `rootfs_<arch> not exists`）
  R10 project.yaml 的 parameters 必须是官方形态：每项用 `i18n:`（每个语言都要有
      name + description），**不得**出现 `desc:` 或 `default_value:`
      （官方严格 yaml 解码：`field desc not found in type project.ParamItem`）
      docker 应用还必须声明 depend_docker_version + depend_fw_version
  R11 compose 的变量必须写**裸 ${VAR}**，不得写 ${VAR:-default}
      （官方：`variables not set: PB_ADMIN_PASSWORD` ⇒ pack 直接失败）
  R12 compose 的 image 引用必须与内嵌 tar 的 **RepoTags 逐字相同**
      （官方按 tar 里 manifest.json 的 RepoTags 匹配 compose，不是按文件名）
  R13 内嵌 tar 的镜像架构必须与所在 rootfs_<arch> 一致
      （官方：`image architecture mismatch (expected: amd64, actual: arm64)`）

当前规则（黄灯）
  Y1  rootfs_<arch>/images/ 为空（镜像 tar 未导出 ⇒ 还不能产出可提交包）
  Y2  正式包必须在 Linux 上用官方 ugcli pack 打出（CI 的 ubuntu-latest 即 Linux
      x86_64 ⇒ 门禁已在 CI 里真跑 pack；本机 macOS 只能验 check）
  Y3  project.yaml 的 version 与仓 VERSION 不一致（仓内文件不被列车物化 ⇒
      构建期会按 VERSION 改写成打包副本，仓内滞后不阻断出包）

零依赖（刻意不引 PyYAML：判定按行解析，与本仓其它脚本一致）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from upk_common import (  # noqa: E402
    compose_images,
    image_tag,
    tar_architecture,
    tar_repo_tags,
)

# `--strict`：校验**组装好的包**（发布前）时的严格口径。
#   · 严格模式：images/ 里出现「compose 不引用」的 tar ⇒ **红灯**
#     （官方 ugcli 逐个校验每个 tar ⇒ 报 `image '...' not referenced in
#      docker-compose.yaml` ⇒ 打包直接失败；实测）。
#   · 宽松模式（默认，校验仓内骨架时）：旧版残留 ⇒ **黄灯**
#     （tar 不入 git ⇒ 仓库 checkout 里必然为空；本地导出后会留上一版残留
#      ⇒ 判红会让列车 bump 后本地必然红 ⇒ 门禁变噪声）。
STRICT = "--strict" in sys.argv[1:]
PKG = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), REPO / "upk"))

# 官方 ParamItem 的合法字段集（实测：desc / default_value 都不在其中 ⇒ 严格解码拒绝）
PARAM_ALLOWED_KEYS = {"key", "type", "required", "i18n", "description"}
# 官方 docker app 的 rootfs_common 允许清单
COMMON_ALLOWED = {"icon.png", "docker-compose.yaml"}

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
def compose_pairs() -> list[tuple[str, str]]:
    """compose 里的 [(服务名, image 引用)]；文件缺失 ⇒ 空列表。"""
    f = PKG / "rootfs_common" / "docker-compose.yaml"
    if not f.is_file():
        return []
    return compose_images(read(f))


def check_images() -> list[str]:
    """检查 compose 的 image 行；返回镜像引用列表（供 R3 用）。"""
    f = PKG / "rootfs_common" / "docker-compose.yaml"
    if not f.is_file():
        fail(f"缺 {f.relative_to(REPO)}（UPK 的 compose 入口）")
        return []
    text = read(f)
    images = [ref for _, ref in compose_pairs()]
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
    ⇒ 仓库 checkout 里**本来就没有** tar ⇒ 「目录为空 / 目录不存在」是**黄灯**
    （还没出包），不是红灯。
    ⇒ 红灯只针对「**有** tar 但**对不上**」（命名/版本错位）——那说明
    导出流程与 compose 漂移了，打出来的包装不上。
    """
    for arch_dir in ("rootfs_amd64", "rootfs_arm64"):
        d = PKG / arch_dir / "images"
        if not d.is_dir():
            # Y1：目录不存在 = 还没导出镜像（导出脚本会自己 mkdir）⇒ 黄灯
            warn(f"{arch_dir}/images/ 不存在——镜像 tar 未导出 ⇒ 现在还不能产出可提交的 UPK 包"
                 f"（跑 scripts/export_upk_images.py --version <ver>；tar 不入 git，"
                 f"由 CI 导出并作为 Release 资产发布）")
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
            if want in tars:
                continue
            # ⚠️ **缺 tar 与「有错版本 tar」要分开判**（2026-10-31 实测）：
            #   镜像 tar **不入 git** ⇒ 仓库 checkout 里必然为空；
            #   本地跑过导出脚本后，目录里会留下**上一版**的 tar（如 4.6.3），
            #   而 compose 已随列车 bump 到 4.6.4 ⇒ 「缺 4.6.4 的 tar」是
            #   **还没导出本车**（黄灯），不是「导出流程与 compose 漂移」（红灯）。
            #   ⇒ 红灯只留给「目录里有 tar、且**恰好**是本车 tag 但命名对不上」
            #     ——那才是真的错配。
            others = [t for t in tars if t != want]
            if others and all(re.search(r"pigeonbox-\w+-\d+\.\d+\.\d+\.tar$", t)
                              for t in others):
                warn(f"{arch_dir}/images/ 里没有本车（{tag.group(1)}）的 {want}"
                     f"——现有的是旧版残留（{', '.join(others)}）"
                     f"⇒ 还没导出本车镜像（跑 scripts/export_upk_images.py "
                     f"--version {tag.group(1)}）")
                continue
            fail(f"{arch_dir}/images/ 里缺 {want}"
                 f"（compose 引用了 {img} ⇒ 打包器按 tag 找 tar，缺了装不上）"
                 f"；现有：{', '.join(tars) or '(空)'}")


# ── R5: compose 变量必须是 project.yaml 已声明的 ────────────────────────
def declared_parameters() -> set[str]:
    """project.yaml 的 parameters 里声明的 key 集合。"""
    py = PKG / "project.yaml"
    if not py.is_file():
        return set()
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
        #    type/required/i18n 等**映射行**，它们同样不以 - 开头
        #    ⇒ 那样判会把块在第一个 type 行就截断（实测漏抓全部 key）。
        if indent <= params_indent:
            in_params = False
            continue
        m = re.match(r"-\s*key\s*:\s*(\S+)", s)
        if m:
            declared.add(m.group(1).strip("\"'"))
    return declared


def compose_code_lines(text: str) -> list[tuple[int, str]]:
    """compose 的**代码行** [(行号, 内容)]（整行注释剔除）。

    ⚠️ 必须剔注释：compose 头部注释里会写「${VAR}」「${VAR:-default}」这类
    **举例**，按原文扫会把注释里的例子当成真变量 ⇒ 误报红灯。
    """
    out: list[tuple[int, str]] = []
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append((i, raw))
    return out


def check_variables() -> None:
    py = PKG / "project.yaml"
    comp = PKG / "rootfs_common" / "docker-compose.yaml"
    if not py.is_file() or not comp.is_file():
        return
    declared = declared_parameters()
    text = read(comp)
    used = set()
    for _, raw in compose_code_lines(text):
        used.update(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", raw))
    for var in sorted(used):
        if var in declared or var == "TZ":
            continue
        fail(f"compose 用了未在 project.yaml parameters 声明的变量 ${{{var}}}"
             f"（官方打包器强校验 ⇒ 打包直接失败）")


# ── R11: compose 变量必须是裸 ${VAR} ───────────────────────────────────
def check_variable_form() -> None:
    """`${VAR:-default}` 形态：官方 check **放行**，但 pack 报 variables not set。

    ⇒ 只靠 `ugcli check` 的门禁会漏掉这一类 ⇒ 必须跑 pack（见 CI 门禁）。
    """
    comp = PKG / "rootfs_common" / "docker-compose.yaml"
    if not comp.is_file():
        return
    text = read(comp)
    for lineno, raw in compose_code_lines(text):
        for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}", raw):
            var, rest = m.group(1), m.group(2).strip()
            if rest:
                fail(f"compose 第 {lineno} 行的变量 ${{{var}{rest}}} 不是裸变量"
                     f"（官方要求写裸 ${{{var}}}；写成带默认值的形式 ⇒ `ugcli check` 放行但 "
                     f"`ugcli pack` 报 variables not set: {var} ⇒ 打包直接失败）")


# ── R6: 禁 privileged / host 网络 ──────────────────────────────────────
def check_hardening() -> None:
    comp = PKG / "rootfs_common" / "docker-compose.yaml"
    if not comp.is_file():
        return
    text = read(comp)
    code = "\n".join(raw for _, raw in compose_code_lines(text))
    if re.search(r"^\s*privileged\s*:\s*true", code, re.M):
        fail("compose 出现 privileged: true（官方打包器禁）")
    if re.search(r"^\s*network_mode\s*:\s*host", code, re.M):
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


# ── R8/R9: 官方布局约束 ────────────────────────────────────────────────
def check_layout() -> None:
    """rootfs_common 的允许清单 + rootfs_<arch> 目录 + images/ 只允许 tar。"""
    common = PKG / "rootfs_common"
    if not common.is_dir():
        fail("缺 rootfs_common/（UPK 的公共 rootfs 目录）")
        return
    entries = sorted(p.name for p in common.iterdir())
    extra = [e for e in entries if e not in COMMON_ALLOWED]
    if extra:
        fail(f"rootfs_common/ 里有官方不允许的文件：{', '.join(extra)}"
             f"（官方只允许 {', '.join(sorted(COMMON_ALLOWED))}"
             f" ⇒ 打包报 only [icon.png, docker-compose.yaml] are allowed for docker app）")
    if "icon.png" not in entries:
        fail("缺 rootfs_common/icon.png（官方要求 ⇒ 打包报 icon.png not exists）")

    arches: list[str] = []
    py = PKG / "project.yaml"
    if py.is_file():
        m = re.search(r"^\s*support_arch\s*:\s*\[([^\]]*)\]", read(py), re.M)
        if m:
            arches = [a.strip().strip("\"'") for a in m.group(1).split(",") if a.strip()]
    if not arches:
        arches = ["amd64", "arm64"]
    for arch in arches:
        d = PKG / f"rootfs_{arch}"
        if not d.is_dir():
            fail(f"缺 {d.name}/（官方按 support_arch 逐个校验 ⇒ 打包报 {d.name} not exists）")
            continue
        images = d / "images"
        if not images.is_dir():
            continue
        bad = sorted(p.name for p in images.iterdir() if p.suffix != ".tar")
        if bad:
            fail(f"{d.name}/images/ 里有官方不允许的文件：{', '.join(bad)}"
                 f"（官方只允许 tar ⇒ 打包报 only tar files are allowed for docker app）")


# ── R10: project.yaml 的参数必须是官方形态 ─────────────────────────────
def _param_blocks(text: str) -> list[list[tuple[int, str]]]:
    """切出 parameters 下每个条目（以 `- key:` 开头的块）。

    返回 [[(缩进, 去掉注释后的行), ...], ...]，每个元素是一个参数条目。
    """
    blocks: list[list[tuple[int, str]]] = []
    in_params = False
    params_indent = 0
    cur: list[tuple[int, str]] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if not in_params:
            if s.startswith("parameters:"):
                in_params = True
                params_indent = indent
            continue
        if indent <= params_indent:
            in_params = False
            continue
        if re.match(r"^-\s+\S", s):
            cur = [(indent, s)]
            blocks.append(cur)
        elif cur:
            cur.append((indent, s))
    return blocks


def check_project_official() -> None:
    """官方 ParamItem 形态 + docker app 必填 depend_*。"""
    py = PKG / "project.yaml"
    if not py.is_file():
        return
    text = read(py)

    for field in ("depend_docker_version", "depend_fw_version"):
        if not re.search(rf"^\s*{field}\s*:\s*\S+", text, re.M):
            fail(f"project.yaml 缺 {field}（官方 docker 应用必填"
                 f"⇒ 打包报 '{field}' is required）")

    blocks = _param_blocks(text)
    for idx, block in enumerate(blocks):
        # YAML 缩进关系（实测 project.yaml 的写法）：
        #   - key: X        ← base
        #     type: path    ← base+2（同一条目的其它键）
        #     i18n:         ← base+2
        #       zh-CN:      ← base+4（语言）
        #         name: …   ← base+6（语言下的字段）
        base = block[0][0]
        item_indent = base + 2
        keys: set[str] = set()
        i18n_at: int | None = None
        for indent, s in block:
            if indent == base and s.startswith("-"):
                keys.add(s.lstrip("- ").split(":", 1)[0].strip())
                continue
            if indent != item_indent:
                continue
            m = re.match(r"^([A-Za-z_][\w]*)\s*:", s)
            if not m:
                continue
            keys.add(m.group(1))
            if m.group(1) == "i18n":
                i18n_at = indent

        bad = sorted(keys - PARAM_ALLOWED_KEYS)
        if bad:
            fail(f"project.yaml 的第 {idx + 1} 个参数用了官方不存在的字段："
                 f"{', '.join(bad)}"
                 f"（官方严格 yaml 解码 ⇒ 打包报 field {bad[0]} not found in "
                 f"type project.ParamItem）")
        if "i18n" not in keys:
            fail(f"project.yaml 的第 {idx + 1} 个参数缺 i18n"
                 f"（官方要求 i18n 且每语言都要有 name + description"
                 f"⇒ 打包报 'parameters[{idx}].i18n' is required）")
            continue
        if i18n_at is None:
            continue
        langs: dict[str, set[str]] = {}
        cur: str | None = None
        for indent, s in block:
            if indent <= i18n_at:
                continue
            if indent == i18n_at + 2:
                m = re.match(r"^([\w.\-]+)\s*:", s)
                cur = m.group(1) if m else None
                if cur:
                    langs.setdefault(cur, set())
                continue
            if indent == i18n_at + 4 and cur:
                m = re.match(r"^([A-Za-z_][\w]*)\s*:", s)
                if m:
                    langs[cur].add(m.group(1))
        for lang, have in langs.items():
            missing = [f for f in ("name", "description") if f not in have]
            if missing:
                fail(f"project.yaml 的第 {idx + 1} 个参数的 i18n[{lang}] 缺 "
                     f"{', '.join(missing)}"
                     f"（官方要求每语言都要有 name + description"
                     f"⇒ 打包报 'parameters[{idx}].i18n[{lang}].{missing[0]}' is required）")


# ── R12/R13: 内嵌 tar 的 RepoTags / 架构必须对得上 ─────────────────────
def check_repo_tags() -> None:
    """官方按 tar 内 manifest.json 的 **RepoTags** 匹配 compose 的 image。

    ⇒ 文件名对、标签不对 ⇒ 官方报 missing image tar for service ⇒ 包装不上。
    ⇒ 反过来：images/ 里出现 **compose 不引用** 的 tar ⇒ 官方报
      `image '...' not referenced in docker-compose.yaml` ⇒ 同样包装不上
      ⇒ 严格模式（校验组装好的包）判**红灯**。
    """
    pairs = compose_pairs()
    if not pairs:
        return
    wanted = {ref for _, ref in pairs}
    for arch_dir in ("rootfs_amd64", "rootfs_arm64"):
        d = PKG / arch_dir / "images"
        if not d.is_dir():
            continue
        tars = sorted(d.glob("*.tar"))
        if not tars:
            continue
        tags: list[str] = []
        unreadable: list[str] = []
        for t in tars:
            rt = tar_repo_tags(t)
            if not rt:
                unreadable.append(t.name)
                continue
            tags.extend(rt)
            # R13：架构必须与所在目录一致
            a = tar_architecture(t)
            if a and a != arch_dir.replace("rootfs_", ""):
                fail(f"{arch_dir}/images/{t.name} 的镜像架构是 {a}，与所在目录 "
                     f"{arch_dir} 不符"
                     f"（官方 ⇒ image architecture mismatch (expected: "
                     f"{arch_dir.replace('rootfs_', '')}, actual: {a})）")
        for name in unreadable:
            fail(f"{arch_dir}/images/{name} 读不出 manifest.json 的 RepoTags"
                 f"（不是 docker-save 同构的镜像 tar ⇒ 官方匹配不到 compose 的 image）")
        for svc, ref in pairs:
            if ref in tags:
                continue
            want_ver = image_tag(ref)
            same_ver = [t for t in tags if image_tag(t) == want_ver]
            if same_ver or STRICT:
                fail(f"{arch_dir}/images/ 里的镜像标签与 compose 引用不一致："
                     f"compose 写 {ref}，tar 里是 {', '.join(sorted(set(same_ver))) or '(无同版标签)'}"
                     f"（官方按 **tar 的 RepoTags** 匹配 compose，不是按文件名"
                     f"⇒ 打包报 missing image tar for service '{svc}'）")
            else:
                warn(f"{arch_dir}/images/ 里的镜像标签是旧版残留"
                     f"（compose 要 {ref}，tar 里是 "
                     f"{', '.join(sorted(set(tags))) or '(读不出)'}）"
                     f"⇒ 还没导出本车镜像（跑 scripts/export_upk_images.py）")
        # 反向：compose 不引用的 tar ⇒ 官方逐个校验时拒 ⇒ 严格模式红灯
        extra = sorted({t for t in tags if t not in wanted})
        if extra:
            msg = (f"{arch_dir}/images/ 里有 compose **不引用**的镜像 tar："
                   f"{', '.join(extra)}"
                   f"（官方逐个校验每个 tar ⇒ 报 image '...' not referenced in "
                   f"docker-compose.yaml ⇒ 打包直接失败）")
            if STRICT:
                fail(msg)
            else:
                warn(msg + "（组装时只放本车 tar ⇒ 不影响出包）")


# ── Y3: project.yaml 的 version 与仓 VERSION ───────────────────────────
def check_version_sync() -> None:
    py = PKG / "project.yaml"
    ver_file = REPO / "VERSION"
    if not py.is_file() or not ver_file.is_file():
        return
    m = re.search(r"^\s*version\s*:\s*(\S+)", read(py), re.M)
    repo_ver = read(ver_file).strip().lstrip("v")
    if m and repo_ver and m.group(1).strip("\"'") != repo_ver:
        warn(f"project.yaml 的 version（{m.group(1)}）与仓 VERSION（{repo_ver}）不一致"
             f"（UPK 包版本 ≠ 产品版本；构建期按 VERSION 改写打包副本 ⇒ 不阻断出包，"
             f"但仓内文件不被列车物化 ⇒ 会滞后）")


def main() -> int:
    images = check_images()
    check_embedded_tars(images)
    check_variables()
    check_variable_form()
    check_hardening()
    check_project_yaml()
    check_project_official()
    check_layout()
    check_repo_tags()
    check_version_sync()

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
