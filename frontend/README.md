# llmpod 前端

pnpm + Turborepo Monorepo，包含平台入口 `portal-web`、租户后台 `tenant-web` 与运营后台 `admin-web`，共享包 `@repo/hooks`、`@repo/naive-ui`。

## 环境要求

- Node >= 20
- pnpm >= 10

## 安装

```bash
pnpm install
```

## 开发

开发前先在仓库根目录准备 `.env.local`（参考 `.env.example`），配置控制平面后端地址：

```bash
API_TARGET=http://127.0.0.1:8080
```

`API_TARGET` 仅被 dev server 的接口代理消费，缺失时 `pnpm dev:*` 会直接报错退出；`pnpm build` 不依赖它，无需配置。

```bash
pnpm dev:portal   # http://localhost:5175/
pnpm dev:tenant   # http://localhost:5173/tenant/
pnpm dev:admin    # http://localhost:5174/admin/
```

## 构建

```bash
pnpm build:portal
pnpm build:tenant
pnpm build:admin
pnpm build        # 构建全部应用
```

`pnpm build` 会先构建三个 app(产物在各 `apps/*-web/dist`),再聚合到根 `dist/`:`dist/`(portal)+ `dist/tenant/` + `dist/admin/`,各 app 资源路径已按自身 base 自洽。如需单独执行聚合可用 `pnpm build:aggregate`(要求三个 app 均已构建,缺任一产物会直接失败且不动现有 `dist/`)。

部署网关分流:`/` → `dist/`(portal)、`/tenant/*` → `dist/tenant/`、`/admin/*` → `dist/admin/`,三者分别配置 SPA 回退。

## 检查

```bash
pnpm lint
pnpm typecheck
pnpm format
```

## 目录

- `apps/portal-web`：平台入口（访问前缀 `/`，选择进入租户或运营后台）
- `apps/tenant-web`：租户后台（访问前缀 `/tenant`）
- `apps/admin-web`：运营后台（访问前缀 `/admin`）
- `packages/hooks`：axios 封装与公共逻辑
- `packages/naive-ui`：Naive UI Provider、主题、样式
