<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, computed } from 'vue'
import {
  CalendarClearOutline,
  CheckmarkCircleOutline,
  GitBranchOutline,
  OpenOutline,
  Refresh,
  RocketOutline,
  ScaleOutline,
  ServerOutline,
  SparklesOutline,
  SwapHorizontalOutline,
} from '@vicons/ionicons5'
import { useBrand, useVersionCheck, message, DEFAULT_SITE_DESCRIPTION } from '@repo/hooks'
import { checkUpdate, getServerVersion } from '@/api/version'
import { sanitizeNotesHtml } from './notes-html'

const { brandConfig } = useBrand()

/**
 * logo 为 Emoji/URL 二选一:URL/data: 走图片渲染,其余按文本(emoji)渲染。
 * 品牌未自定义 logo 时渲染产品默认 logo,并挂 hover 触发的 Lottie 动效
 * (素材 public/animations/logo.json);已自定义时保持原样,不因 hover 被产品 logo 覆盖。
 */
const isImageLogo = computed(() =>
  /^(https?:)?\/\/|^(data:image|\/)/i.test(brandConfig.value.logo?.trim() || ''),
)

/** hover Lottie 素材地址:带 BASE_URL(admin 端为 /admin/),避免裸绝对路径在子路径部署下 404 */
const logoLottieSrc = `${import.meta.env.BASE_URL}animations/logo.json`

/** 独立实例化版本检查:storageKey 与 BasicLayout 一致,localStorage 同文档多实例自动同步红点状态 */
const { hasNewVersion, currentVersion, latestVersion, updateInfo, silentCheck, manualCheck } = useVersionCheck({
  storageKey: 'llmpod:admin:update-viewed',
  fetchLatest: checkUpdate,
})

/** 展示版本:优先注入变量 serverVersion(无需请求),兜底接口 current_version,再取不到显示 — */
const displayVersion = computed(() => getServerVersion() ?? (currentVersion.value || '—'))

/** 上游报告有新版本(与是否已查看无关):每次检查/进入页面都展示更新内容 */
const showUpdatePanel = hasNewVersion

/** 新版本附加信息:更新说明(富文本,白名单消毒后 v-html 渲染) / 发布日期 / 详情链接 */
const releaseUrl = computed(() => updateInfo.value?.release_url ?? '')
/** 发布日期(ISO 8601 截 yyyy-MM-dd 粒度即可) */
const publishedDate = computed(() => updateInfo.value?.published_at?.slice(0, 10) ?? '')

/**
 * 更新日志 HTML:notes 来自官网 manifest 上游,内容不可信,渲染前经
 * sanitizeNotesHtml 白名单消毒(标签/属性级 + style 声明级,颜色可控制,
 * 脚本/外链引用/定位类载体一律剥离),详见 notes-html.ts。
 */
const notesHtml = computed(() => sanitizeNotesHtml(updateInfo.value?.notes ?? ''))

/** 进入页面时静默检查一次,刷新红点/新版本面板状态,不弹提示 */
onMounted(() => {
  silentCheck().catch(() => {})
})

const checking = ref(false)
/** 最小 loading 展示时长:接口返回太快时按钮动画一闪而过观感差,兜底至少转 500ms */
const MIN_CHECKING_MS = 500
async function handleCheckUpdate() {
  checking.value = true
  const minDelay = new Promise<void>(resolve => setTimeout(resolve, MIN_CHECKING_MS))
  try {
    // 手动检查成功/失败提示由 useVersionCheck 内部统一处理,这里只兜底异常
    await manualCheck()
  } catch {
    message.error('检查更新失败,请稍后重试')
  } finally {
    await minDelay
    checking.value = false
  }
}

