<script setup lang="ts">
import { useCachedImage } from '@repo/hooks'
import StarfieldBackground from './StarfieldBackground.vue'

/**
 * 登录页背景容器。
 * 默认使用星空特效;自定义背景按 type 传入 image 或 color 即可,登录页结构无需改动。
 * image 同源地址走 IndexedDB 缓存(useCachedImage),二次进入登录页零网络请求;
 * 跨域外链/data: 直接透传原始地址(不发请求,避免 CORS 报错);同源请求失败降级原始 URL 兜底。
 */
interface Props {
  /** 背景类型:starfield 星空特效 / image 自定义图片 / color 纯色或渐变 */
  type?: 'starfield' | 'image' | 'color'
  /** type 为 image 时的背景图地址 */
  image?: string
  /** type 为 color 时的 background 值(支持纯色、渐变) */
  color?: string
}

const props = withDefaults(defineProps<Props>(), {
  type: 'starfield',
  image: '',
  color: '',
})

const { blobUrl, error } = useCachedImage(() => props.image)
// 同源命中缓存为 objectURL,跨域直接原始地址;同源请求失败(error)降级原始 URL 交给浏览器直接加载;加载中不渲染背景(外层深色底兜底)
const bgImage = computed(() => blobUrl.value ?? (error.value ? props.image : undefined))
</script>

<template>
  <div class="absolute inset-0 overflow-hidden" aria-hidden="true">
    <StarfieldBackground v-if="type === 'starfield'" />
    <div
      v-else-if="type === 'image' && bgImage"
      class="h-full w-full bg-center bg-cover"
      :style="{ backgroundImage: `url('${bgImage}')` }"
    />
    <div
      v-else-if="type === 'color' && color"
      class="h-full w-full"
      :style="{ background: color }"
    />
  </div>
</template>
