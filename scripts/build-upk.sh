#!/usr/bin/env bash
# 组装绿联 UPK 应用包骨架 + 导出内嵌镜像 tar（发布资产）。
#
# 产物：
#   dist/pigeonbox-ugreen-upk-<版本>.tar.gz   ← UPK 应用包骨架（ugcli pack 的输入）
#   dist/pigeonbox-upk-images-<版本>.tar.gz    ← 内嵌镜像 tar 集合（可选单独发布）
#   dist/SHA256SUMS-UPK
#   dist/upk/                                ← 组装好的包目录（官方 ugcli pack 的输入）
#
# ⚠️ **本脚本产出的是「ugcli pack 的输入」，不是最终的 .upk**：
#   官方 ugcli 必须在 **Linux** 上跑（保证 Unix 执行位）——**CI 的 ubuntu-latest
#   就是那台 Linux 打包机**（issue #100 里「待 Linux 打包机」的前提不成立）
#   ⇒ release 流水线里接着跑 `scripts/check_upk_official.py --package dist/upk`
#   ⇒ 出真正的 .upk。sideload 需设备开发者授权（ugdev.sig）⇒ 仍是外部条件。
#   本脚本把「能自动化的部分」全部做完：骨架组装 + 镜像 tar 导出 + 校验。
#
# 用法: ./scripts/build-upk.sh <版本>   例: ./scripts/build-upk.sh 4.7.1
#       <版本> 必须与本仓 VERSION 一致（compose 的镜像 tag 依赖它）
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

VERSION="${1:?用法: build-upk.sh <版本>(例: 4.7.1)}"

# 版本一致性断言：与 VERSION 不符 ⇒ 拒绝（compose 的镜像 tag 与内嵌 tar
# 都按 VERSION 钉，版本错了 ⇒ 打出来的包装不上）。
REPO_VER="$(tr -d '[:space:]' < VERSION)"
if [ "$REPO_VER" != "$VERSION" ]; then
  echo "版本不一致：参数 $VERSION ≠ VERSION 文件 $REPO_VER" >&2
  exit 1
fi

# **拉取源** registry（镜像字节从这儿拉；标签始终取 compose 的 image 引用）。
REGISTRY="${UPK_REGISTRY:-ghcr.io}"
# 镜像尚未发布时的等待预算（分钟）。
# ⚠️ **CI 必须给足**：内嵌镜像 tar 来自 ghcr，而镜像是由 **另一个仓**
#    （pigeonbox/server）的 release 流水线推的 ⇒ 两边并发、无 `needs:` 可排；
#    且多架构 push 约 20 分钟，manifest **索引**在 push 末尾才写 ⇒ tag 在
#    整个推送期间查不到（匿名查 manifest = 404）。
#    实测：4.6.4 的 upk job 因 404 直接失败 ⇒ UPK 产物没出来。
#    ⇒ 默认给 0（本地/手动跑：镜像早就在了，不需要等）；CI 显式设成 40。
WAIT_MINUTES="${UPK_IMAGE_WAIT_MINUTES:-0}"
STAGE="$(mktemp -d)"
# ⚠️ trap 的动作**不能**带重定向（`trap ... >&2` 是无效语法 ⇒ trap 报 usage 并中止）。
#    清理信息走 stdout 即可（CI 日志里能看到）。
OK=0
trap 'rc=$?; rm -rf "$STAGE"; if [ "$rc" -ne 0 ]; then exit $rc; fi; if [ "$OK" != 1 ]; then exit 1; fi; exit 0' EXIT

OUT_DIR="$ROOT/dist"
mkdir -p "$OUT_DIR"

echo "== 1) 导出内嵌镜像 tar（拉取源 ${REGISTRY}，版本 ${VERSION}，等待预算 ${WAIT_MINUTES} 分钟）=="
# ⚠️ 标签与拉取源分离（官方 ugcli 按 tar 的 RepoTags 匹配 compose 的 image
#    引用）⇒ 导出脚本**按 compose 里的 image 引用**打标签，不按拉取源。
python3 scripts/export_upk_images.py \
  --version "$VERSION" --registry "$REGISTRY" \
  --wait-minutes "$WAIT_MINUTES"

echo "== 2) 组装 UPK 骨架 =="
# 组装目录留在 dist/ 下（gitignore 覆盖）⇒ 发布流水线可直接对它跑官方
# `ugcli pack`（官方工具要求路径下就是包目录）。
# ⚠️ 顶层目录名**必须叫 upk** ⇒ 打出来的 tar.gz 内部仍是 `upk/...`
#    （与历史 Release 资产布局一致，别让下游解包脚本改口径）。
PKG_STAGE="$OUT_DIR/upk"
rm -rf "$PKG_STAGE"
mkdir -p "$PKG_STAGE/rootfs_common"

# project.yaml：复制 + **按 VERSION 改写 version 段**
# ⚠️ 改写的原因：UPK 包版本（应用中心展示的版本）必须跟着产品版本走，
#    而 project.yaml **不在列车物化清单里**（列车只改 VERSION 与
#    upk/rootfs_common/docker-compose.yaml 的镜像 tag）⇒ 仓内文件会滞后。
#    ⇒ 打包副本一律按仓 VERSION 改写 ⇒ 打出来的包版本永远对得上。
#    改写用 sed 的**行首锚定**（`^version:`）⇒ 不会误伤 `spec_version:`。
sed -E "s/^(version:[[:space:]]*).*/\1${VERSION}/" upk/project.yaml \
  > "$PKG_STAGE/project.yaml"
