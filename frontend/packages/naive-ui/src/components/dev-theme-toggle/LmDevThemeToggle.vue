<script setup lang="ts">
import { computed, ref, type Component } from 'vue'
import { NIcon, NPopover } from 'naive-ui'
import { SunnyOutline, MoonOutline, DesktopOutline } from '@vicons/ionicons5'
import { useTheme, type ThemeMode } from '@repo/hooks'

defineOptions({ name: 'LmDevThemeToggle' })

const { mode, setThemeMode } = useTheme()

interface ModeOption {
  value: ThemeMode
  label: string
  icon: Component
}

const modes: ModeOption[] = [
  { value: 'light', label: '明亮', icon: SunnyOutline },
  { value: 'dark', label: '暗黑', icon: MoonOutline },
  { value: 'auto', label: '跟随系统', icon: DesktopOutline },
]

// 触发按钮展示当前选中模式的图标
const currentIcon = computed(
  () => modes.find((item) => item.value === mode.value)?.icon ?? SunnyOutline,
)

const showPanel = ref(false)

function handleChoose(option: ModeOption) {
  setThemeMode(option.value)
  showPanel.value = false
}
</script>

<template>
  <div class="fixed bottom-[2rem] right-[2rem] z-[99999999999999]">
    <n-popover v-model:show="showPanel" trigger="click" placement="top-end" :show-arrow="false">
      <template #trigger>
        <button
          type="button"
          aria-label="切换主题"
          class="grid h-[4.4rem] w-[4.4rem] cursor-pointer place-items-center rounded-full bg-primary text-inverse shadow-md transition-transform hover:scale-105"
        >
          <n-icon :size="20">
            <component :is="currentIcon" />
          </n-icon>
        </button>
      </template>

      <div class="flex w-[12rem] flex-col gap-[0.4rem]">
        <button
          v-for="item in modes"
          :key="item.value"
          type="button"
          class="flex cursor-pointer items-center gap-[0.8rem] rounded-md px-[1.2rem] py-[1.2rem] text-left text-default transition-colors"
          :class="mode === item.value ? 'bg-primary-soft text-primary' : 'hover:bg-fill-4'"
          @click="handleChoose(item)"
        >
          <n-icon :size="18">
            <component :is="item.icon" />
          </n-icon>
          <span>{{ item.label }}</span>
        </button>
      </div>
    </n-popover>
  </div>
</template>
