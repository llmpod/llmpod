import { describe, expect, it } from 'vitest'
import {
  commandPlaceholderIssues,
  entryPrefix,
  hasShellSyntax,
  isCustomEntry,
  tokenize,
  writeFlagGroup,
} from '@/views/deploy/components/deploy-list/create-wizard/commandTemplate'

const VLLM_COMMAND =
  'vllm serve --model {model_path} --served-model-name {served_model_name} --host 0.0.0.0 --port {port} --tensor-parallel-size {tp}'

describe('tokenize 引号感知拆分', () => {
  it('按空白拆分并去掉成对引号', () => {
    expect(tokenize('vllm serve --a 1')).toEqual(['vllm', 'serve', '--a', '1'])
    expect(tokenize('--name "my model" x')).toEqual(['--name', 'my model', 'x'])
    expect(tokenize("--name 'my model'")).toEqual(['--name', 'my model'])
  })

  it('连续空白与首尾空白不产生空 token', () => {
    expect(tokenize('  vllm   serve  ')).toEqual(['vllm', 'serve'])
    expect(tokenize('')).toEqual([])
  })

  it('引号不配对返回 null(调用方据此禁用参数区解析)', () => {
    expect(tokenize('--name "unclosed')).toBeNull()
  })
})

describe('hasShellSyntax', () => {
  it('管道/重定向/与运算/命令替换命中', () => {
    expect(hasShellSyntax('vllm serve | tee log')).toBe(true)
    expect(hasShellSyntax('vllm serve > out.log')).toBe(true)
    expect(hasShellSyntax('source env.sh && exec vllm serve')).toBe(true)
    expect(hasShellSyntax('vllm serve --model $(pwd)')).toBe(true)
  })

  it('${SHELL_VAR} 变量展开不算 shell 运算符(留给 shell 展开)', () => {
    expect(hasShellSyntax('vllm serve --api-key ${API_KEY}')).toBe(false)
    expect(hasShellSyntax(VLLM_COMMAND)).toBe(false)
  })
})

describe('writeFlagGroup 按 flag 名写入', () => {
  it('已有 `--flag value` 就替换值并保持位置', () => {
    const next = writeFlagGroup(VLLM_COMMAND, ['--port', '9000'], ['--port'])
    expect(next).toContain('--port 9000')
    expect(next).not.toContain('{port}')
    // 位置保持:仍在 --host 之后
    expect(next.indexOf('--port 9000')).toBeGreaterThan(next.indexOf('--host'))
  })

  it('没有该 flag 就追加到末尾', () => {
    const next = writeFlagGroup(VLLM_COMMAND, ['--gpu-memory-utilization', '0.9'], [
      '--gpu-memory-utilization',
    ])
    expect(next.endsWith('--gpu-memory-utilization 0.9')).toBe(true)
  })

  it('`--flag=value` 形式同样被识别并改写成 `--flag value`', () => {
    const next = writeFlagGroup('vllm serve --port=8000', ['--port', '9000'], ['--port'])
    expect(next).toBe('vllm serve --port 9000')
  })

  it('同一 flag 多次出现只保留一个(以刚写入的值为准)', () => {
    const next = writeFlagGroup(
      'vllm serve --port 8000 --host 0.0.0.0 --port 8001',
      ['--port', '9000'],
      ['--port'],
    )
    expect(next.match(/--port/g)).toHaveLength(1)
    expect(next).toBe('vllm serve --port 9000 --host 0.0.0.0')
  })

  it('成对/布尔参数按整组替换(llama.cpp cache-type 与 flash-attn)', () => {
    const command = 'llama-server --model {model_path} --port {port}'
    const kv = ['--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0', '--flash-attn']
    const next = writeFlagGroup(command, kv, ['--cache-type-k', '--cache-type-v', '--flash-attn'])
    expect(next).toBe(
      'llama-server --model {model_path} --port {port} --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn',
    )
    // 换档:整组替换后旧值不残留
    const switched = writeFlagGroup(next, ['--cache-type-k', 'q4_0', '--cache-type-v', 'q4_0'], [
      '--cache-type-k',
      '--cache-type-v',
      '--flash-attn',
    ])
    expect(switched).not.toContain('q8_0')
    expect(switched).toContain('--cache-type-k q4_0 --cache-type-v q4_0')
  })

  it('group 为空 = 删除该组(如删掉一条参数行)', () => {
    const command = 'llama-server --model {model_path} --cache-type-k q8_0 --cache-type-v q8_0'
    expect(writeFlagGroup(command, [], ['--cache-type-k', '--cache-type-v'])).toBe(
      'llama-server --model {model_path}',
    )
  })

  it('含平台占位符的写入被拒绝(占位符只能留在用户命令里由平台回填)', () => {
    expect(writeFlagGroup(VLLM_COMMAND, ['--model', '{model_path}'], ['--model'])).toBe(VLLM_COMMAND)
  })

  it('引号不配对的命令原样返回(不做解析与写入)', () => {
    const broken = 'vllm serve --name "unclosed'
    expect(writeFlagGroup(broken, ['--port', '9000'], ['--port'])).toBe(broken)
  })

  it('编辑往返恢复引号:含空白的 JSON 值不再被拆碎(--compilation-config 事故回归)', () => {
    const command = `vllm serve --model {model_path} --port {port} --compilation-config '{"cudagraph_mode": "FULL_DECODE_ONLY"}'`
    // 第一轮改别的参数:JSON 值必须以带引号形态保留
    const once = writeFlagGroup(command, ['--gpu-memory-utilization', '0.9'], [
      '--gpu-memory-utilization',
    ])
    expect(once).toContain(`--compilation-config '{"cudagraph_mode": "FULL_DECODE_ONLY"}'`)
    // 第二轮再改:仍完整(此前两轮往返后变成 {cudagraph_mode: FULL_DECODE_ONLY} 致引擎拒启)
    const twice = writeFlagGroup(once, ['--max-num-seqs', '64'], ['--max-num-seqs'])
    expect(tokenize(twice)).toContain('{"cudagraph_mode": "FULL_DECODE_ONLY"}')
  })

  it('值内含单引号按 POSIX 转义,往返解析回原值', () => {
    const command = `vllm serve --port {port}`
    const once = writeFlagGroup(command, ['--name', "it's a model"], ['--name'])
    expect(once).toContain(`--name 'it'\\''s a model'`)
    const twice = writeFlagGroup(once, ['--max-num-seqs', '8'], ['--max-num-seqs'])
    expect(tokenize(twice)).toContain("it's a model")
  })

  it('无空白的普通值不加引号(命令文本保持可读)', () => {
    const next = writeFlagGroup(VLLM_COMMAND, ['--dtype', 'bfloat16'], ['--dtype'])
    expect(next).toContain('--dtype bfloat16')
  })
})

