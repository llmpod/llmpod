.PHONY: dev dev-proxy dev-node set-ip test test-e2e lint format setup db-reset db-migrate db-revision bump-version deps-up deps-down docker-build push push-version node-up node-build-up frontend-install frontend-build frontend-dev

DC_DEPS := docker compose -f docker/docker-compose.deps.yml --env-file docker/.env.deps

# 版本号唯一来源：llmpod/__init__.py 的 __version__（pyproject 静态版本由 bump-version 同步）
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' llmpod/__init__.py)

# 发版改版本号（日历版本号 yy.m.d；当天追加发布 yy.m.d.n）：
# 同步 __init__ 与 pyproject 两处并刷新 uv.lock。
# -test 不是合法 PEP 440（uv lock 拒绝），test 通道只改 __init__.py——
# 运行时版本唯一来源是它，pyproject 留在基号不影响构建与运行。
# 预发布迭代用四段号 + 裸后缀（26.9.23.1-beta → 26.9.23.2-beta）：第四段
# 参与版本比较，同日迭代抬第四段才能被客户端判为新版（亮红点）；后缀只
# 选通道（-beta 内测 / -test 测试），不带序号。三段+后缀（26.9.23-beta）
# 与同号裸三段比较相等，站点发布时会拒绝。
# 用法: make bump-version V=26.9.3   /   make bump-version V=26.9.23.2-beta
bump-version:
	@if [ -z "$(V)" ]; then echo "用法: make bump-version V=26.9.3"; exit 1; fi
	sed -i 's/^__version__ = ".*"/__version__ = "$(V)"/' llmpod/__init__.py
	@if ! echo "$(V)" | grep -q -- '-test$$'; then \
	  sed -i '0,/^version = ".*"/s//version = "$(V)"/' pyproject.toml && uv lock; \
	fi
	@echo "版本已改为 $(V)（llmpod/__init__.py$$(echo "$(V)" | grep -q -- '-test$$' || echo ' + pyproject.toml + uv.lock')）"

# ===== 前台三端（热重载，Ctrl+C 退出）=====

# 管理端 manager+webserver（8080）
dev: .env
	uv run uvicorn llmpod.app:app --host 0.0.0.0 --port 8080 --reload

# API 网关 proxyserver（8090）
dev-proxy: .env
	uv run python -m llmpod proxyserver

# 节点代理（宿主机直跑，热重载；连本机 manager，需 manager 已起过生成 data/node_token）
dev-node: .env
	uv run python -m llmpod node

# ===== 组网：一键改 manager 对外地址 =====
# 自动探测本机 IPv4（tailscale 优先），一并写对 LLMPOD_NODE_MANAGER_IP /
# _IP_LAN / _MULTINODE_SAME_SUBNET_PREFIX 三个键（.env 与 docker/.env）。
# 改完需重启管理端才生效。
#   make set-ip                 自动探测（有 tailscale 就用它，内网进候选）
#   make set-ip IP=10.0.0.5     指定主地址
#   make set-ip RESTART=1       改完顺带重启管理端并等 /health
set-ip: .env
	@RESTART="$(RESTART)" scripts/set_manager_addr.sh $(IP)

# ===== 前端（frontend/ 源码构建；需 Node>=20 + pnpm>=10）=====

# 安装前端依赖（幂等可重复）
frontend-install:
	cd frontend && pnpm install

# 构建三端(portal/tenant/admin)并聚合覆盖到 llmpod/static/；docker-build COPY llmpod/ 时随之打入镜像
frontend-build: frontend-install
	cd frontend && pnpm build
	rm -rf llmpod/static
	mkdir -p llmpod/static
	cp -fR frontend/dist/. llmpod/static/
	@echo "frontend assets synced -> llmpod/static/"

# 前端本地开发（三端 dev server）；后端地址配置 frontend/.env.local，参考 frontend/.env.example
frontend-dev:
	cd frontend && pnpm dev

# ===== 测试 / 质量 =====

test:
	uv run pytest tests/ -v

test-e2e:
	bash tests/e2e/run.sh

lint:
	uv run ruff check llmpod/ scripts/ tests/ alembic/

# 全仓一次成型（llmpod/scripts/tests/alembic）；之后日常只跑增量文件，
# 保持与本次统一的风格基线——make format 永远是幂等的全量口径
format:
	uv run ruff format llmpod/ scripts/ tests/ alembic/

