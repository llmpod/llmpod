<script setup lang="ts">
import { useTheme } from '@repo/hooks'
import LoginBackground from './components/LoginBackground.vue'
import { useController } from './useController'

const { LmForm, submit, submitting, maintenanceActive, maintenanceMessage } = useController()
// 后端下发登录页背景(缓存于 app_remote_theme,刷新不闪);未下发时保持默认星空背景
const { loginBackground, loginPrimary, remoteTheme } = useTheme()
// 背景优先级:背景图 > 登录页主色(纯色) > 默认星空;主色仅显式配置时启用纯色背景
const bgType = computed(() => {
  if (loginBackground.value) return 'image'
  if (remoteTheme.value.loginPrimary) return 'color'
  return 'starfield'
})
</script>

<template>
  <div class="bg-brand-dark relative flex h-screen items-center justify-center overflow-hidden">
    <LoginBackground :type="bgType" :image="loginBackground" :color="loginPrimary" />
    <div class="bg-fill-1 rounded-[1.2rem] min-h-[52rem] w-[88rem] max-w-92vw relative flex overflow-hidden shadow-2xl">
      <!-- 左侧品牌区:原 login-bg-left.svg 插画的动态化版本(Lottie,几何/配色与原图一致);
           窄屏(<lg)隐藏避免挤压表单;加载期间/失败时回落原静态图(fallback 插槽),就绪后淡入覆盖 -->
      <div class="w-[48rem] relative shrink-0 max-lg:hidden overflow-hidden">
        <LmLottie
          src="/animations/login-dashboard.json"
          width="100%"
          height="100%"
          fit="slice"
        >
          <img
            src="/images/login-bg-left.svg"
            alt="LLMPod"
            class="h-full w-full object-cover"
          />
        </LmLottie>
      </div>

      <!-- 右侧登录表单 -->
      <div class="px-[2.4rem] md:px-[5.6rem] flex flex-1 flex-col justify-center py-[3.2rem]">
        <div class="mb-[2.8rem] flex flex-col items-center">
          <img src="/logo.ico" alt="LLMPod" class="h-[4.4rem] w-[4.4rem] text-primary mb-[1.2rem]" />
          <h1 class="text-strong text-xl font-semibold">登录您的LLMPod</h1>
        </div>

        <!-- 维护期横幅:提交前读注入种子常显,登录被拒后刷新文案 -->
        <n-alert v-if="maintenanceActive" type="warning" class="mb-[1.6rem]">
          {{ maintenanceMessage }}
        </n-alert>

        <LmForm @keyup.enter="submit" />

        <n-button
          type="primary"
          class="mt-[6.5rem]"
          size="large"
          block
          :loading="submitting"
          @click="submit"
        >
          登录
        </n-button>

        <!-- 企业SSO登录:功能未上线,暂时隐藏入口(handleSsoLogin 保留在 useController 中)
        <n-button text size="small" class="mx-auto mt-[2rem] text-secondary" @click="handleSsoLogin">
          企业SSO登录
          <template #icon>
            <svg
              class="h-[1.2rem] w-[1.2rem]"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M9 6l6 6-6 6" />
            </svg>
          </template>
        </n-button>
        -->
      </div>
    </div>
  </div>
</template>
