<script lang="ts">
import DOMPurify from 'dompurify'

// 模块级注册一次:消毒后链接统一新窗口打开(hook 在属性过滤之后执行,不会被清除)
if (typeof window !== 'undefined') {
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A' && node.getAttribute('href')) {
      node.setAttribute('target', '_blank')
      node.setAttribute('rel', 'noopener noreferrer')
    }
  })
}
</script>

<script setup lang="ts">
import { Marked } from 'marked'
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** Markdown 源文本。 */
    source: string
    /** 流式渲染:自动补全未闭合的代码块围栏,避免渲染跳变。 */
    streaming?: boolean
  }>(),
  { streaming: false },
)

// 组件库内使用独立实例,避免污染全局 marked 配置
const marked = new Marked({ gfm: true, breaks: true })

/** 统计行首 ``` / ~~~ 围栏数量,奇数表示代码块未闭合。 */
function completeUnclosedFence(text: string): string {
  let fenceCount = 0
  for (const line of text.split('\n')) {
    if (/^\s*(```|~~~)/.test(line)) fenceCount += 1
  }
  return fenceCount % 2 === 1 ? `${text}\n\`\`\`` : text
}

const html = computed(() => {
  if (!props.source) return ''
  const text = props.streaming ? completeUnclosedFence(props.source) : props.source
  const rawHtml = marked.parse(text, { async: false })
  return DOMPurify.sanitize(rawHtml)
})
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html:内容已经过 DOMPurify 消毒 -->
  <div class="md-view" v-html="html" />
</template>

<style scoped lang="scss">
// 正文由 v-html 注入,后代节点无 scoped 属性,作用于内容的选择器必须包 :deep() 才会生效
.md-view {
  font-size: 1.4rem;
  line-height: 1.7;
  color: var(--c-default);
  word-break: break-word;

  > :deep(:first-child) {
    margin-top: 0;
  }

  > :deep(:last-child) {
    margin-bottom: 0;
  }

  :deep(p) {
    margin: 0 0 1em;
  }

  :deep(h1),
  :deep(h2),
  :deep(h3),
  :deep(h4),
  :deep(h5),
  :deep(h6) {
    margin: 1.2em 0 0.6em;
    font-weight: 600;
    line-height: 1.4;
    color: var(--c-strong);

    &:first-child {
      margin-top: 0;
    }
  }

  :deep(h1) {
    font-size: 1.8rem;
  }

  :deep(h2) {
    font-size: 1.7rem;
  }

  :deep(h3) {
    font-size: 1.6rem;
  }

  :deep(h4),
  :deep(h5),
  :deep(h6) {
    font-size: 1.5rem;
  }

  :deep(ul),
  :deep(ol) {
    padding-left: 1.8em;
    margin: 0 0 1em;
  }

  :deep(li) {
    margin: 0.3em 0;

    > p {
      margin-bottom: 0.3em;
    }
  }

  :deep(a) {
    color: rgb(var(--n-primary-color));
    text-decoration: none;

    &:hover {
      text-decoration: underline;
    }
  }

  :deep(blockquote) {
    padding: 0.4em 1em;
    margin: 0 0 1em;
    color: var(--c-secondary);
    background: var(--c-fill-2);
    border-left: 0.3rem solid var(--c-line-strong);
    border-radius: 0.4rem;

    > p {
      margin-bottom: 0;
    }
  }

  :deep(code) {
    padding: 0.15em 0.5em;
    font-size: 1.3rem;
    font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace;
    background: var(--c-fill-4);
    border-radius: 0.4rem;
  }

  :deep(pre) {
    padding: 1.2rem;
    margin: 0 0 1em;
    overflow-x: auto;
    background: var(--c-fill-4);
    border-radius: 0.8rem;

    code {
      padding: 0;
      font-size: 1.3rem;
      line-height: 1.6;
      background: transparent;
    }
  }

  :deep(table) {
    display: block;
    max-width: 100%;
    margin: 0 0 1em;
    overflow-x: auto;
    border-collapse: collapse;
  }

  :deep(th),
  :deep(td) {
    padding: 0.5em 0.8em;
    border: 1px solid var(--c-line-strong);
  }

  :deep(th) {
    font-weight: 600;
    color: var(--c-strong);
    background: var(--c-fill-2);
  }

  :deep(img) {
    max-width: 100%;
    border-radius: 0.4rem;
  }

  :deep(hr) {
    margin: 1.2em 0;
    border: none;
    border-top: 1px solid var(--c-line-strong);
  }
}
</style>
