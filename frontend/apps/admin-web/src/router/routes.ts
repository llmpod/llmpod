import type { RouteRecordRaw } from 'vue-router'

export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: () => import('@/layouts/BasicLayout.vue'),
    children: [
      { path: '', redirect: { name: 'dashboard' } },
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/dashboard/index.vue'),
      },
      {
        path: 'statistics',
        name: 'statistics',
        component: () => import('@/views/statistics/index.vue'),
      },
      {
        path: 'engine-dashboard',
        name: 'engine-dashboard',
        component: () => import('@/views/engine-dashboard/index.vue'),
      },
      {
        path: 'node-list',
        name: 'node-list',
        component: () => import('@/views/node-list/index.vue'),
      },
      {
        path: 'node-pool',
        name: 'node-pool',
        component: () => import('@/views/node-pool/index.vue'),
      },
      // GPU 页面已隐藏:与节点列表的 GPU 信息重复,入口(菜单/全局搜索)全部下掉,views/gpu 暂留待复用
      {
        path: 'deploy',
        name: 'deploy',
        component: () => import('@/views/deploy/index.vue'),
      },
      // 模型管理已合并为「模型管理」页(deploy)的子 tab,旧路由保留为重定向,
      // 兼容流程引导、仪表盘快捷入口、拓扑跳转、全局搜索等已有 name 跳转;query 原样透传。
      // meta.searchGroup 声明其全局搜索归属「模型管理」,由 BasicLayout 自动派生行尾小字,无需在布局写死
      {
        path: 'model-management',
        name: 'model-management',
        redirect: (to) => ({ name: 'deploy', query: { ...to.query, tab: 'management' } }),
        meta: { searchGroup: 'deploy' },
      },
      // 发布中心:含「模型发布」「供应商管理」两个子 tab(?tab= 驱动)。
      // name 沿用 model-release 以复用后端 /admin/model-release 菜单权限,流程引导等 name 跳转不受影响
      {
        path: 'model-release',
        name: 'model-release',
        component: () => import('@/views/model-release/index.vue'),
      },
      // 租户概览已并入「租户管理」页(顶部统计卡 + 列表),旧路由保留为重定向,
      // 兼容收藏/外链等已有 name 跳转;query 原样透传。meta.searchGroup 声明其
      // 全局搜索归属「租户管理」,由 BasicLayout 自动派生行尾小字
      {
        path: 'tenant-overview',
        name: 'tenant-overview',
        redirect: (to) => ({ name: 'tenant-management', query: to.query }),
        meta: { searchGroup: 'tenant-management' },
      },
      {
        path: 'tenant-management',
        name: 'tenant-management',
        component: () => import('@/views/tenant-management/index.vue'),
      },
      {
        path: 'user',
        name: 'user',
        component: () => import('@/views/user/index.vue'),
      },
      {
        path: 'role',
        name: 'role',
        component: () => import('@/views/role/index.vue'),
      },
      {
        path: 'playground',
        name: 'playground',
        component: () => import('@/views/playground/index.vue'),
      },
      {
        path: 'model-mark',
        name: 'model-mark',
        component: () => import('@/views/model-mark/index.vue'),
      },
      {
        path: 'api-key',
        name: 'api-key',
        component: () => import('@/views/api-key/index.vue'),
      },
      {
        path: 'logs',
        name: 'logs',
        component: () => import('@/views/logs/index.vue'),
      },
      {
        path: 'package',
        name: 'package',
        component: () => import('@/views/package/index.vue'),
      },
      {
        path: 'orders',
        name: 'orders',
        component: () => import('@/views/orders/index.vue'),
      },
      {
        path: 'bills',
        name: 'bills',
        component: () => import('@/views/bills/index.vue'),
      },
      {
        path: 'alarm',
        name: 'alarm',
        component: () => import('@/views/alarm/index.vue'),
      },
      {
        path: 'config',
        name: 'config',
        component: () => import('@/views/config/index.vue'),
        // 「关于」已脱离设置页子 tab,独立为左侧菜单页面;
        // 旧收藏/外链 config?tab=about 重定向过去,其余 query 原样透传
        beforeEnter: (to) => {
          if (to.query.tab !== 'about') return true
          const query = { ...to.query }
          delete query.tab
          return { name: 'about', query }
        },
      },
      {
        path: 'about',
        name: 'about',
        component: () => import('@/views/about/index.vue'),
      },
      {
        path: 'sso',
        name: 'sso',
        component: () => import('@/views/sso/index.vue'),
      },
      // 品牌配置已合并为「设置」页(config)的子 tab,旧路由保留为重定向,兼容收藏/外链等
      // 已有 name 跳转;query 原样透传。meta.searchGroup 声明其全局搜索归属「设置」页
      {
        path: 'brand',
        name: 'brand',
        redirect: (to) => ({ name: 'config', query: { ...to.query, tab: 'brand' } }),
        meta: { searchGroup: 'config' },
      },
      {
        path: 'webhook',
        name: 'webhook',
        component: () => import('@/views/webhook/index.vue'),
      },
      {
        path: 'quick-setup',
        name: 'quick-setup',
        component: () => import('@/views/quick-setup/index.vue'),
      },
    ],
  },
]
