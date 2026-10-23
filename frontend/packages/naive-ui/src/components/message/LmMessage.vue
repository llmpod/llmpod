<script setup lang="ts">
import { type VNode } from 'vue'

export type LmMessageType = 'info' | 'success' | 'warning' | 'error' | 'loading'

interface Props {
  type?: LmMessageType
  content?: string
  closable?: boolean
  showIcon?: boolean
  icon?: VNode | (() => VNode)
}

withDefaults(defineProps<Props>(), {
  type: 'info',
  content: '',
  closable: false,
  showIcon: true,
})

defineEmits<{ close: [] }>()
</script>

<template>
  <div :class="['lm-message', `lm-message--${type}-type`]">
    <div
      v-if="showIcon"
      class="lm-message__icon"
    >
      <svg
        v-if="type === 'info'"
        viewBox="0 0 28 28"
        aria-hidden="true"
      >
        <path
          fill="currentColor"
          d="M14,2 C20.6274,2 26,7.37258 26,14 C26,20.6274 20.6274,26 14,26 C7.37258,26 2,20.6274 2,14 C2,7.37258 7.37258,2 14,2 Z M14,11 C13.4477,11 13,11.4477 13,12 L13,12 L13,20 C13,20.5523 13.4477,21 14,21 C14.5523,21 15,20.5523 15,20 L15,20 L15,12 C15,11.4477 14.5523,11 14,11 Z M14,6.75 C13.3096,6.75 12.75,7.30964 12.75,8 C12.75,8.69036 13.3096,9.25 14,9.25 C14.6904,9.25 15.25,8.69036 15.25,8 C15.25,7.30964 14.6904,6.75 14,6.75 Z"
        />
      </svg>
      <svg
        v-else-if="type === 'success'"
        viewBox="0 0 48 48"
        aria-hidden="true"
      >
        <path
          fill="currentColor"
          d="M24,4 C35.045695,4 44,12.954305 44,24 C44,35.045695 35.045695,44 24,44 C12.954305,44 4,35.045695 4,24 C4,12.954305 12.954305,4 24,4 Z M32.6338835,17.6161165 C32.1782718,17.1605048 31.4584514,17.1301307 30.9676119,17.5249942 L30.8661165,17.6161165 L20.75,27.732233 L17.1338835,24.1161165 C16.6457281,23.6279612 15.8542719,23.6279612 15.3661165,24.1161165 C14.9105048,24.5717282 14.8801307,25.2915486 15.2749942,25.7823881 L15.3661165,25.8838835 L19.8661165,30.3838835 C20.3217282,30.8394952 21.0415486,30.8698693 21.5323881,30.4750058 L21.6338835,30.3838835 L32.6338835,19.3838835 C33.1220388,18.8957281 33.1220388,18.1042719 32.6338835,17.6161165 Z"
        />
      </svg>
      <svg
        v-else-if="type === 'warning'"
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path
          fill="currentColor"
          d="M12,2 C17.523,2 22,6.478 22,12 C22,17.522 17.523,22 12,22 C6.477,22 2,17.522 2,12 C2,6.478 6.477,2 12,2 Z M12.0018002,15.0037242 C11.450254,15.0037242 11.0031376,15.4508407 11.0031376,16.0023869 C11.0031376,16.553933 11.450254,17.0010495 12.0018002,17.0010495 C12.5533463,17.0010495 13.0004628,16.553933 13.0004628,16.0023869 C13.0004628,15.4508407 12.5533463,15.0037242 12.0018002,15.0037242 Z M11.99964,7 C11.4868042,7.00018474 11.0642719,7.38637706 11.0066858,7.8837365 L11,8.00036004 L11.0018003,13.0012393 L11.00857,13.117858 C11.0665141,13.6151758 11.4893244,14.0010638 12.0021602,14.0008793 C12.514996,14.0006946 12.9375283,13.6145023 12.9951144,13.1171428 L13.0018002,13.0005193 L13,7.99964009 L12.9932303,7.8830214 C12.9352861,7.38570354 12.5124758,6.99981552 11.99964,7 Z"
        />
      </svg>
      <svg
        v-else-if="type === 'error'"
        viewBox="0 0 48 48"
        aria-hidden="true"
      >
        <path
          fill="currentColor"
          d="M24,4 C35.045695,4 44,12.954305 44,24 C44,35.045695 35.045695,44 24,44 C12.954305,44 4,35.045695 4,24 C4,12.954305 12.954305,4 24,4 Z M17.8838835,16.1161165 L17.7823881,16.0249942 C17.3266086,15.6583353 16.6733914,15.6583353 16.2176119,16.0249942 L16.1161165,16.1161165 L16.0249942,16.2176119 C15.6583353,16.6733914 15.6583353,17.3266086 16.0249942,17.7823881 L16.1161165,17.8838835 L22.233,24 L16.1161165,30.1161165 L16.0249942,30.2176119 C15.6583353,30.6733914 15.6583353,31.3266086 16.0249942,31.7823881 L16.1161165,31.8838835 L16.2176119,31.9750058 C16.6733914,32.3416647 17.3266086,32.3416647 17.7823881,31.9750058 L17.8838835,31.8838835 L24,25.767 L30.1161165,31.8838835 L30.2176119,31.9750058 C30.6733914,32.3416647 31.3266086,32.3416647 31.7823881,31.9750058 L31.8838835,31.8838835 L31.9750058,31.7823881 C32.3416647,31.3266086 32.3416647,30.6733914 31.9750058,30.2176119 L31.8838835,30.1161165 L25.767,24 L31.8838835,17.8838835 L31.9750058,17.7823881 C32.3416647,17.3266086 32.3416647,16.6733914 31.9750058,16.2176119 L31.8838835,16.1161165 L31.7823881,16.0249942 C31.3266086,15.6583353 30.6733914,15.6583353 30.2176119,16.0249942 L30.1161165,16.1161165 L24,22.233 L17.8838835,16.1161165 L17.7823881,16.0249942 L17.8838835,16.1161165 Z"
        />
      </svg>
      <n-spin
        v-else-if="type === 'loading'"
        :size="20"
      />
    </div>
    <div class="lm-message__content">
      {{ content }}
    </div>
    <div
      v-if="closable"
      class="lm-message__close"
      @click="$emit('close')"
    >
      <svg
        viewBox="0 0 24 24"
        width="16"
        height="16"
        aria-hidden="true"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
      >
        <path d="M6 6l12 12M18 6L6 18" />
      </svg>
    </div>
  </div>