if ! grep -q "^version: ${VERSION}$" "$PKG_STAGE/project.yaml"; then
  echo "改写后 project.yaml 的 version 不是 ${VERSION}（源文件里没有 version 段？）" >&2
  exit 1
fi

# rootfs_common：**只**放官方允许的两个文件
# ⚠️ 官方只允许 [icon.png, docker-compose.yaml] ⇒ 多一个文件就打包失败
#    （实测：icon.svg ⇒ only [icon.png, docker-compose.yaml] are allowed）。
#    ⇒ 这里逐个复制（不是整目录 cp -r）⇒ 仓库里万一又混进别的文件也不会带进包。
cp upk/rootfs_common/docker-compose.yaml "$PKG_STAGE/rootfs_common/"
cp upk/rootfs_common/icon.png "$PKG_STAGE/rootfs_common/"
# 断言：rootfs_common 里恰好是这两个（官方 allow-list 的机械判定）
EXTRA="$(ls -A "$PKG_STAGE/rootfs_common" | grep -v -x -e docker-compose.yaml -e icon.png || true)"
if [ -n "$EXTRA" ]; then
  echo "rootfs_common 里有多余文件：${EXTRA}（官方只允许 icon.png + docker-compose.yaml）" >&2
  exit 1
fi

for arch in amd64 arm64; do
  mkdir -p "$PKG_STAGE/rootfs_$arch/images"
done

# 只放**本车**的镜像 tar（compose 引用的那些）。
# ⚠️ 2026-11-02 官方实测：images/ 里多一个 compose 不引用的 tar
#    ⇒ 官方逐个校验时拒：`image 'ghcr.io/pigeonbox/frontend:4.6.4' not
#    referenced in docker-compose.yaml` ⇒ **打包直接失败**。
#    ⇒ 旧版残留（本地导出过上一版）绝不能进包。
python3 -c "
import pathlib, sys
sys.path.insert(0, 'scripts')
import upk_common as U
sys.exit(U.stage_images(pathlib.Path('$PKG_STAGE'), pathlib.Path('upk'), '$VERSION'))
"
# 断言：images/ 里只能有 tar（官方 allow-list）
for arch in amd64 arm64; do
  BAD="$(ls -A "$PKG_STAGE/rootfs_$arch/images" | grep -v '\.tar$' || true)"
  if [ -n "$BAD" ]; then
    echo "rootfs_$arch/images/ 里有非 tar 文件：${BAD}（官方只允许 tar）" >&2
    exit 1
  fi
done

echo "== 3) 校验组装的骨架（规则 + 镜像对应）=="
# 严格模式：组装好的包必须**一个多余 tar 都没有**（官方会逐个校验）。
python3 scripts/check_upk_package.py --strict "$PKG_STAGE"

echo "== 4) 打包 =="
UPK_TAR="$OUT_DIR/pigeonbox-ugreen-upk-${VERSION}.tar.gz"
# ⚠️ **COPYFILE_DISABLE=1 不可省**（2026-10-31 实测，跨仓同源缺陷）：
#   macOS 的 bsdtar 遵循 AppleDouble 约定 ⇒ 建包时会**额外塞进** `._<文件名>`
#   条目（资源叉/扩展属性的伴生文件）⇒ 包内条目数翻倍。
#   实测：UPK 包 28 个条目里有 6 个是 `._*` 垃圾；TOS 7 的 4 文件包更惨
#   （4 → 8 ⇒ **TOS 只收恰好 4 个文件 ⇒ 送审必被打回**）。
#   ⚠️ 且 **bsdtar 自己的 `tar -tzf` 会把这些条目藏起来** ⇒ 用它断言条目数
#   **恰好瞎在这一点上** ⇒ 自检通过、包却是坏的。
#   ⇒ 建包时 COPYFILE_DISABLE=1；断言一律用**标准读取器**（python tarfile），
#     与平台用标准库解包的视角一致。
COPYFILE_DISABLE=1 tar -czf "$UPK_TAR" -C "$OUT_DIR" upk
# 镜像 tar 集合单独出一份（ugcli pack 需要它们在 rootfs_<arch>/images/ 下，
# 这里额外给一份便于分发与校验）
( cd upk && COPYFILE_DISABLE=1 tar -czf "$OUT_DIR/pigeonbox-upk-images-${VERSION}.tar.gz" \
    rootfs_amd64/images rootfs_arm64/images )

# 断言：两个包都不得混入 AppleDouble 垃圾条目（标准读取器口径）
python3 - "$OUT_DIR/pigeonbox-ugreen-upk-${VERSION}.tar.gz" \
           "$OUT_DIR/pigeonbox-upk-images-${VERSION}.tar.gz" <<'PYEOF'
import sys
import tarfile

for path in sys.argv[1:]:
    with tarfile.open(path, "r:gz") as t:
        names = t.getnames()
    junk = [n for n in names if n.startswith("._") or "/._" in n]
    if junk:
        print(f"✗ {path} 混入 macOS AppleDouble 垃圾条目 {len(junk)} 个：{junk[:5]}",
              file=sys.stderr)
        sys.exit(1)
PYEOF

cd "$OUT_DIR"
sha256sum pigeonbox-ugreen-upk-${VERSION}.tar.gz \
          pigeonbox-upk-images-${VERSION}.tar.gz > SHA256SUMS-UPK
cat SHA256SUMS-UPK
ls -la "$OUT_DIR"
echo "✓ UPK 组装完成：$UPK_TAR"
echo "  ⇒ 下一步（CI 的 Linux runner 上）：python3 scripts/check_upk_official.py --package $PKG_STAGE"
OK=1
