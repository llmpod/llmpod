<script setup lang="ts">
import { message, useBreakpoint } from '@repo/hooks'
import {
  ArrowUndoOutline,
  OptionsOutline,
  SendOutline,
  StopOutline,
  TrashBinOutline,
} from '@vicons/ionicons5'
import {
  NButton,
  NIcon,
  NInput,
  NInputNumber,
  NScrollbar,
  NSlider,
  NSwitch,
  NTag,
  NTooltip,
} from 'naive-ui'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import LmSelect from '../form/LmSelect.vue'
import MessageItem from './MessageItem.vue'
import { PLAYGROUND_PRESETS } from './presets'
import { PLAYGROUND_REASONING_EFFORT_OPTIONS } from './types'
import type {
  PlaygroundApiKeyOption,
  PlaygroundChatSender,
  PlaygroundModelOption,
} from './types'
import { applyModelDefaults, PLAYGROUND_DEFAULT_SAMPLING, usePlayground } from './usePlayground'

interface Props {
  /** 模型下拉选项。 */
  models: PlaygroundModelOption[]
  /**
   * 初始优先选中的模型 value(如模型市场「在线调试」跳转携带的路由参数)。
   * 仅在尚未选中模型时生效;不在选项中时回退第一项,用户手动切换后不再覆盖。
   */
  initialModel?: string | null
  /** 由 createChatSender 创建的发送函数。 */
  sender: PlaygroundChatSender
  /** 顶部额度说明文案,例如 `剩余免费额度:45,000 / 100,000 Tokens`。 */
  quotaText?: string
  /** API Key 下拉选项;非空时展示 Key 选择且发送前必须选中。 */
  apiKeys?: PlaygroundApiKeyOption[]
  /** 当前选中的 API Key(v-model:api-key)。 */
  apiKey?: string | null
  /** 未选中 API Key 时禁止发送(租户端直连数据面必选 Key);列表为空时提示先创建。 */
  requireApiKey?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  initialModel: null,
  quotaText: '',
  apiKeys: () => [],
  apiKey: null,
  requireApiKey: false,
})

const emit = defineEmits<{
  'update:apiKey': [value: string | null]
}>()

const {
  params,
  messages,
  input,
  pendingImages,
  sending,
  send,
  stop,
  clear,
  addImageFiles,
  removePendingImage,
  removeMessage,
  resendMessage,
  regenerateMessage,
} = usePlayground({
  sender: props.sender,
  // onError 仅处理图片校验等本地错误;AI 请求失败以 error 消息直接渲染在对话内容区
  onError: (msg) => message.error(msg),
})

// 初始选中:优先 initialModel(路由参数),未匹配/未传回退第一个模型;
// 切换模型时自动填充模型特有配置(思考强度,其余参数保持用户当前值——数据来自调用方传入的模型元数据,未提供时不动)
watch(
  [() => props.models, () => props.initialModel],
  ([models, initialModel]) => {
    if (params.model !== '' || models.length === 0) return
    const preferred =
      initialModel && models.some((model) => model.value === initialModel)
        ? initialModel
        : models[0].value
    params.model = preferred
  },
  { immediate: true },
)

watch(
  () => params.model,
  (value) => {
    applyModelDefaults(params, props.models.find((model) => model.value === value))
  },
  { immediate: true },
)

/** 「最大 Token」输入上限:固定值,不随模型上下文变化。 */
const PLAYGROUND_MAX_TOKENS_LIMIT = 131072

// ===== API Key 选择:当前值失效(列表刷新/清空)时回退到第一项
const selectedApiKey = computed({
  get: () => props.apiKey ?? null,
  set: (value: string | null) => emit('update:apiKey', value),
})

watch(
  () => props.apiKeys,
  (keys) => {
    if (!keys.length) return
    if (!props.apiKey || !keys.some((key) => key.value === props.apiKey)) {
      emit('update:apiKey', keys[0].value)
    }
  },
  { immediate: true },
)

/** 重置采样参数与系统提示词(保留模型选择)。 */
function resetParams() {
  Object.assign(params, PLAYGROUND_DEFAULT_SAMPLING)
  message.success('参数已重置')
}

// ===== 消息滚动:贴近底部时自动跟随,用户上滑回看不强制拉底 =====
const scrollbarRef = ref<InstanceType<typeof NScrollbar>>()
const stickToBottom = ref(true)

