# UPK 二期路线(绿联 UGOS Pro 应用中心)

v0.1 的交付形态是「Docker → 项目 → 粘贴 compose」(见仓库根 README)。本目录是升级到
**UPK 应用包**(应用中心一键安装)的预留骨架——UPK 内嵌镜像 tar,安装时不需要拉
ghcr,天然规避国内网络问题,是体验上限形态。

## 前置事实(2026-10 调研,来源见仓库根 README「参考」)

- UPK 由官方工具 **ugcli** 打包(`ugcli check` → `ugcli pack --arch all --build N`),官方下载:
  `https://osswaf.ugnas.com/pro/ugcli/download/ugcli-v1.1.0.13-linux-amd64`
  (sha256 `c455dd38def630db2566ae32d0850230ee89abbc948bd1df4819364e88b77713`)
- **正式包必须在 Linux 上打包**(保证 Unix 执行位);镜像 tar 放 `rootfs_<arch>/images/`,
  每 tar 单 tag、与 compose 内 tag 一致
- **sideload(手动安装)门槛**:需向绿联申请设备开发者授权(邮件序列号/MAC/管理员名 →
  回发 `ugdev.sig` → 应用中心完成授权,UGOS 1.16.0+ 走「应用中心→设置→应用开发设置」)
- **上架应用中心**:2026-05 起个人开发者可入驻,邮件商务邮箱提交自测报告+安装包+承诺函,
  官方审核后统一上架(参考 KOLFlow 的 docs/qa/ 送审材料结构)

## 待办(二期启动时)

1. `ugcli create com.pigeonbox.app` 生成正规骨架,与本目录字段对齐后 `ugcli check` 校验
   (当前 project.yaml/docker-compose.yaml 未经校验,字段以官方文档为准)
2. CI:多架构 `docker pull --platform` → `docker save` 进 `rootfs_<arch>/images/` →
   `ugcli pack --arch all` → Release 产物 `*.upk` + `SHA256SUMS-UPK`
3. 镜像命名/tag 策略与 ghcr 发布 tag 对齐(禁 latest,版本一一对应)
4. 送审材料:自测报告、承诺函、图标(icon.png 256x256 已就位)、四链接合规

## 参考实现

- [Panda-995/KOLFlow](https://github.com/Panda-995/KOLFlow) — `ugreen/` 目录 + 官方 ugcli CI 出包,与本场景最接近
- [runlevel1977-del/Runlevel-UGOS-Apps](https://github.com/runlevel1977-del/Runlevel-UGOS-Apps) — 已上架官方应用中心的独立开发者应用套件
- [ablom888/Ugreen-NAS-Apps-Decoder](https://github.com/ablom888/Ugreen-NAS-Apps-Decoder) — UPK 格式逆向/解包工具
