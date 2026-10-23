<script setup lang="ts">
import { useBrand, DEFAULT_SITE_DESCRIPTION } from '@repo/hooks'

const { brandConfig } = useBrand()

/**
 * logo 为 Emoji/URL 二选一:URL/data: 走图片渲染,其余按文本(emoji)渲染。
 * 品牌未自定义 logo 时渲染产品默认 logo,并挂 hover 触发的 Lottie 动效
 * (素材 public/animations/logo.json);已自定义时保持原样,不因 hover 被产品 logo 覆盖。
 */
const isImageLogo = computed(() =>
  /^(https?:)?\/\/|^(data:image|\/)/i.test(brandConfig.value.logo?.trim() || ''),
)

/** hover Lottie 素材地址:带 BASE_URL(tenant 端为 /tenant/),避免裸绝对路径在子路径部署下 404 */
const logoLottieSrc = `${import.meta.env.BASE_URL}animations/logo.json`

/** 版本更新信息(版本号/检查更新/新版本说明)仅管理端关于页展示,租户端不感知 */

/** 彩蛋状态与点击计数器:连续快速点击 5 次激活彩蛋炫彩发光动效 */
const isRainbowMode = ref(false)
let clickCount = 0
let clickTimer: ReturnType<typeof setTimeout> | null = null
let rainbowTimer: ReturnType<typeof setTimeout> | null = null

function handleLogoClick() {
  clickCount++
  if (clickTimer) clearTimeout(clickTimer)

  // 1 秒内没有继续点击则重置计数
  clickTimer = setTimeout(() => {
    clickCount = 0
  }, 1000)

  if (clickCount >= 5) {
    clickCount = 0
    isRainbowMode.value = true

    // 持续炫彩 6 秒后恢复
    if (rainbowTimer) clearTimeout(rainbowTimer)
    rainbowTimer = setTimeout(() => {
      isRainbowMode.value = false
    }, 6000)
  }
}

onBeforeUnmount(() => {
  if (clickTimer) clearTimeout(clickTimer)
  if (rainbowTimer) clearTimeout(rainbowTimer)
})
</script>

<template>
  <LmCard
    title="关于平台"
    segmented
  >
    <div class="mx-auto max-w-[64rem] py-[2rem]">
      <!-- 品牌标识 Hero -->
      <div class="flex flex-col items-center text-center">
        <!-- Logo 容器（带点击彩蛋） -->
        <div
          class="relative flex h-[7.2rem] w-[7.2rem] cursor-default select-none items-center justify-center rounded-2xl border border-line bg-fill-2 shadow-sm transition-all duration-300 active:scale-95"
          :class="[
            isRainbowMode
              ? 'rainbow-glow animate-pulse scale-110'
              : '',
          ]"
          @click="handleLogoClick"
        >
          <img
            v-if="isImageLogo"
            :src="brandConfig.logo"
            :alt="brandConfig.platformName"
            class="h-full w-full rounded-2xl object-contain p-[.6rem]"
          >
          <span
            v-else-if="brandConfig.logo"
            class="text-[3.6rem] leading-none"
          >
            {{ brandConfig.logo }}
          </span>
          <LmLottie
            v-else
            :src="logoLottieSrc"
            width="5.2rem"
            height="5.2rem"
            :loop="false"
            trigger="hover"
          >
            <!-- 未 hover / 触屏 / reduced-motion / 加载失败:静态产品 logo 即终态 -->
            <LmLogo :size="44" class="text-primary" />
          </LmLottie>
        </div>

        <!-- 平台名称与副标 -->
        <h2 class="mt-[1.6rem] mb-0 text-xl font-bold text-strong tracking-tight">
          {{ brandConfig.platformName || 'LLMPod' }}
        </h2>
        <p class="mt-[.6rem] mb-0 text-small text-secondary">
          {{ brandConfig.siteDescription || DEFAULT_SITE_DESCRIPTION }}
        </p>
      </div>

      <!-- 信息条目列表 -->
      <div class="mt-[3.2rem] divide-y divide-line rounded-xl border border-line bg-fill-2 px-[2rem]">
        <div class="flex items-center justify-between py-[1.4rem]">
          <span class="text-small text-secondary">
            推理引擎
          </span>
          <span class="text-small font-medium text-default">
            vLLM / SGLang / llama.cpp
          </span>
        </div>

        <div class="flex items-center justify-between py-[1.4rem]">
          <span class="text-small text-secondary">
            接口协议
          </span>
          <span class="text-small font-medium text-default">
            OpenAI Compatible API / Claude API
          </span>
        </div>

        <div class="flex items-center justify-between py-[1.4rem]">
          <span class="text-small text-secondary">
            开源协议
          </span>
          <a
            href="https://www.gnu.org/licenses/agpl-3.0.html"
            target="_blank"
            rel="noopener noreferrer"
            class="text-small font-medium text-primary hover:opacity-80"
          >AGPL-3.0</a>
        </div>
      </div>
    </div>
  </LmCard>
</template>

<style scoped>
.rainbow-glow {
  animation: rainbow-rotate 3s linear infinite;
  box-shadow: 0 0 20px rgba(99, 102, 241, 0.6),
              0 0 35px rgba(236, 72, 153, 0.4),
              0 0 50px rgba(59, 130, 246, 0.3);
}

@keyframes rainbow-rotate {
  0% {
    filter: hue-rotate(0deg);
    border-color: #6366f1;
  }
  50% {
    filter: hue-rotate(180deg);
    border-color: #ec4899;
  }
  100% {
    filter: hue-rotate(360deg);
    border-color: #6366f1;
  }
}
</style>
