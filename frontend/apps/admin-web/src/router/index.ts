import { createRouter, createWebHistory } from 'vue-router'
import { routes } from './routes'
import { setupGuard } from './guard'

export const router = createRouter({
  history: createWebHistory('/admin'),
  routes,
})

setupGuard(router)