function handleMessagesScroll(event: Event) {
  const el = event.target as HTMLElement
  stickToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 60
}

watch(
  messages,
  async () => {
    if (!stickToBottom.value) return
    await nextTick()
    scrollbarRef.value?.scrollTo({ top: Number.MAX_SAFE_INTEGER })
  },
  { deep: true },
)

// ===== 输入区:图片粘贴/拖拽 =====
const dragging = ref(false)

/** 粘贴图片:拦截图片文件,避免作为文件名文本插入。 */
function handlePaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files ?? []).filter((file) =>
    file.type.startsWith('image/'),
  )
  if (files.length) {
    event.preventDefault()
    addImageFiles(files)
  }
}

function handleDrop(event: DragEvent) {
  dragging.value = false
  const files = Array.from(event.dataTransfer?.files ?? [])
  if (files.length) addImageFiles(files)
}

// ===== 发送 =====
/** 发送禁用原因;null 表示可发送(优先级:模型 → API Key → 输入内容)。 */
const sendDisabledReason = computed<string | null>(() => {
  if (params.model === '') return '请先选择模型'
  // Key 必选:列表非空需选中;requireApiKey 下列表为空(尚未创建)同样拦截
  if ((props.apiKeys.length > 0 || props.requireApiKey) && !props.apiKey) {
    return props.apiKeys.length === 0 ? '请先创建 API Key' : '请先选择 API Key'
  }
  if (input.value.trim() === '' && pendingImages.value.length === 0) return '请输入消息内容'
  return null
})
const canSend = computed(() => sendDisabledReason.value === null)

// ===== 必填字段错误态:尝试发送(Enter 被拦/点击发送)后显示,值补上即自动恢复
/** API Key 是否必选(与发送拦截条件一致);模型恒必选。 */
const apiKeyRequired = computed(() => props.apiKeys.length > 0 || props.requireApiKey)
const fieldErrorVisible = ref(false)
const apiKeyInvalid = computed(
  () => fieldErrorVisible.value && apiKeyRequired.value && !props.apiKey,
)
const modelInvalid = computed(() => fieldErrorVisible.value && params.model === '')

// 可发送后清除错误标记,避免外部刷新清空已选值时残留红框
watch(canSend, (value) => {
  if (value) fieldErrorVisible.value = false
})

// 禁用原因 tooltip 显隐:hover 禁用按钮时跟随,Enter 被拦时脉冲显示 1.6s
const hoveringSend = ref(false)
const sendTipPulse = ref(false)
let sendTipTimer: ReturnType<typeof setTimeout> | undefined

const sendTipShow = computed(
  () => sendDisabledReason.value !== null && (hoveringSend.value || sendTipPulse.value),
)

/** Enter 发送被禁用时脉冲显示原因提示,并标记必填字段错误态。 */
function flashSendTip() {
  fieldErrorVisible.value = true
  sendTipPulse.value = true
  clearTimeout(sendTipTimer)
  sendTipTimer = setTimeout(() => {
    sendTipPulse.value = false
  }, 1600)
}

onBeforeUnmount(() => clearTimeout(sendTipTimer))

// 窄屏(≤767px):参数面板转抽屉覆盖层,聊天区占满全宽;回到桌面自动关闭并复位
const { isMobile } = useBreakpoint()
const panelOpen = ref(false)
watch(isMobile, (mobile) => {
  if (!mobile) panelOpen.value = false
})

/** Enter 发送,Shift+Enter 换行;输入法组词中不触发;禁用时提示原因。 */
function handleInputKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    if (canSend.value) send()
    else flashSendTip()
  }
}

/** 点击发送:可发送则发送,否则标记必填错误态并脉冲提示原因。 */
function handleSendClick() {
  if (canSend.value) send()
  else flashSendTip()
}
</script>

