/**
 * 启动命令模板工具(纯函数)。
 *
 * 交互原则:命令文本是唯一权威 —— 参数区(逐行 args / 显存占比 / KV 精度 / 常用参数 chip)
 * 只按 flag 名往命令里写,命令的改动不反写参数区。所以本模块只提供「读命令」与
 * 「写命令」两类无副作用纯函数,状态由 useStore 管。
 */

/** 平台托管占位符:由平台在部署期逐实例回填,参数区一律不写入(只能留在用户命令里) */
export const PLATFORM_PLACEHOLDERS = [
  '{model_path}',
  '{served_model_name}',
  '{port}',
  '{tp}',
  '{pp}',
  '{nnodes}',
  '{node_rank}',
  '{master_addr}',
  '{master_port}',
  '{headless}',
] as const

/** 就绪探活用它分配服务端口:命令里必须保留,否则后端拒绝创建(422) */
export const PORT_PLACEHOLDER = '{port}'
/** vLLM 多机 worker 分片标记:多机命令里必须保留,否则后端拒绝创建(422) */
export const HEADLESS_PLACEHOLDER = '{headless}'

/** shell 运算符(管道/重定向/命令替换):命中即整条命令交给 bash -c,参数区不做解析与写入 */
const SHELL_OPERATOR = /[|>]|&&|\$\(/

/** 命令是否含 shell 运算符(命中时镜像入口不再沿用,也不再做参数区解析) */
export function hasShellSyntax(command: string): boolean {
  return SHELL_OPERATOR.test(command)
}

/**
 * 按空白拆分并尊重单/双引号(引号内保留字面值,双引号内反斜杠转义,单引号外反斜杠
 * 转义下一字符);返回去引号后的纯 token。
 * 引号不配对返回 null(调用方据此禁用解析)。
 */
export function tokenize(command: string): string[] | null {
  const tokens: string[] = []
  let current = ''
  let started = false
  let quote: '"' | "'" | null = null
  for (let index = 0; index < command.length; index++) {
    const char = command[index]
    if (quote) {
      if (char === '\\' && quote === '"' && index + 1 < command.length) {
        current += command[++index]
        continue
      }
      if (char === quote) {
        quote = null
        continue
      }
      current += char
      continue
    }
    // 引号外的反斜杠转义下一字符:引号恢复写出的 '\'' 等序列才能被再次解析回原值
    if (char === '\\' && index + 1 < command.length) {
      current += command[++index]
      continue
    }
    if (char === '"' || char === "'") {
      quote = char
      started = true
      continue
    }
    if (/\s/.test(char)) {
      if (started) {
        tokens.push(current)
        current = ''
        started = false
      }
      continue
    }
    current += char
    started = true
  }
  if (quote) return null
  if (started) tokens.push(current)
  return tokens
}

/** flag token 判定:`--flag` / `-f` 算 flag,单 `-`、`--` 与负数值(`-0.5`)不算 */
function isFlagToken(token: string): boolean {
  if (token.length < 2 || !token.startsWith('-')) return false
  if (token === '--') return false
  if (token[1] === '-') return true
  return /^-[A-Za-z]/.test(token)
}

/** flag 的名称部分(`--k=v` → `--k`) */
export function flagName(token: string): string {
  const separator = token.indexOf('=')
  return separator > 0 ? token.slice(0, separator) : token
}

/** 命令入口前缀(首个 flag 之前的 token 拼接;用于判定是否改掉了默认入口) */
export function entryPrefix(command: string): string {
  const tokens = tokenize(command) ?? []
  const head: string[] = []
  for (const token of tokens) {
    if (isFlagToken(token)) break
    head.push(token)
  }
  return head.join(' ')
}

/** 引擎内置默认入口(catalog 镜像的 entrypoint 等价物;保留它后端仍沿用镜像入口) */
const ENGINE_ENTRIES: Record<string, RegExp> = {
  vllm: /^(vllm\s+serve|python3?\s+-m\s+vllm\.)/,
  sglang: /^(python3?\s+-m\s+sglang\.launch_server|sglang\s+serve)/,
  llamacpp: /^\/?llama-server\b/,
}

/**
 * 命令入口是否已改成别的程序(如 `myvllm xxxx`)。
 * 改了 → 后端把整条命令当容器入口(镜像自带 entrypoint 与 CANN 环境初始化都不再执行),
 * 参数区禁用:往自定义程序后面追加 vLLM 风格 flag 可能被拒。
 * 判定:命中引擎内置入口,或与平台生成命令的入口前缀一致,即视为保留默认入口。
 */
export function isCustomEntry(
  command: string,
  engine: string | null | undefined,
  platformCommand = '',
): boolean {
  const prefix = entryPrefix(command)
  if (!prefix) return false
  const entry = engine ? ENGINE_ENTRIES[engine] : undefined
  if (entry?.test(command.trim())) return false
  const platformPrefix = entryPrefix(platformCommand)
  return !platformPrefix || prefix !== platformPrefix
}

/** 命令缺必需占位符等阻断项(非空即必须拦住提交,否则后端 422) */
export function commandPlaceholderIssues(
  command: string,
  nnodes: number | null | undefined,
  engine: string | null | undefined,
): string[] {
  const issues: string[] = []
  if (!command.includes(PORT_PLACEHOLDER)) {
    issues.push(
      `启动命令缺少 ${PORT_PLACEHOLDER} 占位符:平台按节点分配服务端口并据此探活,提交会被拒绝`,
    )
  }
  if ((nnodes ?? 1) > 1 && engine === 'vllm' && !command.includes(HEADLESS_PLACEHOLDER)) {
    issues.push(
      `多机 vLLM 命令缺少 ${HEADLESS_PLACEHOLDER} 占位符:worker 分片靠它注入 --headless,提交会被拒绝`,
    )
  }
  return issues
}

/** 单个 token 是否含平台托管占位符 */
function tokenHasPlaceholder(token: string): boolean {
  return PLATFORM_PLACEHOLDERS.some((placeholder) => token.includes(placeholder))
}

/** token 组里是否含平台托管占位符(参数区写入守卫:占位符只能留在用户命令里) */
function hasPlaceholder(tokens: string[]): boolean {
  return tokens.some(tokenHasPlaceholder)
}

/** token 不加引号就保不住完整性:值内含空白/引号/反斜杠,或以注释符 # 开头(拼回后会被当注释吃掉) */
function tokenNeedsQuote(token: string): boolean {
  return /[\s"'\\]/.test(token) || token.startsWith('#')
}

/**
 * token 拼回命令文本时的引号规则:平台占位符原样;必须加引号的值整体包成单引号
 * (值内单引号按 POSIX 写成 '\'');其余 token(含 $VAR 等依赖 shell 展开的写法)原样。
 * 不恢复引号的话,含空白的值(如 --compilation-config 的 JSON 配置)会在下一轮编辑
 * 或部署期解析时被拆碎,引擎拿到半截 JSON 直接拒启。
 */
function quoteToken(token: string): string {
  if (tokenHasPlaceholder(token) || !tokenNeedsQuote(token)) return token
  return `'${token.replaceAll("'", "'\\''")}'`
}

/**
 * 按 flag 整组写入命令:
 * 1) 先移除 relatedFlags 的全部既有出现(`--k v` / `--k=v`,同 flag 多次只保留一个);
 * 2) 再把 group 插回首个被移除出现的位置,无命中则追加到末尾(保持原有顺序);
 * 3) group 为空 = 纯删除(如删掉一条参数行);
 * 4) 重拼时对需要引号的 token 恢复引号(quoteToken),值不在编辑往返中丢引号被拆碎。
 * 命令不可解析(引号不配对)或 group 含平台占位符时原样返回。
 */
export function writeFlagGroup(command: string, group: string[], relatedFlags: string[]): string {
  if (hasPlaceholder(group)) return command
  const tokens = tokenize(command)
  if (!tokens) return command
  const related = new Set(relatedFlags)
  const out: string[] = []
  let inserted = false
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index]
    if (isFlagToken(token) && related.has(flagName(token))) {
      // `--k=v` 的值内联在 token 里;`--k v` 需连值一起吃掉
      if (token.indexOf('=') < 0) {
        while (index + 1 < tokens.length && !isFlagToken(tokens[index + 1])) index++
      }
      if (!inserted) {
        out.push(...group)
        inserted = true
      }
      continue
    }
    out.push(token)
  }
  if (!inserted) out.push(...group)
  return out.map(quoteToken).join(' ')
}
