import type { RouteRecordRaw } from 'vue-router'

export const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/login/index.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'dispatch',
    component: () => import('@/views/dispatch/index.vue'),
  },
  { path: '/:pathMatch(.*)*', redirect: { name: 'dispatch' } },
]