describe('isCustomEntry 默认入口前缀判定', () => {
  it('保留引擎默认入口 = 非自定义', () => {
    expect(isCustomEntry(VLLM_COMMAND, 'vllm')).toBe(false)
    expect(isCustomEntry('python3 -m sglang.launch_server --port {port}', 'sglang')).toBe(false)
    expect(isCustomEntry('llama-server --port {port}', 'llamacpp')).toBe(false)
  })

  it('改成别的程序 = 自定义命令模式(逐行参数区禁用)', () => {
    expect(isCustomEntry('myvllm serve --port {port}', 'vllm', VLLM_COMMAND)).toBe(true)
    expect(isCustomEntry('bash -lc "vllm serve"', 'vllm', VLLM_COMMAND)).toBe(true)
  })

  it('与平台生成命令的入口前缀一致也视为默认入口(镜像自定义 entrypoint)', () => {
    expect(isCustomEntry('/opt/run vllm serve --port {port}', 'vllm', '/opt/run vllm serve --x 1')).toBe(
      false,
    )
    expect(entryPrefix('/opt/run vllm serve --port {port}')).toBe('/opt/run vllm serve')
  })
})

describe('commandPlaceholderIssues 占位符阻断', () => {
  it('缺 {port} 阻断', () => {
    expect(commandPlaceholderIssues('vllm serve --model {model_path}', 1, 'vllm')).toHaveLength(1)
    expect(commandPlaceholderIssues(VLLM_COMMAND, 1, 'vllm')).toEqual([])
  })

  it('多机 vLLM 缺 {headless} 阻断,单机不要求', () => {
    expect(commandPlaceholderIssues(VLLM_COMMAND, 2, 'vllm')).toHaveLength(1)
    expect(
      commandPlaceholderIssues(`${VLLM_COMMAND} {headless}`, 2, 'vllm'),
    ).toEqual([])
    // 非 vllm 引擎不要求 headless
    expect(commandPlaceholderIssues(VLLM_COMMAND, 2, 'sglang')).toEqual([])
  })
})
