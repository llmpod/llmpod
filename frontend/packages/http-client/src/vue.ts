import { onScopeDispose, ref, shallowRef, type Ref, type ShallowRef } from 'vue'

import { type HttpError, normalizeHttpError } from './core/errors'

/** `useRequest` 生命周期状态。 */
export type RequestStatus = 'idle' | 'loading' | 'success' | 'error' | 'canceled'

/** `useRequest` 行为配置。 */
export interface UseRequestOptions<TResult> {
  manual?: boolean
  cancelOnDispose?: boolean
  keepPreviousData?: boolean
  injectSignal?: boolean
  onSuccess?(data: TResult): void
  onError?(error: HttpError): void
}

/** `useRequest` 返回的响应式请求控制器。 */
export interface UseRequestReturn<TResult, TArgs extends unknown[]> {
  data: ShallowRef<TResult | undefined>
  error: ShallowRef<HttpError | undefined>
  loading: Ref<boolean>
  status: Ref<RequestStatus>
  execute(...args: TArgs): Promise<TResult | undefined>
  cancel(reason?: string): void
  reset(): void
}

/** 将 Promise 函数包装为可取消、可复用的 Vue 响应式请求状态。 */
export function useRequest<TResult, TArgs extends unknown[]>(
  operation: (...args: TArgs) => Promise<TResult>,
  options: UseRequestOptions<TResult> = {},
): UseRequestReturn<TResult, TArgs> {
  const data = shallowRef<TResult>()
  const error = shallowRef<HttpError>()
  const loading = ref(false)
  const status = ref<RequestStatus>('idle')
  let controller: AbortController | undefined
  let sequence = 0

  const cancel = (reason?: string) => {
    sequence += 1
    controller?.abort(reason)
    controller = undefined
    loading.value = false
    status.value = 'canceled'
  }
  const execute = async (...args: TArgs): Promise<TResult | undefined> => {
    const current = ++sequence
    controller?.abort('由更新的请求替代')
    controller = new AbortController()
    if (!options.keepPreviousData) data.value = undefined
    error.value = undefined
    loading.value = true
    status.value = 'loading'
    try {
      const callArgs = options.injectSignal ? ([...args, controller.signal] as TArgs) : args
      const result = await operation(...callArgs)
      if (current !== sequence) return undefined
      data.value = result
      status.value = 'success'
      options.onSuccess?.(result)
      return result
    } catch (caught) {
      if (current !== sequence) return undefined
      const normalized = normalizeHttpError(caught)
      error.value = normalized
      status.value = normalized.type === 'cancel' ? 'canceled' : 'error'
      if (normalized.type !== 'cancel') options.onError?.(normalized)
      return undefined
    } finally {
      if (current === sequence) loading.value = false
    }
  }
  const reset = () => {
    sequence += 1
    controller?.abort('reset')
    controller = undefined
    data.value = undefined
    error.value = undefined
    loading.value = false
    status.value = 'idle'
  }
  if (options.cancelOnDispose !== false) onScopeDispose(() => cancel('scope disposed'))
  if (options.manual === false) void execute(...([] as unknown as TArgs))
  return { data, error, loading, status, execute, cancel, reset }
}
