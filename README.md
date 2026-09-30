# LLMPod

**私有化大模型推理基础设施**

把自建 GPU 集群变成可调用、可管理、可计量的推理 API。

[官网](https://llmpod.ai/) · [许可证：AGPL-3.0-only](https://www.gnu.org/licenses/agpl-3.0.html)

LLMPod 面向希望使用自有算力运行和提供大模型服务的团队，覆盖从计算节点管理、模型部署到 API 发布、租户管理和用量统计的完整流程。

## 功能特性

### 集群与算力管理

- 通过管理端统一管理集群、计算节点和 GPU 资源。
- 节点代理通过 WebSocket 与管理端保持连接，接收部署任务并上报节点状态和资源信息。
- 采集节点 CPU、内存、磁盘及 GPU 状态；支持 NVIDIA GPU 和 Ascend NPU 的设备探测与监控。
- 根据可用设备和模型资源需求选择部署位置，并管理模型服务实例及副本。

### 模型管理与部署

- 管理模型、版本、部署和发布状态。
- 支持从 Hugging Face、对象存储或本地模型文件导入模型；提供模型源探测与文件信息检查。
- 支持大文件分片上传和模型文件向计算节点分发。
- 提供推理镜像目录，根据推理引擎、硬件架构及版本选择部署镜像。
- 内置 vLLM、SGLang 和 llama.cpp 引擎适配；具体可用的引擎、模型、设备和版本组合取决于对应镜像及运行环境。
- 支持部署生命周期管理，并查看实例状态和部署日志。

### 统一推理网关

ProxyServer 为本地部署和登记的外部模型端点提供统一 API 入口，可使用模型别名发布服务并按路由转发请求。

| 协议 / 能力 | API 路径 |
| --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` |
| OpenAI Embeddings | `POST /v1/embeddings` |
| OpenAI 模型列表 | `GET /v1/models` |
| Anthropic Messages | `POST /v1/messages` |
| Anthropic Token 计数 | `POST /v1/messages/count_tokens` |
| Rerank | `POST /v1/rerank` |

支持对话请求的流式响应、API Key 鉴权、模型访问控制、请求限额和调用记录。客户端可通过网关调用已发布的模型，无需直接连接各个推理服务。

### 多租户、权限与计费

- 提供平台管理端和租户工作台，按租户隔离用户、权限、API Key 和调用数据。
- 支持角色与权限管理，以及租户可用模型的授权。
- 可为 API Key 配置模型访问范围和调用配额。
- 记录请求与 Token 用量，支持模型定价、租户余额、消费明细和用量统计。

### 监控与运维

- 查看集群、节点、GPU、模型部署和推理引擎状态。
- 提供调用量、延迟、首 Token 延迟、错误率及 Token 用量等统计视图。
- 支持请求日志、部署日志和审计日志，方便追踪调用与管理操作。
- 提供 Prometheus 格式的数据面指标接口。

## 架构概览

LLMPod 将管理控制面与推理数据面分开：管理端负责集群、模型和部署编排；数据面接收并路由推理请求；节点代理负责在计算节点上管理推理服务。

- **控制面**：WebServer / Manager 提供管理界面和 API，负责集群、模型、租户与部署编排。
- **节点侧**：Node Agent 通过 WebSocket 连接管理端，接收任务、管理推理服务并上报节点状态。
- **推理数据面**：ProxyServer 接收租户和业务应用的请求，将流量路由到集群内的推理服务或已登记的外部端点。
- **存储与缓存**：WebServer / Manager 和 ProxyServer 使用 PostgreSQL 与 Redis 保存业务数据及运行状态。

## 安装

在目标服务器上运行 LLMPod 官方安装脚本：

```bash
curl -fsSL https://llmpod.ai/src/download/install.sh -o install.sh && sudo bash install.sh
```

安装和初始化过程中请按脚本提示操作。更多产品信息请访问[官网](https://llmpod.ai/)。

## 技术组成

| 部分 | 技术 |
| --- | --- |
| 后端服务与 API | Python 3.12+、FastAPI |
| 数据存储与缓存 | PostgreSQL、Redis |
| 管理端与租户端 | Vue、TypeScript |
| 服务与节点部署 | Docker、Docker Compose |

## 许可证

本项目采用 [GNU Affero General Public License v3.0（AGPL-3.0-only）](https://www.gnu.org/licenses/agpl-3.0.html)。第三方依赖、推理引擎、模型文件及其他外部组件可能适用各自的许可条款。
