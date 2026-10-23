/**
 * 复制文本到剪贴板。
 *
 * 优先走异步 Clipboard API(仅安全上下文 HTTPS/localhost 可用);
 * 非 HTTPS 部署等场景 navigator.clipboard 不存在或调用被拒时,
 * 回退离屏 textarea + document.execCommand('copy') 同步复制。
 * 两条路径都失败才 reject,由调用方决定提示文案。
 */
export async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // 权限被拒/文档失焦等:继续走 execCommand 兜底
    }
  }
  legacyCopy(text)
}

/** execCommand 回退:离屏只读 textarea 承载文本,选中后触发同步复制 */
function legacyCopy(text: string): void {
  const textarea = document.createElement('textarea')
  textarea.value = text
  // 只读 + 视口外固定定位:避免唤起移动端软键盘、触发页面滚动与视觉闪烁
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '-9999px'
  // 挂载点选当前焦点元素(触发复制的按钮)而非 body:
  // n-modal/n-drawer 默认 trapFocus(vueuc FocusTrap 在 document 捕获阶段监听 focus,
  // 焦点目标移出弹窗容器即被拉回),textarea 挂 body 会因 select() 获得焦点触发拦截,
  // 选区被破坏导致 execCommand 复制失败;挂在弹窗内的按钮下则焦点转移合法,选区完整。
  // 无焦点元素(非用户手势调用)时回退 body。
  const prevActive = document.activeElement
  const host =
    prevActive instanceof HTMLElement && prevActive !== document.body ? prevActive : document.body
  host.appendChild(textarea)
  // 保存当前选区,复制后还原(不破坏用户已有选择)
  const selection = document.getSelection()
  const selectedRange = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null
  textarea.select()
  // setSelectionRange 兼容 iOS Safari 选区
  textarea.setSelectionRange(0, text.length)
  const ok = document.execCommand('copy')
  textarea.remove()
  if (selectedRange && selection) {
    selection.removeAllRanges()
    selection.addRange(selectedRange)
  }
  // 还原触发元素焦点(按钮 :focus 态/键盘位置);preventScroll 避免视口跳动
  if (prevActive instanceof HTMLElement && prevActive.isConnected) {
    prevActive.focus({ preventScroll: true })
  }
  if (!ok) throw new Error('execCommand copy failed')
}
