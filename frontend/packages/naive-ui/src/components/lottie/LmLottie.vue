<script setup lang="ts">
import type { AnimationItem } from 'lottie-web'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * Lottie 动画容器:lottie-web(svg 渲染器)与动画 json 均按需加载,不进主包。
 * - prefers-reduced-motion:渲染首帧后停住(静态展示,不自动播放)
 * - IntersectionObserver:离开视口自动暂停,回到视口恢复(仅 autoplay 场景)
 * - 加载/渲染失败:维持 fallback 插槽内容不动(淡入层永不点亮)
 * - src 变化不响应,调用方用 :key 重建实例
 * - trigger='hover':不做入场播放,首次指针移入才加载并播放,之后每次移入从头重播、
 *   播完停在终态;触屏(无 hover 语义)保持静帧,永不加载
 */
interface Props {
  /** 动画 json 地址(public 下路径,如 /animations/xxx.json) */
  src: string
  /** 宽度(px 数字或任意 css 长度串,如 '2.8rem'/'100%') */
  width?: number | string
  /** 高度(同 width) */
  height?: number | string
  /** 循环播放,默认 true */
  loop?: boolean
  /** 就绪后自动播放,默认 true(trigger='hover' 由指针移入接管) */
  autoplay?: boolean
  /** svg preserveAspectRatio:meet 完整可见 / slice 铺满裁切,默认 meet */
  fit?: 'meet' | 'slice'
  /** 播放扳机:auto 就绪即播 / hover 仅指针移入时播放 */
  trigger?: 'auto' | 'hover'
}

const props = withDefaults(defineProps<Props>(), {
  width: undefined,
  height: undefined,
  loop: true,
  autoplay: true,
  fit: 'meet',
  trigger: 'auto',
})

const emit = defineEmits<{ ready: []; error: [message: string] }>()

const host = ref<HTMLElement | null>(null)
/** 动画就绪(淡入层点亮用) */
const ready = ref(false)
/** 加载或渲染失败:永久回落 fallback */
const failed = ref(false)

let anim: AnimationItem | null = null
let observer: IntersectionObserver | null = null
/** hover 扳机:首次移入才发起加载(加载后置 true) */
let started = false
let disposed = false

const reducedMotion =
  typeof window !== 'undefined' &&
  !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

/** 播放扳机:hover 需精确指针(触屏无 hover 语义,mouseenter 不可靠),触屏按静帧处理 */
const hoverMode = computed(() => props.trigger === 'hover')
const finePointer =
  typeof window !== 'undefined' &&
  !!window.matchMedia?.('(hover: hover) and (pointer: fine)').matches

/** 从头重播:goToAndStop/play 均不可用于「正在播」的实例(play 只继续、goToAndStop 整帧空白),
 *  统一 stop() 归零再 play() */
function replay() {
  if (!anim) return
  anim.stop()
  anim.play()
}

async function init() {
  try {
    const [{ default: lottie }, response] = await Promise.all([
      import('lottie-web/build/player/lottie_svg'),
      fetch(props.src),
    ])
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const animationData = await response.json()
    if (disposed || !host.value) return
    anim = lottie.loadAnimation({
      container: host.value,
      renderer: 'svg',
      loop: props.loop,
      // hover 扳机一律不自动播:播放全部由 mouseenter 触发,避免加载瞬间抢跑一帧
      autoplay: props.autoplay && !reducedMotion && !hoverMode.value,
      animationData,
      rendererSettings: { preserveAspectRatio: `xMidYMid ${props.fit}` },
    })
    if (reducedMotion || (hoverMode.value && !finePointer)) {
      // lottie 的 svg 渲染器需先跑满一帧才填充形状数据,直接 goToAndStop(0) 会渲染空白;
      // 故先 play,两帧后停在首帧(静态展示,且与未减弱动效用户看到同一帧)
      anim.play()
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          if (!disposed && anim) anim.goToAndStop(0, true)
        }),
      )
    }
    ready.value = true
    emit('ready')
    // 懒加载完成时指针多半仍在热区内(等不到新的 mouseenter):补播这一次,已移开则静候下次移入
    if (hoverMode.value && !reducedMotion && host.value.matches(':hover')) replay()
    if (!hoverMode.value && props.autoplay && !reducedMotion && 'IntersectionObserver' in window) {
      observer = new IntersectionObserver((entries) => {
        if (!anim) return
        if (entries.some((entry) => entry.isIntersecting)) anim.play()
        else anim.pause()
      })
      observer.observe(host.value)
    }
  } catch (error) {
    if (disposed) return
    failed.value = true
    emit('error', error instanceof Error ? error.message : String(error))
  }
}

/** hover 热区入口:首次移入才加载(首屏不拉 lottie chunk),之后每次移入从头重播 */
function handleEnter() {
  if (disposed || reducedMotion || !finePointer) return
  if (!started) {
    started = true
    void init()
    return
  }
  replay()
}

onMounted(() => {
  // hover 扳机把加载推迟到首次指针移入:未 hover 过的页面零 lottie 字节
  if (hoverMode.value) return
  void init()
})

onBeforeUnmount(() => {
  disposed = true
  observer?.disconnect()
  observer = null
  anim?.destroy()
  anim = null
})

const sizeStyle = computed(() => ({
  width:
    props.width === undefined
      ? undefined
      : typeof props.width === 'number'
        ? `${props.width}px`
        : props.width,
  height:
    props.height === undefined
      ? undefined
      : typeof props.height === 'number'
        ? `${props.height}px`
        : props.height,
}))
</script>

<template>
  <div
    class="lm-lottie"
    :style="sizeStyle"
    @mouseenter="handleEnter"
  >
    <!-- fallback:未就绪/失败时占位;就绪后 lottie 绝对定位淡入覆盖其上 -->
    <slot v-if="!ready" />
    <div
      ref="host"
      class="lm-lottie__host"
      :class="{ 'lm-lottie__host--on': ready && !failed }"
    />
  </div>
</template>

<style scoped>
.lm-lottie {
  position: relative;
  /* inline-flex:根元素仍是行内级(与 inline-block 同布局语义),内部居中让
     fallback 插槽在尺寸由 width/height 决定时也居中,不依赖调用方的对齐方式 */
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.lm-lottie__host {
  position: absolute;
  inset: 0;
  opacity: 0;
  transition: opacity 0.4s ease;
}
.lm-lottie__host--on {
  opacity: 1;
}
</style>
