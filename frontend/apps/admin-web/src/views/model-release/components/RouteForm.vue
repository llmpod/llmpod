<script setup lang="ts">
import { computed, h, onBeforeUnmount, ref } from 'vue'
import { NAutoComplete, NIcon, type SelectOption } from 'naive-ui'
import { ChevronDown } from '@vicons/ionicons5'
import {
  useForm,
  FormInput,
  FormSwitch,
  FormInputNumber,
  FormGroup,
  FormSelect,
  FormCustom,
  FormCheckboxGroup,
  useDialog,
  useDialogClose,
  message,
} from '@repo/hooks'
import { listEndpoints, type EndpointData } from '@/api/endpoint'
import EndpointForm from './endpoint/EndpointForm.vue'
import { useStore as useEndpointStore } from './endpoint/useStore'
import {
  useStore,
  toModelNameOptions,
  toProviderOptions,
  defaultBindingEndpointIds,
  hasAdvancedSettings,
  modelTypeOptions,
  modelTagOptions,
  type RouteFormValues,
  type RouteData,
} from '../useStore'

const props = defineProps<{
  /** 编辑回填；新建时为 undefined */
  initial?: RouteData
}>()

const close = useDialogClose()
const { create, update } = useStore()
const isEdit = computed(() => !!props.initial?.id)

/** 供应商端点列表(异步加载) */
const endpointList = ref<EndpointData[]>([])
/** 上游模型名(served_model)候选选项(同名去重) */
const endpointNameOptions = ref<SelectOption[]>([])
const optionsLoading = ref(true)

/** 端点 store:订阅增删改,在弹窗内创建端点后即时刷新本表单选项 */
const { subscribe: subscribeEndpoints } = useEndpointStore()

/** 拉取端点列表并刷新候选选项 */
async function loadEndpointOptions() {
  try {
    const list = await listEndpoints()
    endpointList.value = list
    endpointNameOptions.value = toModelNameOptions(list)
  } catch {
    // 加载失败由 HTTP 层提示,选项留空,引导入口仍可用
  } finally {
    optionsLoading.value = false
  }
}

void loadEndpointOptions()

// 创建端点成功(store notify)后自动刷新选项,新端点立即可选
const unsubscribeEndpoints = subscribeEndpoints(() => void loadEndpointOptions())
onBeforeUnmount(unsubscribeEndpoints)

/** 挂载供应商选项(名称+供应商区分同名端点) */
const providerOptions = computed<SelectOption[]>(() => toProviderOptions(endpointList.value))

/** 直接打开「接入端点」弹窗(与 EndpointManage.openCreate 同模式);创建成功经 store 订阅刷新选项 */
function openEndpointCreate() {
  useDialog({
    title: '接入端点',
    component: EndpointForm,
    area: 48,
    btn: { confirmText: '创建', cancelText: '取消' },
    dialogProps: { maskClosable: false },
  })
}

/** 非负数字校验（空串放行=不计费/不改） */
const decimalRule = {
  validator: (_rule: unknown, value: string) => !value || /^\d+(\.\d+)?$/.test(value),
  message: '请输入非负数字',
  trigger: 'blur' as const,
}

/**
 * 模型描述起的补充设置(描述/标签/挂载供应商/定价/failover)默认收起缩短表单;
 * 编辑回填有任一值时默认展开,避免已设值不可见。
 */
const showMore = ref(hasAdvancedSettings(props.initial))

/** 挂载供应商仅创建模式展示,且随补充设置折叠区一起收起 */
const showBindingEndpoints = computed(() => showMore.value && !isEdit.value)

/** 表单底部「查看更多/收起」入口(点击切换补充设置显隐);热区仅图标+文字,不占整行 */
const renderMoreToggle = () =>
  h('div', { class: 'my-[.6rem]' }, [
    h(
      'span',
      {
        class:
          'inline-flex cursor-pointer select-none items-center gap-[.4rem] text-base text-secondary transition-colors hover:text-primary',
        onClick: () => {
          showMore.value = !showMore.value
        },
      },
      [
        // 收起时箭头朝右(点击展开),展开后朝下(点击收起)
        h(
          NIcon,
          {
            class: showMore.value
              ? 'transition-transform'
              : 'transition-transform -rotate-90',
          },
          { default: () => h(ChevronDown) },
        ),
        showMore.value ? '收起' : '查看更多',
      ],
    ),
  ])

/**
 * 候选菜单是否已"武装"(允许展示):
 * 弹窗打开瞬间 n-modal 会对首个输入框程序化自动聚焦,此时弹窗入场动画未结束,
 * 菜单 follower 按动画中间态定位会错位;故仅当用户真实交互(点击输入框/键入)后才放行菜单。
 */
const menuArmed = ref(false)

/** 按输入包含匹配过滤上游模型名候选(空输入展示全部;n-auto-complete 不自带过滤) */
function filterNameOptions(keyword: string): { label: string; value: string }[] {
  const kw = keyword.trim().toLowerCase()
  const all = endpointNameOptions.value.map((o) => ({
    label: String(o.label),
    value: String(o.value),
  }))
  if (!kw) return all
  return all.filter((o) => o.label.toLowerCase().includes(kw))
}