# ===== 首次一键初始化（装依赖 + 起依赖栈 + 建表；幂等可重复）=====
setup: .env docker/.env.deps
	uv sync --all-extras
	@$(MAKE) --no-print-directory deps-up
	uv run python scripts/init_db.py
	@echo "setup complete. Run 'make dev' / 'make dev-proxy' / 'make dev-node'"

# ===== 数据库 =====

# 删库重建（开发，不可逆）：drop 库 → 建表 + 种子，一条命令走完（不动 DB 用户）
db-reset:
	@echo "WARNING: this will DROP the entire database, then recreate + seed. Stop services first (前台 Ctrl+C 或 make deps-down)。"
	@uv run python scripts/db_reset.py
	@uv run python scripts/init_db.py

# ===== 数据库迁移（Alembic；建库/种子仍走 init_db.py / make setup） =====

# 手动把存量库升到最新迁移（正常无需手动：容器 entrypoint 每次启动幂等执行）
db-migrate:
	uv run alembic upgrade head

# 模型变更后生成迁移（改完 llmpod/models/* 后执行；生成后务必人工检查）
db-revision:
	@if [ -z "$(MSG)" ]; then echo "用法: make db-revision MSG='add xxx column'"; exit 1; fi
	uv run alembic revision --autogenerate -m "$(MSG)"

# ===== 本地开发依赖栈（PG+Redis，长期驻留）=====

# 起依赖栈（只起 PG/Redis，不建表；建表走 make setup）
deps-up: .env docker/.env.deps
	$(DC_DEPS) up -d --wait
	@echo "deps ready (PG=127.0.0.1:35432 Redis=127.0.0.1:26739)。建表用 make setup"

# 拆栈（保留数据卷；清卷用 docker compose ... down -v）
deps-down:
	$(DC_DEPS) down

# ===== 镜像构建 / 发布 =====

# 构建镜像（本地 node 容器 / 推 registry 前）；APP_VERSION 进 OCI label
docker-build:
	docker build -f docker/Dockerfile --build-arg APP_VERSION=$(VERSION) -t llmpod:latest .

# 推镜像（本机 docker push 直连被墙，走 proxy-push/skopeo 经代理）。
# latest 与版本 tag 同仓库双推：检测更新按日历版本号比较版本 tag，发版必须推。
# 切私有 registry 改 PUSH_REGISTRY（勿只改一处——版本 tag 推错仓库会让新装节点
# 拉不到同代镜像）。先构建再推：直接推会把上一次构建的旧镜像打成新版本 tag。
PUSH_REGISTRY ?= docker.io/btpanel/llmpod
push push-version: docker-build
	proxy-push llmpod:latest $(PUSH_REGISTRY):latest
	proxy-push llmpod:latest $(PUSH_REGISTRY):$(VERSION)

# ===== 节点容器（本地 docker 起 node，连本机 manager ws://localhost:8080）=====
# 与 dev-node 互斥：都代表「本机这个节点」，共享 data/node_id + data/node_token，
# 同时跑会争抢 node_id 被 manager 踢线。NVIDIA 机自动加 --gpus all；Ascend 本地
# 接入走正式引导脚本（curl | bash）更省事。

# 用现有镜像 docker run 起 node
node-up: .env
	@docker rm -f llmpod-node 2>/dev/null || true
	docker run -d --name llmpod-node --restart unless-stopped --network host --no-healthcheck \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-v $$(pwd)/data:/app/data \
		-e LLMPOD_NODE_CLUSTER_ID=$$(grep '^LLMPOD_NODE_CLUSTER_ID=' .env | cut -d= -f2-) \
		$$(m=$$(grep -s '^LLMPOD_NODE_MANAGER_URL=' .env | cut -d= -f2-); [ -n "$$m" ] && echo "-e LLMPOD_NODE_MANAGER_URL=$$m") \
		$$(command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 && echo --gpus all) \
		llmpod:latest node

# build 后用新镜像 docker run 起 node
node-build-up: docker-build
	@$(MAKE) --no-print-directory node-up

# env 文件幂等生成（make 文件依赖：不存在才从 example 拷，存在则跳过）
.env:
	cp .env.example .env

docker/.env.deps:
	cp docker/.env.deps.example docker/.env.deps
