import { createRequest } from '@repo/http-client'
import { message } from '@repo/hooks'

export const { request } = createRequest({
  baseURL: import.meta.env.VITE_AUTH_API_BASE_URL,
  // portal 仅负责登录换 token，自身不持有访问令牌
  getToken: () => undefined,
  onError: (msg) => {
    message.error(msg)
  },
})
