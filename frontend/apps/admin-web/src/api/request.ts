import { createRequest } from '@repo/http-client'
import { goToPortalLogin, message } from '@repo/hooks'
import { useAuthStore } from '@/store/auth'

// 接口报错统一走 @repo/hooks 的 message(挂全局 MessageHost):
// 长错误信息自动换行可读,且超 50 字默认不自动关闭,由用户读完后手动关闭
export const { request } = createRequest({
  baseURL: import.meta.env.VITE_ADMIN_API_BASE_URL,
  getToken: () => useAuthStore().token,
  onUnauthorized: () => {
    useAuthStore().clearToken()
    message.warning('登录已失效,请重新登录')
    goToPortalLogin()
  },
  onForbidden: () => {
    message.error('无操作权限')
  },
  onError: (msg) => {
    message.error(msg)
  },
})