<template>
  <div class="lm-playground">
    <!-- 窄屏抽屉遮罩:点击关闭参数面板 -->
    <div
      v-if="isMobile && panelOpen"
      class="lm-playground__mask"
      @click="panelOpen = false"
    />
    <!-- 左侧:参数配置面板(窄屏转抽屉) -->
    <aside
      class="lm-playground__panel"
      :class="{ 'lm-playground__panel--open': isMobile && panelOpen }"
    >
      <div class="lm-playground__panel-head">
        <h3 class="lm-playground__panel-title">参数设置</h3>
        <n-button quaternary size="tiny" @click="resetParams">
          <template #icon>
            <n-icon :component="ArrowUndoOutline" />
          </template>
          重置
        </n-button>
      </div>

      <div v-if="apiKeys.length || $slots['api-key-empty']" class="lm-playground__field">
        <span
          class="lm-playground__label"
          :class="{ 'lm-playground__label--required': apiKeyRequired }"
        >API Key</span>
        <LmSelect
          v-if="apiKeys.length"
          v-model:value="selectedApiKey"
          :options="apiKeys"
          :status="apiKeyInvalid ? 'error' : undefined"
          placeholder="请选择要使用的 API Key"
          filterable
        />
        <!-- 空态:提供了 api-key-empty 插槽(创建入口)时字段仍展示,否则整块隐藏 -->
        <div v-else class="lm-playground__key-empty">
          <span class="lm-playground__hint">暂无可用 API Key</span>
          <slot name="api-key-empty" />
        </div>
      </div>

      <div class="lm-playground__field">
        <span class="lm-playground__label lm-playground__label--required">模型</span>
        <LmSelect
          v-model:value="params.model"
          :options="models"
          :status="modelInvalid ? 'error' : undefined"
          placeholder="请选择模型"
          filterable
        />
      </div>

      <div class="lm-playground__field lm-playground__field--row">
        <div class="lm-playground__label-row">
          <span class="lm-playground__label">流式输出</span>
          <n-switch v-model:value="params.stream" size="small" />
        </div>
        <span class="lm-playground__hint">关闭后等待完整响应一次性返回</span>
      </div>

      <div class="lm-playground__field">
        <span class="lm-playground__label">思考强度</span>
        <LmSelect
          v-model:value="params.reasoningEffort"
          :options="PLAYGROUND_REASONING_EFFORT_OPTIONS"
        />
      </div>

      <div class="lm-playground__group-title">采样参数</div>

      <div class="lm-playground__field">
        <div class="lm-playground__label-row">
          <span class="lm-playground__label">温度 Temperature</span>
          <span class="lm-playground__value">{{ params.temperature.toFixed(2) }}</span>
        </div>
        <n-slider v-model:value="params.temperature" :min="0" :max="2" :step="0.05" />
      </div>

      <div class="lm-playground__field">
        <div class="lm-playground__label-row">
          <span class="lm-playground__label">Top P</span>
          <span class="lm-playground__value">{{ params.topP.toFixed(2) }}</span>
        </div>
        <n-slider v-model:value="params.topP" :min="0" :max="1" :step="0.05" />
      </div>

      <div class="lm-playground__field">
        <span class="lm-playground__label">最大输出 Token</span>
        <n-input-number
          v-model:value="params.maxTokens"
          class="lm-playground__number"
          :min="1"
          :max="PLAYGROUND_MAX_TOKENS_LIMIT"
          :step="256"
          :show-button="false"
          placeholder="4096"
        >
          <template #suffix>tokens</template>
        </n-input-number>
      </div>

      <div class="lm-playground__group-title">系统提示词</div>

      <div class="lm-playground__field">
        <div class="lm-playground__presets">
          <n-tag
            v-for="preset in PLAYGROUND_PRESETS"
            :key="preset.label"
            class="lm-playground__preset"
            :bordered="false"
            :type="params.systemPrompt === preset.prompt ? 'primary' : 'default'"
            size="small"
            @click="params.systemPrompt = preset.prompt"
          >
            {{ preset.label }}
          </n-tag>
        </div>
        <n-input
          v-model:value="params.systemPrompt"
          type="textarea"
          class="lm-playground__system"
          :autosize="{ minRows: 3, maxRows: 8 }"
          placeholder="设定模型的角色与行为,如:你是一名资深程序员..."
        />
      </div>
    </aside>

    <!-- 右侧:对话区 -->
    <section
      class="lm-playground__chat"
      :class="{ 'lm-playground__chat--dragging': dragging }"
      @dragover.prevent="dragging = true"
      @dragleave="dragging = false"
      @drop.prevent="handleDrop"
    >
      <header class="lm-playground__chat-header">
        <!-- 窄屏:参数面板开关(桌面隐藏) -->
        <n-button
          v-if="isMobile"
          quaternary
          size="small"
          class="lm-playground__panel-toggle"
          @click="panelOpen = true"
        >
          <template #icon>
            <n-icon :component="OptionsOutline" />
          </template>
          参数
        </n-button>
        <span v-if="quotaText" class="lm-playground__quota">{{ quotaText }}</span>
        <span v-else />
        <div class="lm-playground__chat-actions">
          <n-button quaternary size="small" @click="clear">
            <template #icon>
              <n-icon :component="TrashBinOutline" />
            </template>
            清空
          </n-button>
        </div>
      </header>

      <n-scrollbar
        ref="scrollbarRef"
        class="lm-playground__messages"
        @scroll="handleMessagesScroll"
      >
        <div class="lm-playground__messages-inner">
          <MessageItem
            v-for="item in messages"
            :key="item.id"
            :message="item"
            :streaming="sending && item.status === 'streaming'"
            :actions-disabled="sending"
            @delete="removeMessage(item)"
            @resend="(content) => resendMessage(item, content)"
            @regenerate="regenerateMessage(item)"
          />
        </div>
      </n-scrollbar>

      <footer class="lm-playground__composer">
        <!-- 待发送图片预览条 -->
        <div v-if="pendingImages.length" class="lm-playground__pending">
          <div
            v-for="(image, index) in pendingImages"
            :key="index"
            class="lm-playground__pending-item"
          >
            <img :src="image" alt="待发送图片" />
            <button
              type="button"
              class="lm-playground__pending-remove"
              @click="removePendingImage(index)"
            >
              ×
            </button>
          </div>
        </div>

        <n-input
          v-model:value="input"
          type="textarea"
          class="lm-playground__input"
          :autosize="{ minRows: 2, maxRows: 8 }"
          :placeholder="
            sending
              ? '生成中,可先编辑下一条...'
              : '输入消息,Enter 发送,Shift+Enter 换行,支持粘贴图片'
          "
          @keydown="handleInputKeydown"
          @paste="handlePaste"
        >
          <template #suffix>
            <button
              v-if="sending"
              type="button"
              class="lm-playground__send-btn lm-playground__send-btn--stop"
              title="停止生成"
              @click="stop"
            >
              <n-icon :component="StopOutline" />
            </button>
            <!-- 不可发送时 hover/Enter/点击显示原因 tooltip 与必填错误态 -->
            <n-tooltip v-else :show="sendTipShow" placement="top">
              <template #trigger>
                <span
                  class="lm-playground__send-wrap"
                  @mouseenter="hoveringSend = !canSend"
                  @mouseleave="hoveringSend = false"
                >
                  <button
                    type="button"
                    class="lm-playground__send-btn"
                    :class="{ 'lm-playground__send-btn--disabled': !canSend }"
                    :title="canSend ? '发送' : undefined"
                    @click="handleSendClick"
                  >
                    <n-icon :component="SendOutline" />
                  </button>
                </span>
              </template>
              {{ sendDisabledReason }}
            </n-tooltip>
          </template>
        </n-input>
      </footer>
    </section>
  </div>
