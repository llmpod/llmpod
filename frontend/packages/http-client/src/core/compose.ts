import type { HttpContext, HttpMiddleware, HttpNext } from './types'

/** 将中间件组合为洋葱模型执行器。 */
export function compose(middlewares: HttpMiddleware[]) {
  return async (context: HttpContext, terminal: HttpNext): Promise<void> => {
    let lastIndex = -1
    const dispatch = async (index: number): Promise<void> => {
      if (index <= lastIndex) throw new Error('next() called multiple times')
      lastIndex = index
      const middleware = middlewares[index]
      if (middleware) await middleware(context, () => dispatch(index + 1))
      else await terminal()
    }
    await dispatch(0)
  }
}
