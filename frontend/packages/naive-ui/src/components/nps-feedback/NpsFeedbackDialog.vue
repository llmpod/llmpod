<script setup lang="ts">
import { computed, ref } from 'vue'
import { NInput, NRate } from 'naive-ui'
import { ChatboxEllipsesOutline } from '@vicons/ionicons5'

/** 低分档追问：问具体原因 */
const ASK_LOW = '具体是哪里让你不太满意？'
/** 高分档追问：问改进建议 */
const ASK_HIGH = '还有哪一点可以做得更好？'
/** 星级直径（px）：设计稿是放大版评分控件，比 naive 默认 large（24）大一倍 */
const STAR_SIZE = 50

const props = defineProps<{
  /**
   * 提交回调：上报等副作用由调用方负责。
   * 抛错时弹窗保持打开，并在下方显示错误提示。
   */
  onSubmit: (score: number, comment: string) => void | Promise<void>
}>()

const score = ref(0)
const comment = ref('')
const submitting = ref(false)
const errorText = ref('')

/** 1–3 星问原因，4–5 星问建议；未打分时用中性提问，不用低分措辞 */
const askText = computed(() => (score.value > 0 && score.value <= 3 ? ASK_LOW : ASK_HIGH))

/**
 * 由 useDialog 的「提交」按钮触发，close 由 useDialog 注入。
 * 先校验、再提交、成功才关；失败保留弹窗并提示。
 */
async function submit(close: () => void): Promise<void> {
  errorText.value = ''
  if (!score.value) {
    errorText.value = '先点亮一颗星星吧～'
    return
  }
  if (submitting.value) return
  submitting.value = true
  try {
    await props.onSubmit(score.value, comment.value.trim())
    close()
  } catch {
    errorText.value = '提交失败，请稍后再试'
  } finally {
    submitting.value = false
  }
}

defineExpose({ onConfirm: submit })

/** 打分后清掉「请先选择一个分数」 */
function handleScoreChange(): void {
  if (errorText.value) errorText.value = ''
}
</script>

<template>
  <div class="flex flex-col gap-[2.4rem]">
    <!-- 评分：星级居中，下方用「两端语义标签 + 中间发丝线」表达推荐度量尺 -->
    <div class="flex flex-col gap-[1.2rem]">
      <div class="flex justify-center">
        <n-rate
          v-model:value="score"
          class="nps-feedback__rate"
          :count="5"
          :size="STAR_SIZE"
          @update:value="handleScoreChange"
        />
      </div>
      <div class="mx-auto flex w-full max-w-[38rem] items-center gap-[1.2rem]">
        <span class="shrink-0 text-sm text-secondary">非常不推荐</span>
        <span class="flex-1 border-t border-line" />
        <span class="shrink-0 text-sm text-secondary">非常推荐</span>
      </div>
    </div>

    <div class="flex flex-col gap-[1.2rem]">
      <div class="flex items-center gap-[.8rem]">
        <n-icon :size="18" class="text-default">
          <ChatboxEllipsesOutline />
        </n-icon>
        <span class="text-base text-default">{{ askText }}</span>
      </div>

      <!-- 评语上限与接口对齐：后端超过 500 字返回 422，这里用 maxlength + 计数提前拦住 -->
      <n-input
        v-model:value="comment"
        type="textarea"
        size="large"
        :autosize="{ minRows: 3, maxRows: 6 }"
        :maxlength="500"
        show-count
        placeholder="选填，一句话也行"
      />

      <p v-if="errorText" class="text-base text-error-text">
        {{ errorText }}
      </p>
    </div>
  </div>
</template>