</template>

<style scoped lang="scss">
.lm-playground {
  display: flex;
  gap: 1.6rem;
  height: 100%;

  &__panel {
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    width: 28rem;
    padding: 2rem;
    overflow-y: auto;
    background: var(--c-fill-1);
    border-radius: 0.8rem;
  }

  /* 窄屏(≤767px):参数面板转抽屉覆盖层,聊天区占满全宽 */
  &__mask {
    position: fixed;
    inset: 0;
    z-index: 2001;
    background: rgba(0, 0, 0, 0.4);
  }

  &__panel-toggle {
    display: none;
  }

  &__panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1.6rem;
  }

  &__panel-title {
    margin: 0;
    font-size: 1.6rem;
    font-weight: 600;
    color: var(--c-strong);
  }

  &__group-title {
    padding: 1.6rem 0 1rem;
    margin-top: 0.8rem;
    font-size: 1.3rem;
    color: var(--c-secondary);
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
    margin-bottom: 1.6rem;

    &--row {
      gap: 0.4rem;
    }
  }

  &__label-row {
    display: flex;
    justify-content: space-between;
  }

  &__label {
    font-size: 1.4rem;
    color: var(--c-default);

    // 必填星号:与发送拦截条件联动,红色跟随主题 error 色
    &--required::after {
      margin-left: 0.4rem;
      color: rgb(var(--n-error-color));
      content: '*';
    }
  }

  &__value {
    font-size: 1.3rem;
    color: var(--c-secondary);
  }

  &__hint {
    font-size: 1.2rem;
    color: var(--c-tertiary);
  }

  &__number {
    width: 100%;
  }

  &__presets {
    display: flex;
    flex-wrap: wrap;
    gap: 0.8rem;
  }

  &__key-empty {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.8rem;
    padding: 1rem;
    border: 1px dashed var(--c-line-strong);
    border-radius: 0.8rem;
  }

  &__preset {
    cursor: pointer;
  }

  &__system {
    // 系统提示词输入框固定最小高度,避免预设切换时跳动
    :deep(textarea) {
      resize: none;
    }
  }

  &__chat {
    position: relative;
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 0;
    background: var(--c-fill-1);
    border-radius: 0.8rem;
    outline: 2px dashed transparent;
    outline-offset: -0.8rem;
    transition: outline-color 0.2s ease;

    &--dragging {
      outline-color: rgb(var(--n-primary-color));
    }
  }

  &__chat-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1.2rem 2rem;
    border-bottom: 1px solid var(--c-line);
  }

  &__quota {
    font-size: 1.3rem;
    color: var(--c-secondary);
  }

  &__chat-actions {
    display: flex;
    gap: 0.8rem;
  }

  &__messages {
    flex: 1;
    min-height: 0;
  }

  &__messages-inner {
    max-width: 96rem;
    padding: 2rem;
    margin: 0 auto;
  }

  &__composer {
    padding: 1.4rem 2rem 1.6rem;
    border-top: 1px solid var(--c-line);
  }

  &__pending {
    display: flex;
    flex-wrap: wrap;
    gap: 0.8rem;
    margin-bottom: 1rem;
  }

  &__pending-item {
    position: relative;
    width: 6.4rem;
    height: 6.4rem;
    overflow: hidden;
    border: 1px solid var(--c-line-strong);
    border-radius: 0.8rem;

    img {
      width: 100%;
      height: 100%;
      object-fit: cover;
    }
  }

  &__pending-remove {
    position: absolute;
    top: 0;
    right: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 2rem;
    height: 2rem;
    font-size: 1.4rem;
    line-height: 1;
    color: #fff;
    cursor: pointer;
    background: rgba(0, 0, 0, 0.55);
    border: none;
    border-radius: 0 0 0 0.6rem;
  }

  &__input {
    :deep(textarea) {
      resize: none;
    }
  }

  // 发送按钮包裹层:承载 tooltip 触发与 hover 事件(disabled 按钮不派发指针事件)
  &__send-wrap {
    display: inline-flex;
  }

  // 输入框内圆形发送/停止按钮(suffix 插槽)
  &__send-btn {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    width: 3.2rem;
    height: 3.2rem;
    padding: 0;
    font-size: 1.6rem;
    color: var(--c-inverse);
    cursor: pointer;
    background: rgb(var(--n-primary-color));
    border: none;
    border-radius: 50%;
    transition: opacity 0.2s ease;

    &:hover:not(.lm-playground__send-btn--disabled) {
      opacity: 0.85;
    }

    // 禁用态用类而非原生 disabled,保证点击仍派发事件以触发必填提示
    &--disabled {
      cursor: not-allowed;
      opacity: 0.4;
    }

    &--stop {
      background: rgb(var(--n-error-color));
    }
  }

  /* === 窄屏(≤767px):面板抽屉化 ===
   * 面板 fixed 左侧抽屉(默认收起),开关按钮浮出;聊天区 flex:1 占满全宽。
   * 与 LmLayout 侧栏抽屉同交互模型(遮罩+平移),z-index 低于全局弹窗(2002)。 */
  @media (max-width: 767px) {
    position: relative;

    &__panel {
      position: fixed;
      inset-block: 0;
      left: 0;
      z-index: 2002;
      width: 85vw !important;
      max-width: 85vw;
      border-radius: 0 0.8rem 0.8rem 0;
      transform: translateX(-100%);
      transition: transform 0.25s ease;
      box-shadow: 0 0 2.4rem rgba(0, 0, 0, 0.18);

      &--open {
        transform: translateX(0);
      }
    }

    &__panel-toggle {
      display: inline-flex;
    }
  }
}
</style>
