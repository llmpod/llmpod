<script setup lang="ts">
/**
 * 星空背景:深色渐变光晕 + 缓慢闪烁的星星。
 * 低干扰设计,仅作氛围烘托;尊重 prefers-reduced-motion(退化为静态星空)。
 */

interface Star {
  /** 相对坐标(0~1),与画布尺寸解耦,resize 后无需重建 */
  x: number
  y: number
  radius: number
  baseAlpha: number
  /** 闪烁角速度(rad/ms)与初始相位 */
  twinkleSpeed: number
  twinklePhase: number
}

const STAR_COUNT = 160
/** 闪烁时透明度最低保留比例,越接近 1 越安静 */
const TWINKLE_MIN_RATIO = 0

const canvasRef = ref<HTMLCanvasElement | null>(null)
let animationId = 0
let resizeObserver: ResizeObserver | null = null
let stars: Star[] = []

function createStars(): Star[] {
  return Array.from({ length: STAR_COUNT }, () => ({
    x: Math.random(),
    y: Math.random(),
    radius: 0.4 + Math.random() * 0.9,
    baseAlpha: 0.25 + Math.random() * 0.5,
    twinkleSpeed: 0.0002 + Math.random() * 0.0005,
    twinklePhase: Math.random() * Math.PI * 2,
  }))
}

function syncCanvasSize() {
  const canvas = canvasRef.value
  if (!canvas) return
  const dpr = window.devicePixelRatio || 1
  const { clientWidth, clientHeight } = canvas
  canvas.width = clientWidth * dpr
  canvas.height = clientHeight * dpr
  canvas.getContext('2d')?.setTransform(dpr, 0, 0, dpr, 0, 0)
}

function drawFrame(time: number) {
  const canvas = canvasRef.value
  const ctx = canvas?.getContext('2d')
  if (!canvas || !ctx) return

  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight)
  for (const star of stars) {
    const ratio =
      TWINKLE_MIN_RATIO +
      (1 - TWINKLE_MIN_RATIO) * (0.5 + 0.5 * Math.sin(time * star.twinkleSpeed + star.twinklePhase))
    ctx.globalAlpha = star.baseAlpha * ratio
    ctx.fillStyle = '#ffffff'
    ctx.beginPath()
    ctx.arc(star.x * canvas.clientWidth, star.y * canvas.clientHeight, star.radius, 0, Math.PI * 2)
    ctx.fill()
  }
  ctx.globalAlpha = 1
}

function loop(time: number) {
  drawFrame(time)
  animationId = requestAnimationFrame(loop)
}

onMounted(() => {
  stars = createStars()
  syncCanvasSize()
  resizeObserver = new ResizeObserver(syncCanvasSize)
  resizeObserver.observe(canvasRef.value!)

  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    drawFrame(0)
  } else {
    animationId = requestAnimationFrame(loop)
  }
})

onBeforeUnmount(() => {
  cancelAnimationFrame(animationId)
  resizeObserver?.disconnect()
})
</script>

<template>
  <div class="starfield-background">
    <canvas ref="canvasRef" class="block h-full w-full" />
  </div>
</template>

<style scoped>
.starfield-background {
  width: 100%;
  height: 100%;
  /* 两层极淡的径向光晕,增加纵深又不喧宾夺主 */
  background:
    radial-gradient(ellipse 80% 60% at 70% 15%, rgb(64 96 180 / 16%), transparent),
    radial-gradient(ellipse 60% 55% at 20% 85%, rgb(110 80 170 / 12%), transparent);
}
</style>
