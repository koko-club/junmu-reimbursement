# JM 群晖 Docker 部署设计

## Goal

将当前工作树版本 `v1.0.2` 部署到 JM 群晖 DSM Container Manager，镜像、容器和交付资料使用 `junmu-reimbursement` 命名；容器内外均监听并映射 TCP `8800`；应用代码进入镜像，用户数据留在群晖的持久化目录 `/volume1/docker/baoxiao`，并支持保留旧版本进行升级和回滚。

## Scope and Data Boundary

- 镜像包含应用源码、`VERSION`、依赖、模板、HTML/CSS/JavaScript、LibreOffice 和中文字体。
- 本机 `data/`、SQLite 文件、WAL/SHM 文件、`app-secret`、用户上传文件、生成的 Excel/PDF、备份、`.env`、虚拟环境和缓存不得进入 Docker build context、镜像或首次交付包。
- 群晖创建两个互不包含的宿主目录：
  - `/volume1/docker/baoxiao/data` 挂载到容器 `/data`
  - `/volume1/docker/baoxiao/backups` 挂载到容器 `/backups`
- `/data` 整体持久化，包含 `database/app.db`、SQLite WAL/SHM、`app-secret`、`tmp/` 和 `users/`；不能只挂载 `users/`。
- 首次部署使用群晖新建的空 `data` 目录，不迁移当前 Mac 工作区的用户数据。若以后需要迁移，必须使用停服后的 `backup.py` 完整归档和恢复流程。

## Naming and Versioning

- `VERSION` 是唯一版本源，当前值为 `v1.0.2`，构建脚本必须校验其为单行 semver。
- 镜像名固定为 `junmu-reimbursement:<VERSION>`，例如 `junmu-reimbursement:v1.0.2`；镜像写入 `org.opencontainers.image.version` 等 OCI 标签。
- Compose 服务名固定为 `junmu-reimbursement`。
- 当前容器名为 `junmu-reimbursement-v1.0.2`，使运行中的容器可直接识别版本。Compose 项目名固定为 `junmu-reimbursement`，升级时先停掉当前项目容器，再启动新版本容器，避免两个版本争用 `8800`。
- 交付目录按版本保存，例如 `/volume1/docker/baoxiao/releases/v1.0.2`；旧镜像标签和旧发布目录在验证新版本前保留。

## Runtime and Upgrade Flow

1. 在构建机或 JM 群晖上使用 `linux/amd64` 构建并导入 `junmu-reimbursement:v1.0.2`。
2. 在群晖创建目录并按实际部署用户设置 `PUID`/`PGID` 和权限；不把整个 `/volume1/docker/baoxiao` 挂载到 `/data`，以免与备份目录重叠。
3. 复制版本交付目录中的 `.env.example` 为 `.env`，确认 `APP_VERSION=v1.0.2`、`DATA_PATH=/volume1/docker/baoxiao/data`、`BACKUP_PATH=/volume1/docker/baoxiao/backups`。
4. 首次启动前确认目标 `data` 目录为空或明确属于本应用；若存在未知内容，停止并人工确认，不覆盖。
5. 使用 `docker compose up -d --wait --wait-timeout 120` 启动，健康检查访问 `/healthz`，不是 `/api/health`。
6. 升级前运行停服备份并验证归档；导入新版本镜像后，在新发布目录执行同一 Compose 项目的 `down --remove-orphans`，更新 `APP_VERSION`，再 `up -d --force-recreate --wait`。固定的 `DATA_PATH`/`BACKUP_PATH` 不变。
7. 验证登录、历史、Excel/PDF 下载和 `/healthz` 后才清理旧容器；保留旧镜像用于回滚。若新版本已经迁移数据库，回滚必须把升级前完整备份恢复到新的 data 目录，不能让旧代码直接读升级后的数据库。

## Verification Contract

静态测试必须覆盖：

- `VERSION`、镜像 tag、容器名和交付归档名一致；没有生产用 `latest` 或旧 `reimbursement-system` 标识。
- Compose 解析后包含 `8800:8800`、`APP_HOST=0.0.0.0`、`APP_PORT=8800`、两个绝对持久化挂载、`unless-stopped`、非 root 用户和 `/healthz` 健康检查。
- 构建/打包脚本从 `VERSION` 读取版本，且不把 `data/`、备份、密钥或数据库复制到包中。
- 备份脚本使用服务名 `junmu-reimbursement`，即使备份命令失败也会重启并等待健康状态。

远端验收必须记录：

- `docker image ls`/`docker inspect` 显示 `junmu-reimbursement:v1.0.2` 和 OCI 版本标签。
- `docker ps` 显示 `junmu-reimbursement-v1.0.2`、`0.0.0.0:8800->8800/tcp` 和 `healthy`。
- `docker inspect` 显示 `/volume1/docker/baoxiao/data` 与 `/volume1/docker/baoxiao/backups` 的正确挂载，以及非 root UID/GID。
- 群晖本机或局域网请求 `http://<群晖IP>:8800/healthz` 返回 HTTP 200 和 `{"status":"ok"}`。

## Alternatives Considered

- **在群晖直接从源码构建**：不需要在 Mac 安装 Docker，但构建过程依赖群晖网络和资源，交付可重复性较弱。
- **本机/专用构建机生成 amd64 镜像归档后导入**：镜像可审计、离线导入稳定，但当前 Mac 没有可用 Docker，需要额外构建环境。
- **推荐方案**：交付脚本同时支持两者；本次优先使用已配置的 JM 群晖 Docker 环境直接构建或导入版本化镜像，完成远端真实验收，保留版本化归档以便后续离线更新。
