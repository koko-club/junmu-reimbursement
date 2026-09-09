# 在线报销系统部署说明

当前版本：`v1.0.1`。版本变更记录见 [`CHANGELOG.md`](CHANGELOG.md)，版本号记录在 [`VERSION`](VERSION)。

适用于 macOS 本地运行和群晖 DS925+ 的 DSM Container Manager，群晖镜像平台为 `linux/amd64`。默认端口 `8800`，仅在可信局域网部署，不做公网端口转发。

首次访问 `/setup` 创建管理员；普通用户注册后由管理员审批。每位用户只能访问自己的报销记录，删除后进入回收站，保留 30 天。首次管理员创建应由部署人员在开放局域网访问前完成。首次访问根地址会引导进入初始化页面。

## macOS 本地运行

安装 Python 3.12 或更新版本和 [LibreOffice](https://www.libreoffice.org/download/download-libreoffice/)。源码包不附带 Python 或 LibreOffice。在完整项目目录执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sh start_reimbursement_tool.sh --open-browser
```

也可双击 `start_reimbursement_tool.command`；如被系统阻止，用右键“打开”。启动器优先使用项目 `.venv`，检查 Python 版本、openpyxl 和 Pillow，等待健康检查成功后打开 [本机页面](http://127.0.0.1:8800)。终端保持打开，按 Ctrl+C 停服。依赖版本锁定在 `requirements.txt`。

LibreOffice 通常位于 `/Applications/LibreOffice.app/Contents/MacOS/soffice`。发现失败时明确指定：

```sh
APP_SOFFICE_PATH=/Applications/LibreOffice.app/Contents/MacOS/soffice sh start_reimbursement_tool.sh
```

默认监听所有本机网络接口（`0.0.0.0`）以便局域网访问。在 macOS 防火墙中允许 Python 接收连接后，可访问 `http://电脑局域网IP:8800`；如需仅本机访问，执行 `APP_HOST=127.0.0.1 sh start_reimbursement_tool.sh`。

## 构建和离线交付

构建机须已启动 Docker，并安装 Compose 和 buildx 插件；Apple Silicon 构建机也必须构建 `linux/amd64`。

```sh
sh scripts/build-docker.sh
docker image inspect --format '{{.Os}}/{{.Architecture}}' reimbursement-system:latest
docker run --rm --platform linux/amd64 -e REQUIRE_PDF_TESTS=1 reimbursement-system:latest sh -c 'python -m pip install --user -r requirements-test.txt && python -m unittest tests.test_docker_assets tests.test_pdf_regression -v'
sh scripts/export-docker.sh
sh scripts/package-synology.sh
```

强制容器验收要求非 root、amd64、LibreOffice、Noto 中文字体和真实 PDF 测试，缺少组件必须失败。此检查需要访问 Python 包源以安装测试依赖；生产启动不需要联网。构建及 PDF 验收成功才可认定镜像可交付。

输出为 `dist/reimbursement-system-linux-amd64.tar.gz` 和 `dist/reimbursement-system-synology.tar.gz`。交付包包含镜像、`compose.yaml`、`.env.example`、`scripts/backup.sh`、备份/恢复工具及本说明。已有同名归档会报错；请改名保留旧版本后重新执行。群晖包的脚本仍在 `scripts/` 中，不要移到根目录。

没有 Docker 的机器可先制作 `dist/reimbursement-system-source.tar.gz`：

```sh
sh scripts/package-synology.sh --source
```

源码包不能离线导入 Container Manager，也不代表镜像已构建。解压到有 Docker 的构建机后执行上述构建、验收和导出步骤。交付包不包含 `.env`、数据、备份、生成孤儿文件或本机运行时。

## 群晖 DSM Container Manager

1. 安装 Container Manager。解压群晖交付包，将内容放到 `/volume1/docker/reimbursement`，保留 `scripts/` 子目录。
2. 在 File Station 创建 `/volume1/docker/reimbursement/data` 与 `/volume1/docker/reimbursement/backups`。不要把数据目录放进容器的可写层。
3. SSH 登录 NAS，用 `id 部署用户名` 查询 uid/gid，复制 `.env.example` 为 `.env`，填写真实值：

```dotenv
PUID=1026
PGID=100
DATA_PATH=/volume1/docker/reimbursement/data
BACKUP_PATH=/volume1/docker/reimbursement/backups
```

上面的 1026/100 只是示例，以 `id` 输出为准。两个目录必须不同且不能相互包含。首次部署为这两个新建目录赋予容器用户权限（替换 uid/gid）：

```sh
sudo chown 1026:100 /volume1/docker/reimbursement/data /volume1/docker/reimbursement/backups
sudo chmod 700 /volume1/docker/reimbursement/data /volume1/docker/reimbursement/backups
```

4. Container Manager 的“映像 → 新增 → 从文件添加”选择 `reimbursement-system-linux-amd64.tar.gz`。若 DSM 版本不接受 gzip，先解压为 `.tar` 再导入。导入后确认标签 `reimbursement-system:latest`。SSH 的等效命令：

```sh
cd /volume1/docker/reimbursement
gzip -dc reimbursement-system-linux-amd64.tar.gz | sudo docker load
sudo docker compose config
sudo docker compose up -d --wait --wait-timeout 120
```

5. 可在 Container Manager“项目”里选择现有目录及 `compose.yaml` 创建项目；确保读取同目录 `.env`。开放 DSM 防火墙 TCP 8800，仅允许办公网段访问。访问 `http://NAS局域网IP:8800`，由部署人员完成首次管理员初始化后再交给用户。

Compose 固定服务名 `reimbursement`，映射 `8800:8800`，重启策略 `unless-stopped`，通过 `/api/health` 检查服务。默认使用局域网 HTTP，所以 `APP_COOKIE_SECURE=false`。此部署说明不涵盖公网接入。

## 停服备份

`data/database/app.db` 保存账户、会话、配置和文件归属，`data/app-secret` 保存原始 32 字节密钥，`data/users/用户ID/记录UUID/` 保存 Excel/PDF。必须一起备份，不可只复制 SQLite 主文件或重新生成密钥。

```sh
cd /volume1/docker/reimbursement
sudo sh scripts/backup.sh
```

脚本停止 `reimbursement`，运行一次 `python backup.py --data-dir /data --backup-dir /backups`，退出时无论备份成功或失败都重启服务并等待健康检查。任务调度器可定期执行该命令；避免同时启动两个备份任务。运行期间不要通过其他进程或共享目录写入数据。

输出 `backups/reimbursement-UTC时间.tar.gz`，权限为 0600。工具通过 SQLite backup API 建立一致数据库快照，再收入数据库登记的正常记录和回收站文件、密钥以及 `manifest.json` 的 SHA256 清单。归档原子发布且不覆盖旧归档。未登记的生成孤儿、临时文件、无数据清理墓碑不收入归档。

如报错 `purge_claim`，说明有永久删除任务尚未完成，其隔离目录可能已缺少部分文件。备份会拒绝生成不完整恢复点；让服务启动完成清理，检查日志，确认无存储权限问题后停服重试。勿手工删除数据库记录或 `.reimbursement-purge-*` 隔离目录。

保留多份备份并另存到另一台设备。归档含账户和报销资料，应限制读取权限。SHA256 清单用于检测损坏，不提供加密或来源签名。

## 校验及恢复

先校验归档，校验包括 SHA256、路径、普通文件类型、数据库完整性与文件归属；禁止直接对未校验的归档执行 `tar -x`。恢复必须停服并使用尚不存在的新目录，工具不会合并或覆盖现有数据。以下从备份恢复到当前数据挂载点中的 `restored` 子目录：

```sh
cd /volume1/docker/reimbursement
sudo docker compose stop reimbursement
sudo docker compose run --rm --no-deps -T reimbursement python backup.py --verify /backups/reimbursement-20260909T080706Z.tar.gz
sudo docker compose run --rm --no-deps -T reimbursement python backup.py --restore /backups/reimbursement-20260909T080706Z.tar.gz --data-dir /data/restored
```

将示例归档名替换为真实文件名。任一步失败都保留原数据，先检查错误再决定是否重启旧服务。成功后将 `.env` 的 `DATA_PATH` 改为 `/volume1/docker/reimbursement/data/restored`，原目录保留作回滚；后续恢复应选择另一个不存在的路径。备份目录仍设为 `/volume1/docker/reimbursement/backups`。

```sh
sudo docker compose up -d --force-recreate --wait --wait-timeout 120
```

恢复后检查管理员登录、普通用户历史、回收站以及各一份 Excel/PDF 下载。账户、密钥和文件都来自同一归档。也可在有 Python 3.12+ 的离线机器上执行 `python3 backup.py --verify 归档路径` 或 `--restore 归档路径 --data-dir 新目录`，只需同目录 `database.py`，无第三方依赖。

## 升级与回滚

升级前先运行停服备份并记录归档名，保留旧镜像标签，例如 `docker tag reimbursement-system:latest reimbursement-system:previous`。导入新镜像后执行 `docker compose up -d --force-recreate --wait --wait-timeout 120`。确认账户、历史和 PDF 生成正常后再清理旧镜像。

回滚时停服，把 `reimbursement-system:previous` 重新标记为 `reimbursement-system:latest`。若新版本已迁移数据库，必须按上节恢复升级前的整份备份到新目录并更新 `DATA_PATH`，再启动旧镜像；不要让旧程序直接读写已升级的数据库。

## 排查

- 页面打不开：执行 `docker compose ps`、`docker compose logs --tail 100 reimbursement`，检查 8800 是否冲突、NAS IP、DSM 防火墙及是否处于同一局域网。
- 容器不健康：检查 `/api/health` 和日志；Compose 版本需支持 `up --wait`。旧 DSM 插件不支持时应更新 Container Manager 后再使用自动备份脚本。
- 权限不足：核对 `.env` 的 PUID/PGID 与数据/备份目录所有者及 DSM ACL。不要使用 `chmod 777`；不要把容器改为 root 来绕过问题。
- PDF 缺字或生成失败：检查 LibreOffice 与 Noto 中文字体，执行上面的强制容器 PDF 测试；镜像中转换路径固定为 `/usr/bin/soffice`。
- 用户无法登录：待审批或已停用账户需管理员处理。普通用户新登录会使其旧会话失效；重置密码后需更改临时密码。
- 备份失败：检查可用空间、文件权限和缺失文件；`purge_claim` 按停服备份章节处理。归档名冲突时等待下一秒重试或选另一备份目录，已有归档不会被覆盖。

旧版本 `generated` 文件不自动迁移到用户历史，不要把旧目录直接混入 `data/users`。模板保持在 `resources/差旅报销单模板.xlsx`，修改模板前另存原文件并验证实际 PDF。