<style scoped lang="scss">
// 星星：naive 默认未选中是实心灰，这里改成描边空星；选中态保留实心，用圆角笔触 + 同色微光贴合 3D 质感
.nps-feedback__rate {
  // 设计稿星距 26px（默认 6px），配合 50px 星级撑开量尺宽度
  :deep(.n-rate__item:not(:first-child)) {
    margin-left: 2.6rem;
  }

  :deep(.n-rate__item) svg path {
    stroke-linejoin: round;
    stroke-width: 24;
  }

  :deep(.n-rate__item:not(.n-rate__item--active)) svg path {
    fill: none;
    stroke: currentColor;
  }

  :deep(.n-rate__item--active) {
    filter: drop-shadow(0 0 0.6rem currentColor);
  }

  // 窄屏（同 LmDialog 的 767px 断点）：50px × 5 + 26px 间距会撑破近全屏弹窗，按比例缩一档
  @media (max-width: 767px) {
    :deep(.n-rate__item) {
      font-size: 3.6rem;
    }

    :deep(.n-rate__item:not(:first-child)) {
      margin-left: 1.6rem;
    }
  }
}
</style>

<style lang="scss">
/*
 * 只作用于 NPS 弹窗（卡片 class 由 useNpsFeedback 传入），不影响其它 LmDialog：
 * 头部去掉标题下分割线（与内容连成一体）、关闭按钮钉在右上角，底部单按钮居中且不加分割线。
 * 主色不在此处覆盖，跟随应用当前主题（--c-primary / --n-primary-color）。
 */
.lm-dialog.lm-nps-dialog {
  border-radius: 1rem;

  // 背景图铺法：素材已按卡片比例（680×434）重采样，cover 恰好铺满、不产生裁切
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;

  // 右侧留出关闭按钮的位置，避免长标题压到它下面
  .n-card-header {
    position: relative;
    border-bottom: none;
    padding: 1.6rem 5.6rem 0.8rem 2.4rem;
  }

  // 关闭按钮脱离头部流：头部含插画时流内居中会让它落在标题下半区，观感怪；
  // 24px 的落点与内容区左右内边距对齐，和插画左缘形成对称留白
  .n-card-header__close {
    position: absolute;
    top: 2.4rem;
    right: 2.4rem;
    margin: 0;
  }

  .n-card-content {
    padding: 1.2rem 2.4rem 2.4rem;
  }

  // 只剩一个「提交」：居中呈现，去掉与内容之间的分割线
  .n-card__footer {
    display: flex;
    justify-content: center;
    padding: 0 2.4rem 2.4rem;
  }

  .lm-dialog__title {
    font-size: 2rem;
    font-weight: 600;
    line-height: 1.4;
  }

  // 单按钮是弹窗的主操作：横向内边距放到 24px（全局 token 是 12px），并给到 12rem 定宽，
  // 与设计稿的主按钮观感一致；高度沿用全局 32px。颜色与各状态交回主题，不做覆盖。
  .n-card__footer .n-button {
    width: 12rem;
    padding: 0 2.4rem;
    border-radius: 0.6rem;
  }

  // 窄屏（同 LmDialog 的 767px 断点）：插画与内边距一起收一档，把宽度让给标题
  @media (max-width: 767px) {
    .n-card-header {
      padding: 1.6rem 4.8rem 0.8rem 1.6rem;

      .nps-feedback__illustration {
        width: 8rem;
      }
    }

    .n-card-header__close {
      top: 1.6rem;
      right: 1.6rem;
    }

    .n-card-content {
      padding: 1.2rem 1.6rem 1.6rem;
    }

    .n-card__footer {
      padding: 0 1.6rem 1.6rem;
    }
  }
}

/*
 * 背景图：明暗各一张。
 * - 浅色图是半透明叠加层，铺在卡片白底上；叠到深色底会把压缩后的 alpha 放大成脏灰斑块，所以只给浅色用。
 * - 深色图不透明，直接覆盖卡片底色。
 */
html:not(.dark) .lm-dialog.lm-nps-dialog {
  background-image: url('../../assets/nps-feedback/background-light.webp');
}

html.dark .lm-dialog.lm-nps-dialog {
  background-image: url('../../assets/nps-feedback/background-dark.webp');
}
</style>
