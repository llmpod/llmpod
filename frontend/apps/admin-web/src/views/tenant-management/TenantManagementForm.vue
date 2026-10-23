<script setup lang="ts">
import {
  useForm,
  FormInput,
  FormPassword,
  FormSelect,
  FormCustom,
  useDialogClose,
  message,
} from '@repo/hooks'
import { NIcon } from 'naive-ui'
import { ChevronDown } from '@vicons/ionicons5'
import {
  useStore,
  type TenantFormValues,
  type TenantFormInitial,
  type BlockedRouteOption,
} from './useStore'

/** 编辑回填数据;新增时为 undefined */
const props = defineProps<{
  initial?: TenantFormInitial
}>()

const isEdit = !!props.initial?.id

const close = useDialogClose()
const { save, loadBlockedRouteOptions } = useStore()

/** 低频字段(超管邮箱/手机号、模型封禁)默认收起缩短表单;编辑回填已有封禁时默认展开,避免已设值不可见 */
const showAdvanced = ref(!!props.initial?.blocked_models?.length)

/** 表单底部「展开更多/收起」入口(点击切换低频字段显隐);热区仅图标+文字,不占整行 */
const renderAdvancedToggle = () =>
  h('div', { class: 'my-[.6rem]' }, [
    h(
      'span',
      {
        class:
          'inline-flex cursor-pointer select-none items-center gap-[.4rem] text-base text-secondary transition-colors hover:text-primary',
        onClick: () => {
          showAdvanced.value = !showAdvanced.value
        },
      },
      [
        // 收起时箭头朝右(点击展开),展开后朝下(点击收起)
        h(
          NIcon,
          {
            class: showAdvanced.value
              ? 'transition-transform'
              : 'transition-transform -rotate-90',
          },
          { default: () => h(ChevronDown) },
        ),
        showAdvanced.value ? '收起' : '展开更多',
      ],
    ),
  ])

/** 模型封禁候选(已发布路由全集);弹窗挂载即拉取,失败静默由 HTTP 层统一提示 */
const blockedRouteOptions = ref<BlockedRouteOption[]>([])
onMounted(() => {
  loadBlockedRouteOptions()
    .then((options) => {
      blockedRouteOptions.value = options
    })
    .catch(() => {})
})
const blockedRouteSelectOptions = computed(() =>
  blockedRouteOptions.value.map((o) => ({ label: o.name, value: o.route_id })),
)

const { LmForm, submit } = useForm<TenantFormValues>({
  data: () => ({
    id: props.initial?.id,
    name: props.initial?.name ?? '',
    admin_username: '',
    admin_password: '',
    admin_email: '',
    admin_phone: '',
    // 勾选=封禁;编辑由回填的封禁集推导勾选初值
    blocked_route_ids: props.initial?.blocked_models?.map((m) => m.route_id) ?? [],
  }),
  options: () => [
    FormInput('租户名称', 'name', {
      rules: [{ required: true, message: '请输入租户名称', trigger: 'blur' }],
      attrs: { placeholder: '租户名称(唯一)' },
    }),
    // 新增态同步创建租户超管(后端写死绑 tenant_admin 角色,建好即可登录);编辑态不展示
    ...(isEdit
      ? []
      : [
          FormInput('超管用户名', 'admin_username', {
            rules: [{ required: true, message: '请输入超管用户名', trigger: 'blur' }],
            attrs: { placeholder: '租户超管登录名' },
          }),
          FormPassword('超管密码', 'admin_password', {
            rules: [{ required: true, message: '请输入超管密码', trigger: 'blur' }],
            attrs: { placeholder: '租户超管登录密码' },
          }),
        ]),
    // 展开更多/收起入口:始终在表单展开项头部
    FormCustom(renderAdvancedToggle),
    // ---- 低频字段:默认收起,底部「展开更多」展开 ----
    ...(isEdit
      ? []
      : [
          FormInput('超管邮箱', 'admin_email', {
            visible: showAdvanced,
            attrs: { placeholder: '可选' },
            rules: [
              {
                trigger: 'blur',
                validator: (_rule, value: string) =>
                  !value ||
                  /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) ||
                  new Error('邮箱格式不正确'),
              },
            ],
          }),
          FormInput('超管手机号', 'admin_phone', {
            visible: showAdvanced,
            attrs: { placeholder: '可选' },
          }),
        ]),
    FormSelect('模型封禁', 'blocked_route_ids', blockedRouteSelectOptions.value, {
      visible: showAdvanced,
      attrs: {
        placeholder: '搜索并选择要封禁的模型(留空=全部可用)',
        multiple: true,
        filterable: true,
        maxTagCount: 'responsive',
      },
    }),
  ],
  layout: { labelWidth: 90 },
  submit: async (model, validate) => {
    // 校验失败会 throw,弹窗保留
    await validate()
    // 保存 + 提示 + 关闭均封装在组件内部,不依赖父级回调
    const { mode, name } = await save(model)
    message.success(mode === 'create' ? `已新增:${name}` : `已更新:${name}`)
    close()
  },
})

// useDialog 点「确定」时默认读取该钩子;成功才关,失败保留弹窗
defineExpose({
  onConfirm: (close: () => void) => submit().then(close, () => {}),
})
</script>

<template>
  <LmForm class="pt-[.75rem]" />
</template>
