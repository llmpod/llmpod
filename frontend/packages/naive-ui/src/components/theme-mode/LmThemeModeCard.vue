<script setup lang="ts">
import type { Component } from 'vue'
import { nextTick } from 'vue'
import { DesktopOutline, MoonOutline, SunnyOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import { useTheme, type ThemeMode } from '@repo/hooks'

defineOptions({ name: 'LmThemeModeCard' })

const { mode, darkMode, setThemeMode } = useTheme()

/** 三态选项:自动(显示器 + 主题色角点)/浅色(太阳)/深色(月亮) */
const MODE_OPTIONS: Array<{ value: ThemeMode; label: string; icon: Component }> = [
  { value: 'auto', label: '自动', icon: DesktopOutline },
  { value: 'light', label: '浅色', icon: SunnyOutline },
  { value: 'dark', label: '深色', icon: MoonOutline },
]

/** 选项的最终明暗:auto 跟随系统偏好,light/dark 由选项自身决定 */
function resolveDark(value: ThemeMode): boolean {
  if (value !== 'auto') return value === 'dark'
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

/** 选择主题:明暗实际变化时用 View Transitions 斜切扫屏过渡(与 LmLayout 切换同款),否则直接切换 */
function handleSelect(value: ThemeMode) {
  // 点 auto 但系统偏好与当前明暗一致等场景界面无变化,跳过转场避免空扫一屏
  if (!document.startViewTransition || resolveDark(value) === darkMode.value) {
    setThemeMode(value)
    return
  }
  const transition = document.startViewTransition(async () => {
    setThemeMode(value)
    // 等 Vue 将新主题应用到 DOM 后再捕获新视图
    await nextTick()
  })
  void transition.ready.then(() => {
    // 斜切扫屏:45° 斜边从左上向右下扫过
    document.documentElement.animate(
      {
        clipPath: [
          'polygon(0% 0%, 0% 0%, -100% 100%, -100% 100%)',
          'polygon(0% 0%, 200% 0%, 100% 100%, 0% 100%)',
        ],
      },
      { duration: 400, easing: 'ease-out', pseudoElement: '::view-transition-new(root)' },
    )
  })
}
</script>

<template>
  <div class="flex flex-col gap-[1rem]">
    <div class="flex gap-[.8rem]">
      <button
        v-for="option in MODE_OPTIONS"
        :key="option.value"
        type="button"
        class="text-default flex w-[6.4rem] cursor-pointer flex-col items-center gap-[.6rem] rounded-[.6rem] border px-[.8rem] py-[1rem] transition-colors"
        :class="
          mode === option.value
            ? 'border-primary bg-primary-soft'
            : 'border-line-strong bg-fill-1 hover:border-primary/60'
        "
        :aria-pressed="mode === option.value"
        @click="handleSelect(option.value)"
      >
        <span class="relative">
          <n-icon
            :size="20"
            :class="option.value === 'light' ? 'text-warning' : 'text-secondary'"
          >
            <component :is="option.icon" />
          </n-icon>
          <!-- 自动选中时:右上角主题色状态点,示意正在跟随系统 -->
          <span
            v-if="mode === 'auto' && option.value === 'auto'"
            class="bg-primary absolute top-[-.2rem] right-[-.2rem] h-[.5rem] w-[.5rem] rounded-full"
          />
        </span>
        <span
          class="text-base"
          :class="mode === option.value ? 'text-primary font-medium' : ''"
        >
          {{ option.label }}
        </span>
      </button>
    </div>
    <p class="text-secondary text-small">
      选择界面主题风格，影响整体界面的明暗风格
    </p>
  </div>
</template>

<style>
/* 主题切换转场:禁用 View Transitions 默认交叉淡入,由斜切 clip-path 动画接管(与 LmLayout 同款规则) */
::view-transition-old(root),
::view-transition-new(root) {
  animation: none;
  mix-blend-mode: normal;
}
</style>
