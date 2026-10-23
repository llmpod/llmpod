import { createRouter, createWebHistory } from 'vue-router'
import { setupGuard } from './guard'
import { routes } from './routes'

export const router = createRouter({
  history: createWebHistory('/'),
  routes,
})

setupGuard(router)
