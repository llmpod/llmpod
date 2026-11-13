<script setup lang="ts">
import { computed } from 'vue'
import { AddOutline, TrashOutline } from '@vicons/ionicons5'
import { createModelItem, type EndpointModelItem } from './useStore'

defineOptions({ name: 'EndpointModelRows' })

const props = withDefaults(
  defineProps<{
    /** 行数据（v-model:value） */
    value: EndpointModelItem[]
    /** 上游模型名候选（空=未拉取/拉取失败，纯手填） */
    options?: { label: string; value: string }[]
    /** 上游清单拉取中 */
    loading?: boolean
    /** 上游清单拉取失败：行下方给出「直接手填」引导 */
    failed?: boolean
  }>(),
  {
    options: () => [],
    loading: false,
    failed: false,
  },
)

const emit = defineEmits<{
  'update:value': [rows: EndpointModelItem[]]
  /** 用户真实交互（聚焦/键入模型 ID）：由表单决定是否打上游拉清单 */
  fetch: []
}>()

/** 行下方的一句话说明：清单没取到时不弹 message，只在这里交代「直接手填」 */
const hint = computed(() => (props.failed ? '未取到上游模型清单，直接填写模型 ID 即可' : ''))

/** 改单行字段：整数组替换上行，父级 setValues 才是唯一数据源 */
function patch(index: number, key: keyof EndpointModelItem, value: string): void {
  emit(
    'update:value',
    props.value.map((row, i) => (i === index ? { ...row, [key]: value } : row)),
  )
}

function add(): void {
  emit('update:value', [...props.value, createModelItem()])
}

/** 删一行；删到只剩零行时补一空行，避免出现无可填的空白区 */
function remove(index: number): void {
  const rows = props.value.filter((_, i) => i !== index)
  emit('update:value', rows.length ? rows : [createModelItem()])
}

/**
 * 单行候选：按输入做包含匹配（n-auto-complete 不自带过滤），
 * 并剔除已被其它行占用的模型名——同批重复会被后端整批 422。
 */
function optionsFor(index: number, keyword: string): { label: string; value: string }[] {
  const used = new Set(
    props.value.filter((_, i) => i !== index).map((row) => row.served_model.trim()),
  )
  const kw = keyword.trim().toLowerCase()
  return props.options.filter(
    (option) => !used.has(option.value) && (!kw || option.label.toLowerCase().includes(kw)),
  )
}

/**
 * 候选菜单始终允许弹出:naive-ui 默认只在 value 非空时弹(getShow 缺省为 !!value),
 * 这里改成聚焦就看到已拉取的全部清单,输入时再按关键字收窄。
 */
const alwaysShow = (): boolean => true

/**
 * 菜单宽度跟随输入框(n-auto-complete 内写死 width: target),而模型名常比输入框长,
 * 这里给个下限避免候选被截成「Qwen3.8-27B-AWQ-…」。
 */
const menuProps = { style: { minWidth: '24rem' } }

/**
 * n-auto-complete 的 onFocus 在原始 input 上被 n-input 自身的 handler 覆盖,
 * 所以拉清单同时挂在组件级 focus 与 input 的 mousedown 上:一次交互只打一次上游(表单侧按签名去重)。
 */
const fetchOnMousedown = { onMousedown: () => emit('fetch') }

/** 候选菜单空态：交代当下该等、该填还是该换关键字 */
const emptyHint = computed(() => {
  if (props.loading) return '正在拉取上游模型清单…'
  if (props.failed) return '未取到上游模型清单，直接输入模型 ID 即可'
  if (!props.options.length) return '暂无候选，直接输入模型 ID 即可'
  return '没有匹配的模型，可直接使用输入的模型 ID'
})
</script>

<template>
  <div class="flex w-full flex-col gap-[.8rem]">
    <div
      v-for="(row, index) in value"
      :key="index"
      class="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] items-center gap-[.6rem]"
    >
      <n-auto-complete
        class="w-full"
        :value="row.served_model"
        placeholder="模型 ID，如 gpt-4o"
        :options="optionsFor(index, row.served_model)"
        :loading="loading"
        :get-show="alwaysShow"
        :menu-props="menuProps"
        show-empty
        clearable
        :input-props="fetchOnMousedown"
        @focus="emit('fetch')"
        @update:value="(value: string) => patch(index, 'served_model', value)"
      >
        <template #empty>
          <div class="px-[1rem] py-[1.2rem] text-center text-small text-tertiary">
            {{ emptyHint }}
          </div>
        </template>
      </n-auto-complete>
      <n-input
        class="w-full"
        :value="row.name"
        placeholder="显示名称，留空同模型 ID"
        :maxlength="255"
        @update:value="(value: string) => patch(index, 'name', value)"
      />
      <n-button quaternary title="删除该模型" aria-label="删除该模型" @click="remove(index)">
        <template #icon>
          <n-icon><TrashOutline /></n-icon>
        </template>
      </n-button>
    </div>
    <div class="flex flex-wrap items-center gap-[1rem]">
      <n-button size="small" dashed @click="add">
        <template #icon>
          <n-icon><AddOutline /></n-icon>
        </template>
        添加模型
      </n-button>
      <span v-if="hint" class="text-small text-tertiary">{{ hint }}</span>
    </div>
  </div>
</template>