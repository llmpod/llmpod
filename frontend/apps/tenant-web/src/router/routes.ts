import type { RouteRecordRaw } from 'vue-router'

export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: () => import('@/layouts/BasicLayout.vue'),
    children: [
      { path: '', redirect: { name: 'console' } },
      // 登录后默认跳转 /tenant/dashboard,重定向到控制台
      { path: 'dashboard', redirect: { name: 'console' } },
      {
        path: 'console',
        name: 'console',
        component: () => import('@/views/console/index.vue'),
      },
      {
        path: 'model-mark',
        name: 'model-mark',
        component: () => import('@/views/model-mark/index.vue'),
      },
      {
        path: 'playground',
        name: 'playground',
        component: () => import('@/views/playground/index.vue'),
      },
      {
        path: 'api-key',
        name: 'api-key',
        component: () => import('@/views/api-key/index.vue'),
      },
      {
        path: 'statistics',
        name: 'statistics',
        component: () => import('@/views/statistics/index.vue'),
      },
      {
        path: 'cost',
        name: 'cost',
        component: () => import('@/views/cost/index.vue'),
      },
      {
        path: 'team',
        name: 'team',
        component: () => import('@/views/team/index.vue'),
      },
      {
        path: 'logs',
        name: 'logs',
        component: () => import('@/views/logs/index.vue'),
      },
      {
        path: 'auth',
        name: 'auth',
        component: () => import('@/views/auth/index.vue'),
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
    ],
  },
]
