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
STAGE="$(mktemp -d)"
# ⚠️ trap 的动作**不能**带重定向（`trap ... >&2` 是无效语法 ⇒ trap 报 usage 并中止）。
#    清理信息走 stdout 即可（CI 日志里能看到）。
trap 'rm -rf "$STAGE"; echo "（已清理临时目录 ${STAGE}）"' EXIT

OUT_DIR="$ROOT/dist"
mkdir -p "$OUT_DIR"

echo "== 1) 导出内嵌镜像 tar（$REGISTRY/${NAMESPACE}，版本 ${VERSION}）=="
python3 scripts/export_upk_images.py \
  --version "$VERSION" --registry "$REGISTRY" --namespace "$NAMESPACE"

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
tar -czf "$UPK_TAR" -C "$STAGE" upk
# 镜像 tar 集合单独出一份（ugcli pack 需要它们在 rootfs_<arch>/images/ 下，
# 这里额外给一份便于分发与校验）
( cd upk && tar -czf "$OUT_DIR/pigeonbox-upk-images-${VERSION}.tar.gz" \
    rootfs_amd64/images rootfs_arm64/images )

cd "$OUT_DIR"
sha256sum pigeonbox-ugreen-upk-${VERSION}.tar.gz \
          pigeonbox-upk-images-${VERSION}.tar.gz > SHA256SUMS-UPK
cat SHA256SUMS-UPK
ls -la "$OUT_DIR"
echo "✓ UPK 组装完成：$UPK_TAR"
