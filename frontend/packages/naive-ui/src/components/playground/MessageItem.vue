<script setup lang="ts">
import {
  CheckmarkOutline,
  CloseCircleOutline,
  CopyOutline,
  CreateOutline,
  PersonOutline,
  RefreshOutline,
  StopCircleOutline,
  TrashOutline,
} from '@vicons/ionicons5'
// 别名避开 prop message(模板大量使用 message.content 等,同名 import 会遮蔽 prop 绑定)
import { message as lmMessage } from '@repo/hooks'
import { NButton, NIcon, NImage, NInput } from 'naive-ui'
import { computed, ref } from 'vue'
import { copyToClipboard } from '../../utils/clipboard'
import LmLogo from '../layout/LmLogo.vue'
import type { PlaygroundChatMessage } from './types'
import MarkdownView from './MarkdownView.vue'
import ThinkBlock from './ThinkBlock.vue'

const props = withDefaults(
  defineProps<{
    message: PlaygroundChatMessage
    /** 该消息是否仍在流式生成。 */
    streaming?: boolean
    /** 禁用消息操作(整轮生成中)。 */
    actionsDisabled?: boolean
  }>(),
  { streaming: false, actionsDisabled: false },
)

const emit = defineEmits<{
  /** 删除该消息。 */
  delete: []
  /** 用户消息编辑后重发。 */
  resend: [content: string]
  /** 重新生成该条 assistant 回复。 */
  regenerate: []
}>()

const copied = ref(false)

const showThink = computed(() => props.message.reasoning !== '')
/** 流式中且尚无任何内容:显示输入中占位。 */
const showTyping = computed(
  () => props.streaming && props.message.content === '' && props.message.reasoning === '',
)
const showStopped = computed(
  () =>
    props.message.status === 'stopped' &&
    (props.message.content !== '' || props.message.reasoning !== ''),
)

/** OpenAI 标准错误字段标签:HTTP 状态码/type/code/param。 */
const errorTags = computed(() => {
  const detail = props.message.errorDetail
  if (!detail) return []
  const tags: string[] = []
  if (detail.status !== undefined) tags.push(`HTTP ${detail.status}`)
  if (detail.type) tags.push(detail.type)
  if (detail.code) tags.push(detail.code)
  if (detail.param) tags.push(`param: ${detail.param}`)
  return tags
})

// ===== 行内编辑(用户消息) =====
const editing = ref(false)
const draft = ref('')
/** 空 text 且无图片时不可发送。 */
const canSubmitEdit = computed(
  () => draft.value.trim() !== '' || props.message.images.length > 0,
)

const showActions = computed(() => !props.streaming && !props.actionsDisabled && !editing.value)

function startEdit() {
  draft.value = props.message.content
  editing.value = true
}

function cancelEdit() {
  editing.value = false
}

function submitEdit() {
  if (!canSubmitEdit.value) return
  editing.value = false
  emit('resend', draft.value.trim())
}

async function handleCopy() {
  try {
    await copyToClipboard(props.message.content)
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 1500)
  } catch {
    lmMessage.error('复制失败,请手动选择复制')
  }
}
</script>

