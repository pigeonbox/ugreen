# UPK 二期路线(绿联 UGOS Pro 应用中心)

v0.1 的交付形态是「Docker → 项目 → 粘贴 compose」(见仓库根 README)。本目录是升级到
**UPK 应用包**(应用中心一键安装)的骨架——UPK 内嵌镜像 tar,安装时不需要拉
ghcr,天然规避国内网络问题,是体验上限形态。

## 前置事实(2026-10 调研,来源见仓库根 README「参考」)

- UPK 由官方工具 **ugcli** 打包(`ugcli check` → `ugcli pack --arch all --build N`),官方下载:
  `https://osswaf.ugnas.com/pro/ugcli/download/ugcli-v1.1.0.13-linux-amd64`
  (sha256 `c455dd38def630db2566ae32d0850230ee89abbc948bd1df4819364e88b77713`)
- **正式包必须在 Linux 上打包**(保证 Unix 执行位)⇒ **GitHub Actions 的 `ubuntu-latest`
  就是那台 Linux x86_64 打包机**(官方只提供 linux-amd64 二进制);
  issue #100 里「待 Linux 打包机」的前提**不成立** ⇒ 打包已在 release 流水线里真跑
- **sideload(手动安装)门槛**:需向绿联申请设备开发者授权(邮件序列号/MAC/管理员名 →
  回发 `ugdev.sig` → 应用中心完成授权,UGOS 1.16.0+ 走「应用中心→设置→应用开发设置」)
- **上架应用中心**:2026-05 起个人开发者可入驻,邮件商务邮箱提交自测报告+安装包+承诺函,
  官方审核后统一上架(参考 KOLFlow 的 docs/qa/ 送审材料结构)

## 官方口径实测(2026-11-02,用官方 ugcli v1.1.0.13 逐条实测,非文档转述)

| 约束 | 官方报错原文 | 谁能判出来 |
|---|---|---|
| `rootfs_common` 只允许 `icon.png` + `docker-compose.yaml` | `only [icon.png, docker-compose.yaml] are allowed for docker app` | check / pack |
| `rootfs_<arch>/images` 只允许 tar | `only tar files are allowed for docker app` | check / pack |
| 参数文案必须 `i18n:` 且每语言有 `name`+`description` | `'parameters[0].i18n' is required` | check / pack |
| `desc:` 不是官方字段 | `field desc not found in type project.ParamItem` | pack(check 只报 i18n 缺失) |
| `default_value` 不是官方字段 | `field default_value not found in type project.ParamItem` | **只有 pack** |
| docker 应用必填 `depend_docker_version`+`depend_fw_version` | `'depend_fw_version' is required` | check / pack |
| compose 变量必须**裸 `${VAR}`** | `variables not set: PB_ADMIN_PASSWORD` | **只有 pack** |
| 官方按 **tar 的 RepoTags** 匹配 compose(不是文件名) | `missing image tar for service 'pigeonbox'` | check / pack |
| tar 架构须与 `rootfs_<arch>` 一致 | `image architecture mismatch (expected: amd64, actual: arm64)` | check / pack |

⇒ **结论:`ugcli check` 会放行 3 类违规 ⇒ 门禁必须跑 `pack`**。
⇒ 本仓两道门禁:
  1. `scripts/check_upk_package.py` —— 自研、离线、**红灯/黄灯**两级(规则 + 变异自证);
  2. `scripts/check_upk_official.py` —— **官方工具**判定(check + pack + 10 条变异自证),
     CI 的 `ubuntu-latest` 上真跑。

## 目录形态

```
upk/
  project.yaml                 # 应用包定义（参数文案用 i18n，无 desc/default_value）
  assets/icon.svg              # 矢量图标（**不能**放 rootfs_common —— 官方 allow-list 只认 png）
  rootfs_common/
    docker-compose.yaml        # 内嵌编排（变量裸 ${VAR}；image 引用 = tar 的 RepoTags）
    icon.png                   # 应用图标（官方必需）
  rootfs_{amd64,arm64}/images/ # 内嵌镜像 tar（CI 导出，不入 git；官方只允许 tar）
```

## 待办

1. ~~`ugcli create` 骨架对齐~~ ✅ **已完成**(2026-11-02):`project.yaml`/布局/compose 全部
   按官方口径实测修正,并加了官方工具门禁
2. ~~CI 出正式包~~ ✅ **已完成**(2026-11-02):release 流水线 `upk` job 在 **Linux runner**
   上真跑官方 `ugcli pack` ⇒ 产出 `*.ugb` 并作为 Release 资产发布
3. **绿联侧送审前置(外部条件,代码侧无欠账)**
   - Docker Hub `pigeonbox` 命名空间**不存在**(2026-11-02 实测:search count=0、
     `pigeonbox/server` object not found)⇒ 送审形态的镜像引用目前只在 ghcr 有货
   - 设备开发者授权 `ugdev.sig`(需绿联侧配合)
   - UGOS Pro 真机验证:装包 → 起服务 → 分享/取件闭环 → 升级覆盖
4. 送审材料:自测报告、承诺函、图标(icon.png 已就位)、四链接合规

## 参考实现

- [Panda-995/KOLFlow](https://github.com/Panda-995/KOLFlow) — `ugreen/` 目录 + 官方 ugcli CI 出包,与本场景最接近
- [runlevel1977-del/Runlevel-UGOS-Apps](https://github.com/runlevel1977-del/Runlevel-UGOS-Apps) — 已上架官方应用中心的独立开发者应用套件
- [ablom888/Ugreen-NAS-Apps-Decoder](https://github.com/ablom888/Ugreen-NAS-Apps-Decoder) — UPK 格式逆向/解包工具
