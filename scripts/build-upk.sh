#!/usr/bin/env bash
# 组装绿联 UPK 应用包骨架 + 导出内嵌镜像 tar（发布资产）。
#
# 产物：
#   dist/pigeonbox-ugreen-upk-<版本>.tar.gz   ← UPK 应用包骨架（ugcli pack 的输入）
#   dist/pigeonbox-upk-images-<版本>.tar.gz    ← 内嵌镜像 tar 集合（可选单独发布）
#   dist/SHA256SUMS-UPK
#
# ⚠️ **本脚本产出的是「ugcli pack 的输入」，不是最终的 .upk**：
#   官方 ugcli 必须在 **Linux** 上跑（保证 Unix 执行位），且 sideload 需
#   设备开发者授权（ugdev.sig）⇒ 最终 .upk 的产出与上架是外部条件。
#   本脚本把「能自动化的部分」全部做完：骨架组装 + 镜像 tar 导出 + 校验。
#
# 用法: ./scripts/build-upk.sh <版本>   例: ./scripts/build-upk.sh 4.6.3
#       <版本> 必须与本仓 VERSION 一致（compose 的镜像 tag 依赖它）
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

VERSION="${1:?用法: build-upk.sh <版本>(例: 4.6.3)}"

# 版本一致性断言：与 VERSION 不符 ⇒ 拒绝（compose 的镜像 tag 与内嵌 tar
# 都按 VERSION 钉，版本错了 ⇒ 打出来的包装不上）。
REPO_VER="$(tr -d '[:space:]' < VERSION)"
if [ "$REPO_VER" != "$VERSION" ]; then
  echo "版本不一致：参数 $VERSION ≠ VERSION 文件 $REPO_VER" >&2
  exit 1
fi

REGISTRY="${UPK_REGISTRY:-ghcr.io}"
NAMESPACE="${UPK_NAMESPACE:-pigeonbox}"
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
trap 'rm -rf "$STAGE"; echo "（已清理临时目录 ${STAGE}）"' EXIT

OUT_DIR="$ROOT/dist"
mkdir -p "$OUT_DIR"

echo "== 1) 导出内嵌镜像 tar（$REGISTRY/${NAMESPACE}，版本 ${VERSION}，等待预算 ${WAIT_MINUTES} 分钟）=="
python3 scripts/export_upk_images.py \
  --version "$VERSION" --registry "$REGISTRY" --namespace "$NAMESPACE" \
  --wait-minutes "$WAIT_MINUTES"

echo "== 2) 组装 UPK 骨架 =="
PKG_STAGE="$STAGE/upk"
mkdir -p "$PKG_STAGE"
# project.yaml / rootfs_common 逐文件复制（不含 images/，那由导出脚本填）
cp upk/project.yaml "$PKG_STAGE/"
cp -r upk/rootfs_common "$PKG_STAGE/"
for arch in amd64 arm64; do
  mkdir -p "$PKG_STAGE/rootfs_$arch/images"
  cp upk/rootfs_$arch/images/*.tar "$PKG_STAGE/rootfs_$arch/images/" 2>/dev/null || true
done

echo "== 3) 校验组装的骨架（规则 + 镜像对应）=="
python3 scripts/check_upk_package.py "$PKG_STAGE"

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
COPYFILE_DISABLE=1 tar -czf "$UPK_TAR" -C "$STAGE" upk
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
