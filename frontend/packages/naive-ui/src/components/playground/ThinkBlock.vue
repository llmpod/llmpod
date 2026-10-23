<script setup lang="ts">
import { BulbOutline, ChevronDownOutline } from '@vicons/ionicons5'
import { NIcon } from 'naive-ui'
import { computed, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 思考内容全文。 */
    reasoning: string
    /** 是否仍在思考中(流式)。 */
    streaming?: boolean
    /** 折叠时头部预览的尾部字符数。 */
    previewLength?: number
  }>(),
  { streaming: false, previewLength: 40 },
)

// 默认折叠,展开状态由用户控制,流式更新不重置
const expanded = ref(false)

/** 头部预览:思考内容的最后 N 个字符(空白折叠为单空格)。 */
const preview = computed(() => {
  const text = props.reasoning.replace(/\s+/g, ' ').trim()
  if (!text) return ''
  return text.length > props.previewLength ? `...${text.slice(-props.previewLength)}` : text
})
</script>

<template>
  <div class="think-block" :class="{ 'think-block--expanded': expanded }">
    <button type="button" class="think-block__header" @click="expanded = !expanded">
      <n-icon class="think-block__icon" :component="BulbOutline" />
      <span class="think-block__title">
        {{ streaming ? '思考中' : '思考过程' }}
        <span v-if="streaming" class="think-block__dots">
          <i /><i /><i />
        </span>
      </span>
      <span v-if="!expanded && streaming" class="think-block__preview">{{ preview }}</span>
      <n-icon class="think-block__arrow" :component="ChevronDownOutline" />
    </button>
    <div class="think-block__collapse">
      <div class="think-block__collapse-inner">
        <div class="think-block__body">{{ reasoning }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.think-block {
  margin-bottom: 1rem;
  background: var(--c-fill-2);
  border: 1px solid var(--c-line);
  border-radius: 0.8rem;

  &__header {
    display: flex;
    gap: 0.8rem;
    align-items: center;
    width: 100%;
    padding: 0.8rem 1.2rem;
    font-size: 1.3rem;
    color: var(--c-secondary);
    text-align: left;
    cursor: pointer;
    background: none;
    border: none;
    transition: color 0.2s ease;

    &:hover {
      color: var(--c-default);
    }
  }

  &__icon {
    flex-shrink: 0;
    font-size: 1.5rem;
  }

  &__title {
    display: flex;
    flex-shrink: 0;
    gap: 0.3rem;
    align-items: center;
    color: var(--c-default);
  }

  &__dots {
    display: inline-flex;
    gap: 0.3rem;

    i {
      width: 0.3rem;
      height: 0.3rem;
      background: currentColor;
      border-radius: 50%;
      animation: think-dot 1.2s infinite ease-in-out;

      &:nth-child(2) {
        animation-delay: 0.2s;
      }

      &:nth-child(3) {
        animation-delay: 0.4s;
      }
    }
  }

  &__preview {
    flex: 1;
    overflow: hidden;
    font-size: 1.3rem;
    color: var(--c-secondary);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__arrow {
    flex-shrink: 0;
    font-size: 1.4rem;
    transition: transform 0.2s ease;
  }

  &--expanded &__arrow {
    transform: rotate(180deg);
  }

  &__collapse {
    display: grid;
    grid-template-rows: 0fr;
    transition: grid-template-rows 0.25s ease;
  }

  &--expanded &__collapse {
    grid-template-rows: 1fr;
  }

  &__collapse-inner {
    min-height: 0;
    overflow: hidden;
  }

  &__body {
    padding: 0.4rem 1.2rem 1.2rem 3.5rem;
    font-size: 1.3rem;
    line-height: 1.7;
    color: var(--c-secondary);
    white-space: pre-wrap;
    word-break: break-word;
    border-top: 1px dashed var(--c-line);
  }
}

@keyframes think-dot {
  0%,
  60%,
  100% {
    opacity: 0.3;
    transform: translateY(0);
  }

  30% {
    opacity: 1;
    transform: translateY(-0.2rem);
  }
}
</style>
