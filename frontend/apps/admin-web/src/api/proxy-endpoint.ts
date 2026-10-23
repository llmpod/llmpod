import { request } from './request'

/** 数据面接入地址（对应后端 ProxyEndpointOut；平台端/租户端登录均可调用） */
export interface ProxyEndpointData {
  /** 数据面接入基址（优先配置的宣告地址，否则 host:port 拼接） */
  base_url: string
  /** 数据面监听主机 */
  proxy_host: string
  /** 数据面监听端口 */
  proxy_port: number
  /** 是否用了显式配置的宣告地址（false=回退 host:port） */
  advertise_url_configured: boolean
}

/** 获取数据面接入地址（GET /api/v1/proxy-endpoint，供在线调试等场景确定「API 打到哪」） */
export function getProxyEndpoint(signal?: AbortSignal): Promise<ProxyEndpointData> {
  return request({ url: '/proxy-endpoint', method: 'GET', signal })
}
