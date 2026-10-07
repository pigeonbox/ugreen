#!/usr/bin/env bash
# 打包绿联 UGOS Pro「compose 项目一键部署包」zip(发布资产)。
# 内容:deploy/ 下的 compose 编排(原版+ghcr 加速版)、.env 模板、安装指引。
# 用法: ./scripts/build-zip.sh <版本>     例: ./scripts/build-zip.sh 0.1.0
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:?用法: build-zip.sh <版本>(例: 0.1.0)}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

OUT="dist/pigeonbox-ugreen-${VERSION}.zip"
mkdir -p dist
# 文本压 LF(UI 粘贴与 Windows 下载解压场景都不被 CRLF 坑)
for f in deploy/compose.yml deploy/compose.ghcr-mirror.yml deploy/env.example README.md; do
    perl -pi -e 's/\r$//' "$f"
    cp "$f" "$STAGE/$(basename "$f")"
done
mv "$STAGE/README.md" "$STAGE/安装指引.md"
(cd "$STAGE" && zip -q -r "$OLDPWD/$OUT" .)
echo "✓ ${OUT}"
unzip -l "$OUT"
