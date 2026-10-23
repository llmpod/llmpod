<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import { Comment, type Component } from 'vue'

defineProps<{
  /** 层图标(@vicons/ionicons5 组件) */
  icon: Component
  title: string
  subtitle?: string
  /** 状态点颜色类,如 bg-success;不传则不显示状态点 */
  statusColor?: string
  /** 选中态:主色边框 + 软底(选择场景,如新建部署向导选卡) */
  selected?: boolean
  /** 灰显禁选态(离线/已占用) */
  dimmed?: boolean
  /** 自定义边框色(十六进制):多副本选卡按份着色(色环);与 selected 并存时优先本值 */
  ringColor?: string
  /** 虚线边框(配对差卡份标记) */
  dashed?: boolean
}>()

const slots = useSlots()
/** 插槽内容可能被 v-if 包成 Comment 占位(简洁模式),需检测真实内容避免渲染空容器 */
const hasContent = computed(
  () => slots.default?.().some((vnode) => vnode.type !== Comment) ?? false,
)

</script>

<template>
  <!-- 点击交互由 Vue Flow 的 node-click 统一处理 -->
  <div
    class="w-[18rem] rounded-lg border px-[1.2rem] py-[.8rem] shadow-sm"
    :class="[
      // 色环模式下边框色走内联 style,这里不再叠主题边框类
      ringColor ? '' : selected ? 'border-primary' : 'border-line-strong',
      selected ? 'bg-primary-soft' : 'bg-fill-1',
      dashed ? 'border-dashed' : '',
      dimmed ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer',
    ]"
    :style="ringColor ? { borderColor: ringColor } : undefined"
  >
    <Handle type="target" :position="Position.Left" class="!opacity-0" />
    <div class="flex items-center gap-[.6rem]">
      <n-icon :size="18" class="text-primary shrink-0"><component :is="icon" /></n-icon>
      <n-tooltip trigger="hover">
        <template #trigger>
          <span class="text-small text-strong font-medium truncate">{{ title }}</span>
        </template>
        {{ title }}
      </n-tooltip>
      <span
        v-if="statusColor"
        class="w-[.8rem] h-[.8rem] rounded-full ml-auto shrink-0"
        :class="statusColor"
      />
    </div>
    <div v-if="subtitle" class="text-xs text-secondary mt-[.2rem] truncate">{{ subtitle }}</div>
    <div v-if="hasContent" class="mt-[.6rem] flex flex-col gap-[.3rem] text-xs text-default">
      <slot />
    </div>
    <Handle type="source" :position="Position.Right" class="!opacity-0" />
  </div>
</template>
