#!/usr/bin/env bash
# 打包绿联 UGOS Pro「compose 项目一键部署包」zip(发布资产)。
# 内容:deploy/ 下的 compose 编排(原版+ghcr 加速版)、.env 模板、安装指引。
# 用法: ./scripts/build-zip.sh <版本>     例: ./scripts/build-zip.sh 0.1.0
#
# ⚠️ 2026-10-26 审计 P2（本脚本的两处修法）：
#   ① 此前对**仓里被 git 跟踪的源文件**就地 `perl -pi -e 's/\r$//'`
#      ⇒ 构建后工作树变脏（同一次 CI job 后续若做 git diff --exit-code 就会把
#      副作用提交进仓）⇒ 改成「先 cp 到 $STAGE,再对**副本**压 LF」
#      （与 synology/scripts/build-spk.sh 同款做法）；
#   ② 产物路径用 "$OLDPWD/$OUT" 定位 ⇒ 从别的目录调用时 zip 写到**调用目录**的
#      dist/（不存在则 zip 直接失败,错误信息与真实原因不符）⇒ 改成显式绝对路径。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

VERSION="${1:?用法: build-zip.sh <版本>(例: 0.1.0)}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

OUT="dist/pigeonbox-ugreen-${VERSION}.zip"
OUT_ABS="$ROOT/$OUT"
mkdir -p "$ROOT/dist"

# 源文件内容指纹(构建前)⇒ 构建后比对:构建**不许改**仓里被跟踪的源文件。
# 为什么不用 git status 比对:status 只报「哪个路径脏」,不报内容——若该文件
# 构建前就已经是脏的(本地开发常见),就地改写**看不出来** ⇒ 守卫会漏放。
# ⇒ 直接对**内容**取指纹(sha256),逐字节可比。
SRC_FILES="deploy/compose.yml deploy/compose.ghcr-mirror.yml deploy/env.example README.md"
fingerprint() {
  for f in $SRC_FILES; do
    [ -f "$f" ] && printf '%s %s\n' "$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$f" | cut -d' ' -f1)" "$f"
  done
}
FINGERPRINT_BEFORE="$(fingerprint)"

# 文本压 LF(UI 粘贴与 Windows 下载解压场景都不被 CRLF 坑)
# —— 只对**副本**做,绝不碰仓里被跟踪的源文件。
for f in deploy/compose.yml deploy/compose.ghcr-mirror.yml deploy/env.example README.md; do
    cp "$f" "$STAGE/$(basename "$f")"
    perl -pi -e 's/\r$//' "$STAGE/$(basename "$f")"
done
mv "$STAGE/README.md" "$STAGE/安装指引.md"
(cd "$STAGE" && zip -q -r "$OUT_ABS" .)


# 构建后源文件内容必须**逐字节未变**(就地 perl -pi 的机械守卫)
FINGERPRINT_AFTER="$(fingerprint)"
if [ "$FINGERPRINT_BEFORE" != "$FINGERPRINT_AFTER" ]; then
  echo "✗ 构建改动了仓里的源文件(构建前后内容指纹不一致):" >&2
  diff <(printf '%s\n' "$FINGERPRINT_BEFORE") <(printf '%s\n' "$FINGERPRINT_AFTER") >&2 || true
  exit 1
fi
echo "✓ 构建未改动任何源文件(内容指纹一致)"

echo "✓ ${OUT}"
unzip -l "$OUT_ABS"