</template>

<style scoped lang="scss">
.lm-message {
  box-sizing: border-box;
  display: inline-flex;
  align-items: center;
  flex-wrap: nowrap;
  overflow: hidden;
  max-width: 720px;
  padding: 10px 20px;
  border-radius: var(--lm-message-radius, 3px);
  color: var(--c-default);
  background-color: var(--c-fill-1);
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.08);
  pointer-events: auto;
}

.lm-message__icon {
  flex-shrink: 0;
  margin: 0 10px 0 0;
  width: 20px;
  height: 20px;
  font-size: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.lm-message__icon svg {
  width: 1em;
  height: 1em;
  display: block;
}

.lm-message--info-type .lm-message__icon {
  color: rgb(var(--n-info-color));
}

.lm-message--success-type .lm-message__icon {
  color: rgb(var(--n-success-color));
}

.lm-message--warning-type .lm-message__icon {
  color: rgb(var(--n-warning-color));
}

.lm-message--error-type .lm-message__icon {
  color: rgb(var(--n-error-color));
}

.lm-message--loading-type .lm-message__icon {
  color: rgb(var(--n-primary-color));
}

.lm-message__content {
  display: inline-block;
  max-width: 100%;
  font-size: 14px;
  line-height: 1.6;
  /* 长文本(默认不自动关闭场景)在提示内正常换行,配合 pre-line 保留后端多行报错 */
  white-space: pre-line;
  word-break: break-word;
  overflow-wrap: anywhere;
}

.lm-message__close {
  flex-shrink: 0;
  margin: 0 0 0 10px;
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: var(--c-icon);
  transition: color 0.3s var(--n-bezier, ease);
}

.lm-message__close:hover {
  color: var(--c-default);
}
</style>

<style lang="scss">
/* MessageHost 容器:fixed 居中,多消息垂直堆叠 */
.lm-message-container {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 6000;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  pointer-events: none;
}

/* TransitionGroup(name="lm-message") 进出场动画 */
.lm-message-enter-active,
.lm-message-leave-active {
  transition:
    opacity 0.3s var(--n-bezier, ease),
    transform 0.3s var(--n-bezier, ease);
}

.lm-message-enter-from,
.lm-message-leave-to {
  opacity: 0;
  transform: scale(0.85);
}
</style>
