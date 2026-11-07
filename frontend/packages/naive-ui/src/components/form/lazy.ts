import { defineAsyncComponent, h } from 'vue'

/**
 * 低频重字段的按需加载入口。
 *
 * LmDatePicker 依赖 date-fns(约 200KB 源码),LmTreeSelect 依赖树形检索逻辑。
 * 若它们与基础字段一起被静态导入,任何使用 useForm 的页面(例如只用用户名/密码
 * 的登录页)都会被迫把整套日期依赖打进首屏 chunk —— 实测登录页因此多下载约
 * 51KB(gzip)的日期组件与 locale 数据。
 *
 * 这里用 defineAsyncComponent 把模块边界切开:组件代码只在字段真实渲染时才加载,
 * 基础表单首屏不再背负 date-fns。同步导出(LmDatePicker 等)保持不变,供既有
 * 直接引用与测试使用。
 *
 * 启用范围仅限上面两个低频重字段。LmInputNumber 不在此列:它不依赖任何重量级库,
 * 切走的包装层仅约 0.5KB,却属于表单最高频字段,异步化只会让每个含数字输入的表单
 * 多一层加载边界,收益与代价不成比例,故保持同步引入。
 *
 * 使用约束:异步字段首次渲染的那一帧尚未挂载真实控件,依赖其内部控件的
 * n-form-item 校验须等加载完成。因此只对低频、且不属于"首屏即校验"的字段启用;
 * 输入框/密码/下拉等高频首屏字段仍保持同步引入。
 *
 * 占位对齐:骨架与失败兜底都用 var(--n-blank-height) 撑高,该变量由 n-form-item
 * 提供且默认等于对应 size 的控件高度,所以加载期不会造成表单高度塌陷/回弹。
 */
const FieldSkeleton = () =>
  h('div', {
    'aria-hidden': 'true',
    style:
      'width: 100%; min-height: var(--n-blank-height, 2.125rem); border-radius: 0.1875rem; background: currentColor; opacity: 0.08;',
  })

const FieldLoadFailed = () =>
  h(
    'div',
    {
      role: 'alert',
      style:
        'width: 100%; min-height: var(--n-blank-height, 2.125rem); display: flex; align-items: center; font-size: 0.75rem; opacity: 0.6;',
    },
    '字段加载失败,请刷新重试',
  )

export const LmDatePickerAsync = defineAsyncComponent({
  loader: () => import('./LmDatePicker.vue'),
  // delay 置 0:默认 200ms 内不渲染任何内容,弱网下表现为"控件位置空白且点不动"
  delay: 0,
  loadingComponent: FieldSkeleton,
  errorComponent: FieldLoadFailed,
})

export const LmTreeSelectAsync = defineAsyncComponent({
  loader: () => import('./LmTreeSelect.vue'),
  delay: 0,
  loadingComponent: FieldSkeleton,
  errorComponent: FieldLoadFailed,
})
