import { cp, rm, mkdir, readdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

// 聚合三个 app 的构建产物到根 dist/
// 结构:dist/ (portal,base '/') + dist/tenant/ + dist/admin/
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const distRoot = resolve(root, 'dist')

const apps = [
  ['portal-web', ''], // portal → dist 根
  ['tenant-web', 'tenant'],
  ['admin-web', 'admin'],
]

// 源产物校验前置:任一 app 缺 dist 即整体失败,不做部分聚合
// (部分聚合会产出缺一端路由的站点,经网关分流后另一端整站 404)
const missing = apps.filter(([app]) => !existsSync(resolve(root, `apps/${app}/dist`)))
if (missing.length > 0) {
  for (const [app] of missing) {
    console.error(`[error] apps/${app}/dist 不存在,请先执行 pnpm build:${app.replace('-web', '')}`)
  }
  process.exit(1)
}

// 全部源就绪后才删除旧 dist:校验失败时本地上一份可用产物不受影响
await rm(distRoot, { recursive: true, force: true })
await mkdir(distRoot, { recursive: true })

for (const [app, sub] of apps) {
  const src = resolve(root, `apps/${app}/dist`)
  if (sub === '') {
    const entries = await readdir(src, { withFileTypes: true })
    for (const e of entries) {
      await cp(resolve(src, e.name), resolve(distRoot, e.name), { recursive: true })
    }
    console.log(`[copy] apps/${app}/dist → dist/`)
  } else {
    await cp(src, resolve(distRoot, sub), { recursive: true })
    console.log(`[copy] apps/${app}/dist → dist/${sub}`)
  }
}

console.log(`\n聚合完成 → ${distRoot}`)
console.log('结构:dist/ (portal) + dist/tenant/ + dist/admin/')
