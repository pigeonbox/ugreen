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

# ── shellkit（编译侧统一管道；**设备侧脚本一律不引**，见 C1） ───────────────
# vendor/shellkit 是上游镜像（`shellkit vendor .` 的产物）⇒ **勿手改**。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO_ROOT/vendor/shellkit/lib/bootstrap.sh"
export SHELLKIT_LANG="${SHELLKIT_LANG:-zh_CN}"

cd "$REPO_ROOT"

VERSION="${1:?用法: build-zip.sh <版本>(例: 0.1.0)}"
STAGE="$(mktemp -d)"
# 清理栈 + trap 安装（shellkit 接管 EXIT/INT/TERM/HUP）。
# ⚠️ 原来的手写 trap（`trap 'rc=$?; rm -rf "$STAGE"; …OK 哨兵…' EXIT`）由
#    `trap_install` 取代：它的处理器取 $? 后原样 `exit "$__st"`
#    ⇒ **退出码照旧透传**（C3），并顺带覆盖 INT/TERM/HUP（原来不清理）。
#    原 trap 里还有一条「rc==0 且没跑到结尾 ⇒ 退出 1」的成功哨兵判定：本脚本的
#    失败出口只有三种（`${1:?…}` 退出 1、指纹不一致 `die 1`、`set -e` 下命令
#    失败非 0）⇒「跑完却没成功」的出口不存在 ⇒ 与原哨兵等价（不再单列 OK 标志）。
trap_push 'rm -rf "$STAGE"'
trap_install

OUT="dist/pigeonbox-ugreen-${VERSION}.zip"
OUT_ABS="$REPO_ROOT/$OUT"
fs_ensure_dir "$REPO_ROOT/dist"

# 源文件内容指纹(构建前)⇒ 构建后比对:构建**不许改**仓里被跟踪的源文件。
# 为什么不用 git status 比对:status 只报「哪个路径脏」,不报内容——若该文件
# 构建前就已经是脏的(本地开发常见),就地改写**看不出来** ⇒ 守卫会漏放。
# ⇒ 直接对**内容**取指纹(sha256),逐字节可比。
# ⚠️ 2026-11-18（shellkit 改造）：原来手写的 `sha256sum … || shasum -a 256 …`
#    里 `||` 永远轮不到（管道状态取的是 `cut` 的 0）⇒ 在**没有 sha256sum 的机器**
#    （老 macOS / 精简容器）上算出的是**空串** ⇒ 这道守卫在那些机器上恒绿
#    （有 sha256sum 的 Linux CI 与 macOS 13+ 上正常）。换成 shellkit 的
#    `net_sha256`（sha256sum / shasum 都认）⇒ 两端同口径，守卫到处都真的有牙齿。
SRC_FILES="deploy/compose.yml deploy/compose.ghcr-mirror.yml deploy/env.example README.md"
fingerprint() {
  for f in $SRC_FILES; do
    [ -f "$f" ] && printf '%s %s\n' "$(net_sha256 "$f")" "$f"
  done
}
FINGERPRINT_BEFORE="$(fingerprint)"

# 文本压 LF(UI 粘贴与 Windows 下载解压场景都不被 CRLF 坑)
# —— 只对**副本**做,绝不碰仓里被跟踪的源文件。
# ⚠️ CRLF 压平**保留手写**：shellkit 没有文本/换行原语，perl -pi 是这里的唯一口径
#    （且只作用于 $STAGE 里的**副本** ⇒ 不碰仓里被跟踪的源文件）。
for f in deploy/compose.yml deploy/compose.ghcr-mirror.yml deploy/env.example README.md; do
  cp "$f" "$STAGE/$(basename "$f")"
  perl -pi -e 's/\r$//' "$STAGE/$(basename "$f")"
done
mv "$STAGE/README.md" "$STAGE/安装指引.md"
(cd "$STAGE" && zip -q -r "$OUT_ABS" .)

# 构建后源文件内容必须**逐字节未变**(就地 perl -pi 的机械守卫)
FINGERPRINT_AFTER="$(fingerprint)"
if [ "$FINGERPRINT_BEFORE" != "$FINGERPRINT_AFTER" ]; then
  log_error "构建改动了仓里的源文件(构建前后内容指纹不一致):"
  diff <(printf '%s\n' "$FINGERPRINT_BEFORE") <(printf '%s\n' "$FINGERPRINT_AFTER") >&2 || true
  die 1 "构建中止：仓里的源文件被改动了"
fi
log_ok "构建未改动任何源文件(内容指纹一致)"

log_ok "${OUT}"
# stdout 只给数据：zip 内容清单留在 stdout（人看的目录），日志一律走 stderr。
unzip -l "$OUT_ABS"
