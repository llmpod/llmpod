<script setup lang="ts">
import { NButton, NModal, NSpace } from 'naive-ui'

interface Props {
  title?: string
  showFooter?: boolean
  positiveText?: string
  negativeText?: string
  showPositiveButton?: boolean
  showNegativeButton?: boolean
  positiveLoading?: boolean
  negativeLoading?: boolean
  positiveDisabled?: boolean
  negativeDisabled?: boolean
}

withDefaults(defineProps<Props>(), {
  title: '',
  showFooter: true,
  positiveText: '确定',
  negativeText: '取消',
  showPositiveButton: true,
  showNegativeButton: true,
  positiveLoading: false,
  negativeLoading: false,
  positiveDisabled: false,
  negativeDisabled: false,
})

const emit = defineEmits<{
  'positive-click': []
  'negative-click': []
  close: []
  'update:show': [value: boolean]
}>()
</script>

<template>
  <n-modal
    v-bind="$attrs"
    preset="card"
    class="lm-dialog"
    :mask-closable="false"
    :draggable="$attrs?.draggable ?? true"
    :internal-appear="true"
    @close="emit('close')"
    @update:show="emit('update:show', $event)"
  >
    <template #header>
      <slot name="header">
        <div class="lm-dialog__title">
          {{ title }}
        </div>
      </slot>
    </template>

    <template #header-extra>
      <slot name="header-extra" />
    </template>

    <slot />

    <template v-if="showFooter" #footer>
      <slot name="footer">
        <n-space justify="end">
          <n-button
            v-if="showNegativeButton"
            :loading="negativeLoading"
            :disabled="negativeDisabled"
            @click="emit('negative-click')"
          >
            {{ negativeText }}
          </n-button>
          <n-button
            v-if="showPositiveButton"
            type="primary"
            :loading="positiveLoading"
            :disabled="positiveDisabled"
            @click="emit('positive-click')"
          >
            {{ positiveText }}
          </n-button>
        </n-space>
      </slot>
    </template>
  </n-modal>
</template>

<style scoped lang="scss">
// 限制弹窗最大高度不超出视口:与 area 设定的固定 height 取交集,超出时由内容区滚动
:global(.lm-dialog) {
  max-height: calc(100vh - 3.2rem);
}

// 窄屏(≤767px):宽度强制近全屏(两侧各留 1rem),覆盖 useDialog/useConfirm 的 area 内联宽;
// 内联 style 非 important,!important 媒体查询规则胜出
@media (max-width: 767px) {
  :global(.lm-dialog) {
    width: calc(100vw - 2rem) !important;
    max-width: calc(100vw - 2rem) !important;
  }
}

:global(.lm-dialog .n-card-header) {
  flex-shrink: 0;
  border-bottom: 1px solid var(--n-border-color);
}

:global(.lm-dialog .n-card-content) {
  min-height: 0;
  overflow-y: auto;
  padding-top: 1.6rem;
}

:global(.lm-dialog .n-card__footer) {
  flex-shrink: 0;
}

:global(.lm-dialog .lm-dialog__title) {
  font-size: 1.6rem;
  font-weight: 500;
}
</style>