/** 更新日志容器点击委托:消毒放行的 <a> 统一强制新窗口打开(比逐节点改属性更稳,不受 hook 移除影响) */
function handleNotesClick(event: MouseEvent) {
  const anchor = (event.target as HTMLElement).closest<HTMLAnchorElement>('a[href]')
  if (!anchor) return
  event.preventDefault()
  window.open(anchor.href, '_blank', 'noopener,noreferrer')
}

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
      <!-- 品牌标识与版本 Hero -->
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

        <!-- 版本标签 & 检查更新按钮 -->
        <div class="mt-[1.6rem] flex items-center justify-center gap-[1rem]">
          <n-tag
            round
            :bordered="false"
            type="primary"
            size="small"
            class="font-medium"
          >
            <template #icon>
              <n-icon :size="13">
                <SparklesOutline />
              </n-icon>
            </template>
            v{{ displayVersion }}
          </n-tag>
          <n-tag
            v-if="showUpdatePanel"
            round
            :bordered="false"
            type="success"
            size="small"
          >
            发现新版本 v{{ latestVersion }}
          </n-tag>
          <n-button
            size="small"
            secondary
            round
            :loading="checking"
            @click="handleCheckUpdate"
          >
            <template #icon>
              <n-icon><Refresh /></n-icon>
            </template>
            检查更新
          </n-button>
        </div>
      </div>

      <!-- 新版本说明(上游报告有更新即展示,与红点已查看状态无关):主色软底面板,作为本页唯一行动区点睛 -->
      <div
        v-if="showUpdatePanel"
        class="mt-[2rem] rounded-xl bg-primary-soft px-[2rem] py-[1.6rem] text-left"
      >
        <div class="flex flex-wrap items-center gap-x-[.8rem] gap-y-[.4rem]">
          <n-icon
            :size="18"
            class="text-primary"
          >
            <RocketOutline />
          </n-icon>
          <span class="text-small font-bold text-strong">
            新版本 <span class="text-primary">v{{ latestVersion }}</span> 可用
          </span>
          <span
            v-if="publishedDate"
            class="ml-auto flex items-center gap-[.4rem] text-small text-secondary tabular-nums"
          >
            <n-icon
              :size="14"
              class="text-icon"
            >
              <CalendarClearOutline />
            </n-icon>
            {{ publishedDate }} 发布
          </span>
        </div>
        <!-- eslint-disable vue/no-v-html --><!-- 内容已经过 DOMPurify 白名单消毒,消毒逻辑见 notes-html.ts -->
        <div
          v-if="notesHtml"
          class="update-notes mt-[1rem] max-h-[24rem] overflow-y-auto pr-[1rem] text-small text-default leading-[1.6]"
          @click="handleNotesClick"
          v-html="notesHtml"
        />
        <!-- eslint-enable vue/no-v-html -->
        <a
          v-if="releaseUrl"
          :href="releaseUrl"
          target="_blank"
          rel="noopener noreferrer"
          class="mt-[1rem] inline-flex items-center gap-[.4rem] text-small font-medium text-primary transition-opacity hover:opacity-80"
        >
          查看更新详情
          <n-icon :size="14">
            <OpenOutline />
          </n-icon>
        </a>
      </div>

      <!-- 信息条目列表 -->
      <div class="mt-[3.2rem] divide-y divide-line rounded-xl border border-line bg-fill-2 px-[2rem]">
        <div class="flex items-center justify-between py-[1.4rem]">
          <span class="flex items-center gap-[.8rem] text-small text-secondary">
            <n-icon
              :size="16"
              class="text-primary"
            >
              <GitBranchOutline />
            </n-icon>
            当前版本
          </span>
          <span class="text-small font-medium text-strong tabular-nums">
            v{{ displayVersion }}
          </span>
        </div>

        <div class="flex items-center justify-between py-[1.4rem]">
          <span class="flex items-center gap-[.8rem] text-small text-secondary">
            <n-icon
              :size="16"
              class="text-success"
            >
              <CheckmarkCircleOutline />
            </n-icon>
            最新版本
          </span>
          <span class="text-small font-medium text-strong tabular-nums">
            {{ showUpdatePanel ? `v${latestVersion}` : '已是最新版本' }}
          </span>
        </div>

        <div class="flex items-center justify-between py-[1.4rem]">
          <span class="flex items-center gap-[.8rem] text-small text-secondary">
            <n-icon
              :size="16"
              class="text-primary"
            >
              <ServerOutline />
            </n-icon>
            推理引擎
          </span>
          <span class="text-small font-medium text-default">
            vLLM / SGLang / llama.cpp
          </span>
        </div>

        <div class="flex items-center justify-between py-[1.4rem]">
          <span class="flex items-center gap-[.8rem] text-small text-secondary">
            <n-icon
              :size="16"
              class="text-primary"
            >
              <SwapHorizontalOutline />
            </n-icon>
            接口协议
          </span>
          <span class="text-small font-medium text-default">
            OpenAI Compatible API / Claude API
          </span>
        </div>

        <div class="flex items-center justify-between py-[1.4rem]">
          <span class="flex items-center gap-[.8rem] text-small text-secondary">
            <n-icon
              :size="16"
              class="text-primary"
            >
              <ScaleOutline />
            </n-icon>
            开源协议
          </span>
          <a
            href="https://www.gnu.org/licenses/agpl-3.0.html"
            target="_blank"
            rel="noopener noreferrer"
            class="flex items-center gap-[.4rem] text-small font-medium text-primary transition-opacity hover:opacity-80"
          >
            AGPL-3.0
            <n-icon :size="13">
              <OpenOutline />
            </n-icon>
          </a>
        </div>
      </div>
    </div>
  </LmCard>
</template>

<style scoped>
/* 更新日志富文本(v-html 注入,后代无 scoped 属性,子选择器必须 :deep()) */
.update-notes {
  > :deep(:first-child) {
    margin-top: 0;
  }

  > :deep(:last-child) {
    margin-bottom: 0;
  }

  :deep(p) {
    margin: 0 0 .6em;
  }

  :deep(ul),
  :deep(ol) {
    padding-left: 1.6em;
    margin: .4em 0;
  }

  :deep(li) {
    margin: .2em 0;
  }

  :deep(a) {
    color: rgb(var(--n-primary-color));
    text-decoration: none;

    &:hover {
      text-decoration: underline;
    }
  }

  :deep(code) {
    padding: .1em .4em;
    font-size: 1.3rem;
    font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace;
    background: var(--c-fill-4);
    border-radius: .4rem;
  }

  :deep(pre) {
    padding: 1rem 1.2rem;
    margin: .4em 0;
    overflow-x: auto;
    background: var(--c-fill-4);
    border-radius: .6rem;

    code {
      padding: 0;
      background: transparent;
    }
  }

  :deep(blockquote) {
    padding: .3em .9em;
    margin: .4em 0;
    color: var(--c-secondary);
    border-left: .3rem solid var(--c-line-strong);

    > p:last-child {
      margin-bottom: 0;
    }
  }

  :deep(img) {
    max-width: 100%;
    border-radius: .4rem;
  }

  :deep(h1),
  :deep(h2),
  :deep(h3),
  :deep(h4),
  :deep(h5),
  :deep(h6) {
    margin: .8em 0 .4em;
    font-weight: 600;
    color: var(--c-strong);

    &:first-child {
      margin-top: 0;
    }
  }

  :deep(hr) {
    margin: .8em 0;
    border: none;
    border-top: 1px solid var(--c-line);
  }
}

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
