# PigeonBox UGREEN UGOS Pro

[![CI](https://github.com/pigeonbox/ugreen/actions/workflows/ci.yml/badge.svg)](https://github.com/pigeonbox/ugreen/actions/workflows/ci.yml)
[![Release](https://github.com/pigeonbox/ugreen/actions/workflows/release.yml/badge.svg)](https://github.com/pigeonbox/ugreen/releases)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)

PigeonBox（文件快递柜，匿名口令分享文本/文件）的 **绿联 NAS（UGOS Pro）部署包**：官方 Docker 镜像（`ghcr.io/pigeonbox/server` + `frontend`）的 docker compose 一键粘贴部署。

- UGOS Pro 1281 固件（2024-08）起 **Docker → 项目** 原生支持 compose，粘贴即部署
- 镜像双版本：**ghcr 原版** + **国内加速版**（`ghcr.nju.edu.cn` 前缀，南京大学镜像）
- JWT 密钥首启自动生成并持久化到数据目录，重启/升级不丢
- 开机自启：编排内已置 `restart: unless-stopped`，随 Docker 套件拉起
- 默认关闭开放注册（NAS 场景，管理员建号）

> 系统要求：UGOS Pro（DXP4800/DXP6800/DXP8800 等），Docker 应用可用。
> 绿联官方暂无免审的第三方包格式：UPK（应用中心包）需开发者授权 sideload / 邮件送审上架，
> 为二期路线（见 [upk/](./upk/README.md)）；当前形态零门槛、更新最即时。

## 安装（5 分钟）

1. 下载本仓 [Releases](https://github.com/pigeonbox/ugreen/releases) 的部署包 zip 并解压
   （国内网络选 `compose.ghcr-mirror.yml`，其余步骤相同）
2. UGOS Pro 桌面打开 **Docker** 应用 → 左侧 **项目** → 右上角 **创建**
3. 项目名填 `pigeonbox`（小写），配置来源选「粘贴/编辑 compose」，把 `compose.yml` 全文粘入
4. （可选）把编排里 `FCB_ADMIN_PASSWORD` 一行改为你的管理员密码；不改为默认 `admin/admin123`
5. 点 **部署**（或「应用」），等两个容器 running
6. 浏览器访问 `http://NAS的IP:12345`，默认管理员 `admin/admin123`——**装完先改密码**

### 常用调参（粘贴前直接在 YAML 里改）

| 位置 | 默认 | 说明 |
|---|---|---|
| `ports: "12345:8080"` | `12345` | 对外端口（冲突就换，如 `29345:8080`） |
| `/volume1/docker/pigeonbox/data:/app/data` | 见左 | 数据目录（SQLite+上传文件+JWT 密钥；**备份它=备份全部**） |
| `FCB_ADMIN_PASSWORD` | 空 | 管理员密码（留空=`admin123`） |
| `FCB_USER_ALLOW_REGISTRATION` | `false` | 开放注册开关 |

装好后的日常修改：Docker → 项目 → pigeonbox → 编辑 compose → 重新部署，配置即持久在该项目里。

## ghcr 拉取失败（国内网络）

- 首选：直接用加速版编排 `compose.ghcr-mirror.yml`（镜像源换成 `ghcr.nju.edu.cn` 前缀）
- 或 Docker → 镜像 → 镜像仓库列表 → 新建加速器（官方知识库有图文）
- 兜底：任意外网机器 `docker pull` + `docker save` 出 tar，上传 NAS 后
  **Docker → 本地镜像 → 导入**，再部署项目

## 数据与备份

- 数据全在 `/volume1/docker/pigeonbox/data`（`fileCodeBox.db`、上传文件、`.jwt_secret`）
- 备份 = 停止项目后在文件管理器复制该目录
- 删除项目**不会**删数据目录；彻底清理请手动删除

## 常见问题

- **部署时报端口占用**：换端口（改 `ports` 前半段）重新部署
- **页面 502/容器反复重启**：等 1-2 分钟（后端健康检查通过后前端才放行）；仍异常看 `pigeonbox` 容器日志
- **取件/上传报权限错误**：数据目录属主需与容器内运行身份一致（uid 1000），SSH 执行
  `chown -R 1000:1000 /volume1/docker/pigeonbox/data` 后重新部署项目

## 升级

改 compose 里两处镜像 tag（`server`/`frontend` 的 `:vX.Y.Z`，与 [server 仓 Releases](https://github.com/pigeonbox/server/releases) 对齐）→ 重新部署。数据目录不动，配置/数据全保留。

## 开发与构建

共享资产（`compose.yml` / `env.example` / `compose.ghcr-mirror.yml`）的**真相源在生态主仓 [`deploy/nas/`](https://github.com/pigeonbox/pigeonbox/tree/main/deploy/nas)**：改编排/默认值请改 hub 模板后执行 `bash deploy/nas/sync.sh sync`，**勿直接改本仓这两个文件**——CI 有「与 hub 模板对齐」漂移门禁，模板一动未同步的仓全部变红。跟随 server 新镜像版本走发版列车：hub 仓 `scripts/nas-release-train.sh <镜像tag> --push` 一条命令完成四处钉版+打 tag。
```sh
./scripts/build-zip.sh 0.1.0     # → dist/pigeonbox-ugreen-0.1.0.zip(发布资产)
docker compose -f deploy/compose.yml config -q   # 模板校验
```

打 `v*` tag 自动：组装部署包 → 挂本仓 Release → 回挂生态主仓 `ugreen-v*` Release（需 `UGREEN_PAT`，未配置时 CI 放行失败、本地 `gh release upload` 兜底）。

**真机验证状态**：compose 模板与加速版一致性过 CI；UGOS Pro 真机 UI 粘贴部署验证待补（欢迎反馈 issue）。

## UPK 二期（应用中心形态）

见 [upk/README.md](./upk/README.md)：官方 ugcli 打包、镜像 tar 内嵌、邮件送审上架；
sideload 需设备开发者授权。骨架已就位，未经 `ugcli check` 校验。

## 参考

- [绿联官方:Docker 镜像加速器配置](https://support.ugnas.com/detail/article/zh-CN/297)
- [UGOS Pro 开发者平台](https://developer.ugnas.com)（UPK 格式与 ugcli）
- [Panda-995/KOLFlow](https://github.com/Panda-995/KOLFlow)（官方 ugcli CI 出包先例）
- ghcr 国内加速前缀 `ghcr.nju.edu.cn`（南京大学镜像站,可用性随时间变化）

## License

Apache-2.0（与生态一致，见 [LICENSE](./LICENSE)）。
