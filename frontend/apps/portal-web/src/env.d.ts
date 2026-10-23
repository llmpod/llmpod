/// <reference types="vite/client" />
declare module '*.scss'
declare module 'virtual:uno.css'
declare module '@repo/naive-ui/styles'

interface ImportMetaEnv {
  readonly VITE_APP_ENV: string
  readonly VITE_TENANT_URL: string
  readonly VITE_ADMIN_URL: string
  readonly VITE_AUTH_API_BASE_URL: string
  readonly VITE_USE_AUTH_MOCK: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