const { LmForm, submit, params } = useForm<RouteFormValues>({
  data: () => ({
    id: props.initial?.id,
    name: props.initial?.name ?? '',
    // 模型类别缺省 llm(与后端默认一致)
    model_type: props.initial?.model_type ?? 'llm',
    description: props.initial?.description ?? '',
    context_length: props.initial?.context_length ?? null,
    tags: props.initial?.tags ?? [],
    input_price_per_mtok: props.initial?.input_price_per_mtok ?? '',
    output_price_per_mtok: props.initial?.output_price_per_mtok ?? '',
    cached_input_price_per_mtok: props.initial?.cached_input_price_per_mtok ?? '',
    // 新建默认开启失败自动切换,最多切 3 次(编辑回填原值,含 false/0)
    failover_enabled: props.initial?.failover_enabled ?? true,
    failover_max_retries: props.initial?.failover_max_retries ?? 3,
    binding_endpoint_ids: [],
  }),
  options: () => [
    // 原生 NAutoComplete 自定义字段:允许从端点候选中选,也允许自定义输入
    FormCustom(
      (ctx) =>
        h(NAutoComplete, {
          value: ctx.model.name,
          'onUpdate:value': (v: string) => ctx.setValues({ name: v }),
          placeholder: '输入对外模型名',
          clearable: true,
          loading: optionsLoading.value,
          // 武装后才允许弹候选:聚焦即出全量,输入时按包含匹配过滤
          getShow: () => menuArmed.value,
          // 点击输入框/键入 = 用户真实交互,此时弹窗动画已结束,定位正常
          onMousedown: () => {
            menuArmed.value = true
          },
          onInput: () => {
            menuArmed.value = true
          },
          options: filterNameOptions(ctx.model.name ?? ''),
        }),
      {
        wrapFormItem: true,
        label: '对外模型名',
        field: 'name',
        rules: [{ required: true, message: '请输入对外模型名', trigger: ['blur', 'change'] }],
      },
    ),
    FormSelect('模型类型', 'model_type', modelTypeOptions, {
      attrs: { placeholder: '选择模型类别（默认语言模型）' },
    }),
    FormInputNumber('最大上下文长度(tokens)', 'context_length', {
      attrs: {
        placeholder: '空=未设置',
        min: 1,
        precision: 0,
        clearable: true,
        class: 'w-full',
      },
    }),
    // 查看更多/收起入口:始终在表单展开项头部
    FormCustom(renderMoreToggle),
    // ---- 补充设置:默认收起,底部「查看更多」展开 ----
    FormInput('模型描述', 'description', {
      visible: showMore,
      attrs: {
        placeholder: '模型能力一句话简介，展示在租户端模型中心（可空）',
        type: 'textarea',
        maxlength: 500,
        showCount: true,
        rows: 2,
      },
    }),
    FormCheckboxGroup('能力标签', 'tags', modelTagOptions, { visible: showMore }),
    FormSelect('挂载供应商', 'binding_endpoint_ids', providerOptions.value, {
      // 仅创建时可随请求挂初始成员;编辑模式的成员调整走供应商抽屉
      visible: showBindingEndpoints,
      attrs: {
        placeholder: '选择要挂到该模型的供应商端点，可多选',
        multiple: true,
        filterable: true,
        maxTagCount: 'responsive',
        loading: optionsLoading.value,
      },
      controlSlots: {
        action: () =>
          h('div', { class: 'flex items-center px-[1rem] py-[.6rem]' }, [
            h(
              'span',
              {
                class: 'text-primary text-base hover:opacity-[0.9] cursor-pointer',
                onClick: () => openEndpointCreate(),
              },
              { default: () => '添加供应商' },
            ),
          ]),
      },
    }),
    FormGroup(
      [
        FormInput('输入定价(/百万token,元)', 'input_price_per_mtok', {
          rules: [decimalRule],
          itemProps: { class: 'flex-1' },
          attrs: { placeholder: '空=不计费；embeddings/rerank 全量 token 同此价', type: 'text' },
        }),
        FormInput('输出定价(/百万token,元)', 'output_price_per_mtok', {
          rules: [decimalRule],
          itemProps: { class: 'flex-1' },
          attrs: { placeholder: '空=不计费', type: 'text' },
        }),
      ],
      { visible: showMore },
    ),
    FormInput('缓存命中定价(/百万token,元)', 'cached_input_price_per_mtok', {
      visible: showMore,
      rules: [decimalRule],
      attrs: { placeholder: '空=按全价计', type: 'text' },
    }),
    FormGroup(
      [
        FormSwitch('失败自动切换(failover)', 'failover_enabled', {
          itemProps: { class: 'flex-1' },
        }),
        FormInputNumber('切换次数上限', 'failover_max_retries', {
          itemProps: { class: 'flex-1' },
          attrs: { placeholder: '0=不切换', min: 0, class: 'w-full' },
        }),
      ],
      { visible: showMore },
    ),
  ],
  layout: { labelPlacement: 'top' },
  submit: async (model, validate) => {
    await validate()
    if (isEdit.value) {
      const { name } = await update(model)
      message.success(`已更新：${name}`)
    } else {
      // 即建即上架;name 重复 409 由 HTTP 层提示,表单保留可改名重试
      const { name, bindingCount } = await create(model)
      message.success(
        bindingCount ? `已发布：${name}（挂载 ${bindingCount} 家供应商）` : `已发布：${name}`,
      )
    }
    close()
  },
})

// 创建模式:选中模型后默认全选支持该模型的全部端点(用户可再手动增删;
// 端点列表晚到时也会补一次同步)
watch(
  [() => params().name, endpointList],
  ([name]) => {
    if (isEdit.value || !name) return
    params().binding_endpoint_ids = defaultBindingEndpointIds(endpointList.value, name)
  },
)

defineExpose({
  onConfirm: (dialogClose: () => void) => submit().then(dialogClose, () => {}),
})
</script>

<template>
  <LmForm class="pt-[.75rem]" />
</template>