<template>
  <!-- 用户消息:右侧头像 + 主色气泡,支持行内编辑重发 -->
  <div v-if="message.role === 'user'" class="msg-item msg-item--user">
    <div class="msg-item__avatar msg-item__avatar--user">
      <n-icon :component="PersonOutline" />
    </div>

    <div class="msg-item__main msg-item__main--user">
      <!-- 编辑态:图片保留展示 + 文本编辑框 + 取消/发送 -->
      <div v-if="editing" class="msg-item__editor">
        <div
          v-if="message.images.length"
          class="msg-item__images"
          :class="{ 'msg-item__images--single': message.images.length === 1 }"
        >
          <n-image
            v-for="(image, index) in message.images"
            :key="index"
            class="msg-item__image"
            :src="image"
            object-fit="cover"
            :img-props="{ loading: 'lazy' }"
          />
        </div>
        <n-input
          v-model:value="draft"
          type="textarea"
          class="msg-item__editor-input"
          :autosize="{ minRows: 2, maxRows: 8 }"
          placeholder="编辑后重新发送"
        />
        <div class="msg-item__editor-actions">
          <n-button size="tiny" quaternary @click="cancelEdit">取消</n-button>
          <n-button size="tiny" type="primary" :disabled="!canSubmitEdit" @click="submitEdit">
            发送
          </n-button>
        </div>
      </div>

      <template v-else>
        <div class="msg-item__bubble msg-item__bubble--user">
          <div
            v-if="message.images.length"
            class="msg-item__images"
            :class="{ 'msg-item__images--single': message.images.length === 1 }"
          >
            <n-image
              v-for="(image, index) in message.images"
              :key="index"
              class="msg-item__image"
              :src="image"
              object-fit="cover"
              :img-props="{ loading: 'lazy' }"
            />
          </div>
          <div v-if="message.content" class="msg-item__text">{{ message.content }}</div>
        </div>
        <div v-if="showActions" class="msg-item__actions">
          <button
            v-if="message.content"
            type="button"
            class="msg-item__action"
            title="复制"
            @click="handleCopy"
          >
            <n-icon :component="copied ? CheckmarkOutline : CopyOutline" />
          </button>
          <button type="button" class="msg-item__action" title="编辑后重新发送" @click="startEdit">
            <n-icon :component="CreateOutline" />
          </button>
          <button type="button" class="msg-item__action" title="删除" @click="emit('delete')">
            <n-icon :component="TrashOutline" />
          </button>
        </div>
      </template>
    </div>
  </div>

  <!-- 助手消息:项目 logo 头像 + 思考折叠块 + Markdown 正文 + 状态尾巴 + 操作栏 -->
  <div v-else class="msg-item msg-item--assistant">
    <div class="msg-item__avatar msg-item__avatar--assistant">
      <LmLogo :size="24" />
    </div>

    <div class="msg-item__main">
      <div class="msg-item__bubble msg-item__bubble--assistant">
        <ThinkBlock v-if="showThink" :reasoning="message.reasoning" :streaming="streaming" />
        <div v-if="showTyping" class="msg-item__typing">
          正在输入<span class="msg-item__typing-dots"><i /><i /><i /></span>
        </div>
        <MarkdownView v-else :source="message.content" :streaming="streaming" />
        <div v-if="showStopped" class="msg-item__stopped">
          <n-icon :component="StopCircleOutline" />
          已停止生成
        </div>
        <div v-if="message.status === 'error'" class="msg-item__error">
          <n-icon :component="CloseCircleOutline" />
          <div class="msg-item__error-body">
            <div class="msg-item__error-text">{{ message.error || '生成失败' }}</div>
            <div v-if="errorTags.length" class="msg-item__error-tags">
              <span v-for="tag in errorTags" :key="tag" class="msg-item__error-tag">
                {{ tag }}
              </span>
            </div>
          </div>
        </div>
      </div>
      <div v-if="showActions" class="msg-item__actions">
        <button
          v-if="message.content"
          type="button"
          class="msg-item__action"
          title="复制全文"
          @click="handleCopy"
        >
          <n-icon :component="copied ? CheckmarkOutline : CopyOutline" />
        </button>
        <button type="button" class="msg-item__action" title="重新生成" @click="emit('regenerate')">
          <n-icon :component="RefreshOutline" />
        </button>
        <button type="button" class="msg-item__action" title="删除" @click="emit('delete')">
          <n-icon :component="TrashOutline" />
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.msg-item {
  display: flex;
  gap: 1rem;
  align-items: flex-start;
  margin-bottom: 1.2rem;

  &--user {
    flex-direction: row-reverse;
  }

  &__avatar {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    width: 3.2rem;
    height: 3.2rem;
    overflow: hidden;
    border-radius: 50%;

    &--user {
      font-size: 1.8rem;
      color: var(--c-icon);
      background: var(--c-fill-4);
    }

    &--assistant {
      background: var(--c-fill-1);
      border: 1px solid var(--c-line-strong);
    }
  }

  &__main {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 0;

    &--user {
      align-items: flex-end;
    }
  }

  &__bubble {
    max-width: 88%;
    padding: 1.2rem 1.4rem;
    font-size: 1.4rem;
    line-height: 1.7;
    border-radius: 0.8rem;

    &--user {
      color: var(--c-inverse);
      background: rgb(var(--n-primary-color));
    }

    &--assistant {
      min-width: 12rem;
      color: var(--c-default);
      background: var(--c-fill-4);
    }
  }

  &__images {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.6rem;
    margin-bottom: 0.8rem;

    &--single {
      grid-template-columns: minmax(0, 24rem);

      .msg-item__image {
        height: auto;
      }

      .msg-item__image :deep(img) {
        height: auto;
      }
    }
  }

  &__image {
    overflow: hidden;
    width: 100%;
    height: 10rem;
    border-radius: 0.6rem;

    :deep(img) {
      width: 100%;
      height: 100%;
    }
  }

  &__text {
    white-space: pre-wrap;
    word-break: break-word;
  }

  &__typing {
    display: flex;
    gap: 0.2rem;
    align-items: center;
    color: var(--c-secondary);
  }

  &__typing-dots {
    display: inline-flex;
    gap: 0.3rem;

    i {
      width: 0.3rem;
      height: 0.3rem;
      background: currentColor;
      border-radius: 50%;
      animation: msg-typing-dot 1.2s infinite ease-in-out;

      &:nth-child(2) {
        animation-delay: 0.2s;
      }

      &:nth-child(3) {
        animation-delay: 0.4s;
      }
    }
  }

  &__stopped,
  &__error {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    margin-top: 0.8rem;
    font-size: 1.3rem;
    word-break: break-word;
  }

  &__stopped {
    align-items: center;
    color: var(--c-secondary);
  }

  &__error {
    color: rgb(var(--n-error-color));

    :deep(.n-icon) {
      flex-shrink: 0;
      margin-top: 0.3rem;
    }
  }

  &__error-body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.5rem;
    min-width: 0;
  }

  &__error-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  &__error-tag {
    padding: 0.1rem 0.6rem;
    font-size: 1.2rem;
    line-height: 1.6;
    color: rgb(var(--n-error-color));
    background: rgb(var(--n-error-color) / 0.12);
    border-radius: 0.4rem;
  }

  &__actions {
    display: flex;
    gap: 0.4rem;
    margin-top: 0.6rem;
    visibility: hidden;
    opacity: 0;
    transition:
      opacity 0.2s ease,
      visibility 0.2s ease;
  }

  &__action {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 2.4rem;
    height: 2.4rem;
    padding: 0;
    font-size: 1.4rem;
    color: var(--c-secondary);
    cursor: pointer;
    background: transparent;
    border: none;
    border-radius: 0.6rem;
    transition: color 0.2s ease;

    &:hover {
      color: var(--c-default);
    }
  }

  &__editor {
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
    width: 88%;
    padding: 1rem;
    background: var(--c-fill-1);
    border: 1px solid rgb(var(--n-primary-color));
    border-radius: 0.8rem;
  }

  &__editor-input {
    :deep(textarea) {
      resize: none;
    }
  }

  &__editor-actions {
    display: flex;
    gap: 0.8rem;
    justify-content: flex-end;
  }

  &:hover &__actions {
    visibility: visible;
    opacity: 1;
  }
}

@keyframes msg-typing-dot {
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
