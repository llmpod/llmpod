# LLMPod 文档

[返回项目首页](../README.md)

## 阅读导航

| 分类 | 文档 | 内容 |
| --- | --- | --- |
| 使用 | [部署指南](usage/deployment.md) | Docker Compose 安装、配置、升级与运维 |
| 使用 | [API 接入](usage/api.md) | 接口列表、鉴权与调用示例 |
| 开发 | [后端开发指南](development/README.md) | 技术架构、环境搭建、常用命令与项目结构 |

## 目录约定

```text
docs/
├── README.md               # 文档索引
├── usage/                  # 面向使用者：部署、配置、API 接入
│   ├── deployment.md
│   └── api.md
└── development/            # 后端环境、架构、开发流程
    └── README.md
```

根目录的 `README.md` 保留项目介绍与快速开始，部署、API 与后端开发文档统一放在以上目录。

- 新增文档优先归入现有分类，并在本页添加入口。
- 文件名使用简短的小写英文，多个单词用连字符连接；分类首页使用 `README.md`。
- 文档注明命令的执行目录；页面间使用相对链接，迁移时同步修改引用。
- 过程稿和临时笔记放入已忽略的 `human-docs/`。正式文档目前仅放行上述两个分类；新增分类时同步调整根目录的 [`.gitignore`](../.gitignore)。
