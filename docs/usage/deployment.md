# LLMPod Docker 部署

[文档目录](../README.md) · [API 接入](api.md)

推荐使用安装命令：

```bash
curl -fsSL https://llmpod.ai/src/download/install.sh -o install.sh && sudo bash install.sh
```

需要自行部署时，按下面的步骤操作。在 Linux 服务器上准备好 Docker Engine 和支持 `docker compose` 命令的 Compose 插件。使用发布包中的 Compose 文件部署 PostgreSQL、Redis、管理端和 API 网关，无需克隆源码或构建镜像。

```bash
docker info >/dev/null
docker compose version
```

## 1. 下载配置

```bash
mkdir -p ~/llmpod && cd ~/llmpod
curl -fsSL https://llmpod.ai/src/download/latest/docker-compose.yml -o docker-compose.yml
curl -fsSL https://llmpod.ai/src/download/latest/.env.example -o .env.example
cp .env.example .env
```

`latest` 是稳定版。需要固定版本时，将两条下载地址中的 `latest` 改为 `version/<版本号>`，并把 Compose 文件中管理端、API 网关的镜像标签一并改为该版本号。

## 2. 配置 `.env`

编辑刚创建的 `.env`。至少替换模板中的 PostgreSQL、Redis 密码，并填写节点能够访问的管理端地址。

| 变量 | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD`、`REDIS_PASSWORD` | 分别设置不同的强密码；不要沿用模板示例值 |
| `LLMPOD_NODE_MANAGER_IP` | 节点访问管理端的 IP 或域名；使用 HTTPS 反向代理时可填完整的 `wss://` 地址 |
| `WEBSERVER_PORT`、`PROXY_PORT` | 管理端和 API 网关端口，宿主机与容器使用相同端口；模板默认 `18080`、`18090` |
| `HOST_IP` | 对外绑定地址，默认 `0.0.0.0` |
| `MODEL_HOST_DIR` | 宿主机模型目录，默认 `/data/models`；容器内固定为 `/data/models` |
| `LLMPOD_NODE_MANAGER_IP_LAN` | 可选，节点可直连的内网地址，多个地址用逗号分隔 |
| `LLMPOD_PROXY_ADVERTISE_URL` | 可选，通过固定域名或反向代理访问 API 网关时填写完整对外地址 |
| `LLMPOD_ADMIN_USER`、`LLMPOD_ADMIN_PASSWORD` | 可选，仅首次建库时生效；留空则自动生成账号 |

先创建 `MODEL_HOST_DIR` 指向的宿主机目录，并确认容器对它有读写权限。PostgreSQL 和 Redis 默认只在 Compose 网络内使用，不需要开放宿主机端口。

## 3. 启动

在 `~/llmpod` 目录执行：

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env pull
docker compose --env-file .env up -d --no-build
docker compose --env-file .env ps
```

确认管理端、API 网关状态为 `healthy`。如果未设置管理员账号，首次启动后查看自动生成的凭据：

```bash
docker compose --env-file .env exec -T webserver cat /app/data/admin_credentials
```

启动失败时查看日志：

```bash
docker compose --env-file .env logs -f webserver proxyserver
```

## 升级

升级到 WebSocket 请求头鉴权版本时，先在现有管理端运行期间更新所有节点容器，
确认它们使用新镜像重新上线，再升级管理端。更新节点时保留原数据卷中的
`data/node_id` 和共享节点令牌；旧节点缺少请求头，遇到新管理端会在
WebSocket 握手阶段被拒绝，无法再接收自动升级提议。新节点首次登记需要安装命令中
的短期引导令牌；安装时可逐项确认或修改领取时给出的默认参数。

先备份数据库和 `.env`，再下载新版 Compose 文件与配置模板。沿用现有 `.env`，对照新模板补充新增变量；不要用模板覆盖已有密码。

```bash
cd ~/llmpod
docker compose --env-file .env exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > llmpod-$(date +%Y%m%d).sql
cp .env .env.backup-$(date +%Y%m%d)
cp docker-compose.yml docker-compose.yml.backup-$(date +%Y%m%d)

curl -fsSL https://llmpod.ai/src/download/latest/docker-compose.yml -o docker-compose.yml
curl -fsSL https://llmpod.ai/src/download/latest/.env.example -o .env.example
# 对照 .env.example，将新增配置项补入 .env
docker compose --env-file .env config --quiet
docker compose --env-file .env pull webserver proxyserver
docker compose --env-file .env up -d --no-build
docker compose --env-file .env ps
```

数据库迁移会随管理端启动自动执行。数据库结构升级后，不要直接换回旧版镜像。需要固定版本时，升级时同样下载该版本的两份配置文件，并使用相同版本的镜像标签。

`docker compose --env-file .env down` 会停止并删除容器，但保留 PostgreSQL、Redis、管理端的数据卷。不要在需要保留数据时使用 `down -v`；重新部署也应保留原 `.env`，使数据库密码与现有数据卷一致。

## 运维须知：多机部署的互联地址在规划时写定

多机部署的 rendezvous 地址（worker 连 head 的 `master_addr`）在调度规划时写进
各实例的配置快照，续跑与重启不重新解析。因此**变更节点 IP 后，存量多机部署需
restart 触发重规划**（或先停再启动），否则 worker 仍按旧地址连 head，表现为
worker 起不来或互相等待超时。单机部署不受影响。
