"""从环境变量和 .env 文件加载的应用配置。

所有配置遵循 LLMPOD_ 前缀约定。
Node 端只需要 node_* 配置；PG/Redis/CH 的默认值不会产生影响，
因为 Node 不会导入数据库层。
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from llmpod.config.enums import AffinityFailoverPolicy


class Settings(BaseSettings):
    # ===== PostgreSQL（仅 manager 端）=====
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "llmpod"
    pg_password: str = ""
    pg_database: str = "llmpod"
    pg_pool_size: int = 10
    pg_max_overflow: int = 20

    # ===== Redis（manager 端）=====
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0
    redis_max_connections: int = 50

    # ===== 管理端（manager + webserver）=====
    webserver_host: str = "0.0.0.0"
    webserver_port: int = 8080  # 对外端口：dev=监听口；容器部署映射两端同值并回注 env（见 docker-compose.yml 头注释）
    manager_ws_path: str = "/ws/node"

    # ===== API 网关（proxyserver，v2）=====
    proxy_host: str = "0.0.0.0"  # 监听绑定地址，只管 bind、不进对客 URL
    proxy_port: int = 8090  # 对外端口：dev=监听口；容器内外同值。proxy-endpoint 未配宣告地址时拼它
    # API 网关对外宣告地址（external_url 风格完整地址，如 https://api.xxx.com）：固定 IP/域名或
    # Nginx 统一接入时配置，优先于「请求 Host + proxy_port」推导；空=按请求推导
    proxy_advertise_url: str = ""
    # 网关内部地址（管理端调网关用，如在线调试转发）：容器部署下两者分容器，回环够不到
    # 对方，须由 compose 指到服务名（http://proxyserver:<proxy_port>）。空 = 127.0.0.1 回环，
    # dev 两进程同宿主直连即此。
    proxy_internal_url: str = ""
    # 配置热更新轮询周期（秒）：周期重建路由快照原子替换。3s = 管理端改动 ~3s 生效，
    # 每次重建 = 3 张配置表全量 SELECT + 1 次 settings 点查（单机规模，代价可忽略）
    poll_interval_seconds: int = 3
    # 悬空冻结回收（proxyserver 崩溃遗留的 frozen hold 自动释放；详见 hold_reaper.py）
    hold_reap_interval_seconds: int = 300  # 回收扫描周期（默认 5min）
    hold_reap_grace_seconds: int = 3600  # 过期后再留的宽限（默认 1h，垫长流在途窗口）
    # Redis 熔断降级（API 网关热路径；详见 redis_guard.py）
    redis_breaker_failures: int = 5  # 连续失败多少次进入 open（降级）
    redis_breaker_recovery_s: int = 30  # open 后多少秒转半开试探
    redis_fail_mode: str = "open"  # open=限流在降级窗口放行（鉴权始终回源 PG）；closed=维持硬失败 500
    # 推理请求体大小上限（字节）：读 body 前先按 Content-Length 拒绝、流式读取时
    # 累计超限中断。防匿名大 body 打满内存（鉴权在读 body 之后才发生）。
    proxy_max_body_bytes: int = 10485760  # 10 MB
    # 调用日志 body 块目录：API 网关写、管理端读，compose 同卷
    body_dir: str = "data/request_logs"
    # body 块 GC 轮询周期（秒）：清超保留期且无计量行引用的块
    body_sweep_interval_seconds: int = 3600
    # body 块保留期（秒）：新写的块即使无引用也不会被提前清（防竞态误删）
    body_retention_seconds: int = 86400
    # 冻结租约时长（秒）：hold 时写入 balance_hold.expires_at，hold_reaper 据此判定过期悬空冻结
    fixed_lease_ttl_seconds: int = 7200
    # 管理员引导凭据：仅首次建库（平台尚无超管）时生效——有值用值，空则随机生成并落
    # data/admin_credentials；建库后本配置失效，改密/改名走管理后台。
    admin_user: str = ""
    admin_password: str = ""

    # ===== API 网关 → 上游（推理引擎）调用超时与端点熔断 =====
    # 引擎故障时请求不能无限挂起：分段超时 + 连续失败熔断。四段与 httpx.Timeout 一一对应。
    upstream_connect_timeout: float = 10.0  # TCP 连接（引擎不可达/黑洞 → 快速失败）
    upstream_read_timeout: float = 300.0  # 两次读之间空闲上限（流式分块间隔；非流式即整体）
    upstream_write_timeout: float = 30.0
    upstream_pool_timeout: float = 10.0
    # 首字节墙钟上限（流式）：forward() 在首个 SSE 事件返回，故等待它=等首字节。引擎「活着但
    # 不回包」（GPU 卡死/死锁）时按此收敛，而不是等到 read 空闲上限。健康引擎首字节由 prefill
    # 决定（秒级），60s 足够宽松；长思考模型的**首字之后**由 read_timeout 兜。
    upstream_ttfb_timeout: float = 60.0
    # 非流式整体墙钟上限：非流式响应只在生成完毕才回包，首字节与整体不可区分，故只能整体限时
    # （超过即按超时失败，不再等引擎）。按最慢的合理生成留足，遇到长文生成请上调。
    upstream_nonstream_timeout: float = 300.0
    # 端点熔断：连续 N 次上游失败进入冷却窗口（连接/超时属端点级故障，单候选也熔断；
    # HTTP 4xx/5xx 可能是请求级错误，仅多候选时才冷却）
    upstream_failure_threshold: int = 2
    upstream_cooldown_seconds: int = 60

    # ===== 管理端 → 来源端点连通性探测（接入/编辑前置闸门 + /admin/endpoints/test）=====
    # 外部端点登记时真发一次最小 chat 请求验证 Key / 地址 / 模型名 / 路径；不通即 422 不落库。
    # 关掉只关准入闸门，不影响 /admin/endpoints/test（运维手动诊断不该被连坐）——留作
    # 「容器无外网出口，所有外部端点都登记不进来」这类环境故障的兜底开关。
    endpoint_probe_enabled: bool = True
    # 单次探测的墙钟上限（秒）：整次探测（建连 + 发请求 + 读响应）累计到此即判超时。
    # 同一常量也作 httpx 的分段超时（快速失败），但**上限由墙钟兑现**——分段超时管的是
    # 「两次读之间」，滴答式上游能逐段续命把一次人工操作拖成分钟级。探测是同步等结论的
    # 交互动作，按总时长收敛才符合直觉（长上下文/冷启动模型请上调）。
    endpoint_probe_timeout: float = 15.0

    # ===== 会话亲和（前缀 → 端点绑定，提升上游 KV / prompt 缓存命中）=====
    # 把同一会话的连续请求路由回同一上游：引擎侧前缀缓存（vLLM/SGLang 自动前缀缓存、
    # Anthropic prompt cache）只存在于服务过该前缀的实例上，逐请求随机会让它全 miss。
    # 亲和 key 由请求内容派生（多段前缀链式 hash），不依赖客户端会话 ID。
    affinity_enabled: bool = True
    # 绑定存活时长（秒）：会话空闲超过它即解除，避免已结束的会话长期占用绑定
    affinity_ttl_seconds: int = 300
    # 前缀取样步长（消息条数）：每 N 条消息取一个边界。1 = 每条都取，第 2 轮起即粘住；
    # 调大省条目，代价是要多等一轮才生效、且匹配粒度更粗（更容易撞车）
    affinity_stride: int = 1
    # 绑定条数上限：达到后只在写入路径按 TTL 摊销扫过期腾位，腾不出就跳过本次写入
    # （旧绑定原样保留，不是整表清空——有界优先于命中率，丢最近几条不损正确性）
    affinity_max_entries: int = 100_000
    # 亲和命中后该端点转发失败时的换端策略，取值见 config.enums.AffinityFailoverPolicy：
    # rebind = 可用性优先（换端重试，成功后改绑）｜strict = 缓存优先（不换端直接失败）
    # 类型保持 str：取值写错按默认 rebind 处理并告警，不在启动期拦（见 proxyserver 装配）
    affinity_failover_policy: str = AffinityFailoverPolicy.REBIND.value

    # ===== 指标（proxyserver 埋点 /metrics + webserver 中央 scraper）=====
    # /metrics bearer token（fail-closed：空 = 一律 401，须显式配置才开放指标端点）
    metrics_token: str = ""
    # scraper 抓取目标（逗号分隔完整 URL）：proxyserver 管道指标；引擎指标由
    # scraper 按 DB 发现直抓（Instance join Node.ip），不走此配置
    metrics_scrape_targets: str = "http://127.0.0.1:8090/metrics"
    # 旧单一抓取间隔（秒）。已废弃：仅当环境里显式配置了本项、且未配置引擎
    # 快/慢档时，作为引擎两档的取值（引擎抓取曾是它最贵的部分）。proxy 档
    # 语义已变（管道指标采样密度），不受本项影响
    metrics_scrape_interval: int = 15
    # proxyserver 管道指标抓取间隔（秒）。固定档不参与升降——它喂的
    # metric_rollup 是全部统计页面的持久数据源，采样密度需稳定
    metrics_scrape_interval_proxy: int = 5
    # 引擎指标抓取间隔（秒）·快档：实时面板被消费（路由触碰需求信号）时生效
    metrics_scrape_interval_engine_fast: int = 5
    # 引擎指标抓取间隔（秒）·慢档：无人消费时的心跳档（保持快照新鲜不判死）
    metrics_scrape_interval_engine_slow: int = 30
    # metric_rollup 保留期（天），超期桶由清理循环删除
    metrics_retention_days: int = 90

    @model_validator(mode="after")
    def _backfill_scrape_intervals(self) -> "Settings":
        """分档间隔的旧值回退与合法性收敛。

        只回退**引擎**两档：proxy 档的语义变了（旧单一间隔 ~15s 是「全目标一档」
        的妥协，proxy 快档的意义正是把管道指标采样密度提上来），把 15s 原样灌进
        proxy 档会让热层点距变粗、5s 轮询契约失效——宁可要求显式配置。
        旧值回退是整体生效：任一引擎档被显式配置就整体丢弃旧值——只补一半
        再做 slow≥fast 收敛，会把显式档静默抬到旧值上。收敛只兜「显式 slow <
        显式 fast」的写反（钳到 fast，宁可多抓不可丢心跳）。
        """
        fields = self.model_fields_set
        legacy = self.metrics_scrape_interval if "metrics_scrape_interval" in fields else None
        fast = self.metrics_scrape_interval_engine_fast
        slow = self.metrics_scrape_interval_engine_slow
        tiers_set = "metrics_scrape_interval_engine_fast" in fields or "metrics_scrape_interval_engine_slow" in fields
        if legacy is not None and not tiers_set:
            fast = slow = legacy
        fast = max(1, fast)
        slow = max(1, slow, fast)
        self.metrics_scrape_interval_engine_fast = fast
        self.metrics_scrape_interval_engine_slow = slow
        self.metrics_scrape_interval_proxy = max(1, self.metrics_scrape_interval_proxy)
        return self

    @model_validator(mode="after")
    def _clamp_affinity(self) -> "Settings":
        """会话亲和参数的合法性收敛（越界值要么让 API 网关起不来、要么静默失效）。

        stride < 1 会在构造 EndpointAffinity 时直接 ValueError（发生在装配期，8090
        起不来）；ttl / max_entries ≤ 0 让亲和静默形同关闭。全部钳到下限，与
        scrape 档位收敛同思路：配置写错宁可退化成保守行为，不崩进程。
        """
        self.affinity_stride = max(1, self.affinity_stride)
        self.affinity_ttl_seconds = max(1, self.affinity_ttl_seconds)
        self.affinity_max_entries = max(1, self.affinity_max_entries)
        # 0/负值会让 asyncio.timeout 在首个挂起点立即取消——每个探测都「超时」却从未
        # 发过网络请求，症状极像上游不可达。钳到下限 1s，配置写错退化成保守行为
        self.endpoint_probe_timeout = max(1.0, self.endpoint_probe_timeout)
        return self

    # ===== 安全 =====
    # 采用「不透明令牌 + Redis 缓存」方案：令牌为随机字符串，会话信息
    # 存于 Redis，过期由 Redis TTL（token_expire_seconds）管理。
    token_expire_seconds: int = 86400
    token_key_prefix: str = "llmpod:token:"
    # 登录暴力破解限速（Redis 计数）：用户名键与 IP 键各自计失败次数，
    # 任一超限即在窗口内拒绝登录；窗口到期自然复位
    login_fail_limit: int = 10  # 单用户名窗口内允许的连续失败次数
    login_ip_fail_limit: int = 30  # 单来源 IP 窗口内允许的失败次数（NAT 多人共用的余量）
    login_fail_window_seconds: int = 900  # 计数窗口（秒）

    # 节点引导令牌（短期 HMAC，见 apps/webserver/bootstrap_token.py）
    # 管理员登录态签发，拼在 curl URL 里供目标机器无登录态拉取引导脚本。
    bootstrap_token_ttl: int = 28800  # 默认有效期 8h（秒）
    bootstrap_token_max_ttl: int = 86400  # 上限 24h（秒）；签发时 ttl clamp 到 [60, max]

    # ===== 应用层加密（source_auth 等；dev 级，生产显式设置）=====
    # Model.source_auth_encrypted 用此 key 加解密（core/secrets.py）；空 → 首启随机
    # 生成落 secret_key_file（仅 webserver 角色做，见其 lifespan）。显式配置优先。
    secret_key: str = ""
    # 自动生成的 secret_key 落盘位置（部署唯一密钥，随数据卷持久化）
    secret_key_file: str = "data/secret_key"

    # ===== Node 节点 =====
    node_id: str = ""
    node_name: str = ""  # 上报给 Manager 的节点名称；留空使用本机 hostname
    node_id_file: str = "data/node_id"
    node_token: str = ""
    node_token_file: str = "data/node_token"
    # 首次登记专用；Manager 分配 node_id 后，续连只使用共享 node_token。
    node_bootstrap_token: str = ""
    # 本机对外的 manager 地址（管理机配置）：只填 IP 或域名，ws://<它>:<webserver_port>/ws/node
    # 与 http(s) origin 由 core.advertise 拼出。填完整 URL（wss://mgr.example.com）= 反代 + TLS，
    # scheme 原样、端口不补。空 = localhost，仅同机可用；节点在别的机器上时必须填对外可达地址。
    node_manager_ip: str = ""
    # 内网直连候选（逗号分隔的裸 IP）：装机脚本把本机全部内网地址写进来，
    # bootstrap 渲染成回连候选清单，节点机逐个探活（/health）优选内网、
    # 全不通落 node_manager_ip 兜底（见 core.advertise）。
    # 只影响引导脚本的候选清单；ws/http 端点地址仍由 node_manager_ip 拼出。
    node_manager_ip_lan: str = ""
    # 节点侧：要连的 manager 完整 WS 地址，由引导脚本注入（LLMPOD_NODE_MANAGER_URL，见
    # scripts/addnode_template.sh）。管理机不配这个，配 node_manager_ip。
    node_manager_url: str = "ws://localhost:8080/ws/node"
    # 本节点对外 advertise 的 IP（多机 rendezvous / API 转发用，别的节点靠它连本机）。
    # 引导脚本必选显式指定（参数/env/菜单），正常节点都有值；留空 = 自报自动探测值
    # （host 网络下=宿主 LAN IP），manager 按未固定节点收下并告警。
    node_advertise_ip: str = ""
    # 多机部署（nnodes>1）校验所有节点须在同一子网的掩码前缀（NCCL 需双向 L3 可达）。
    multinode_same_subnet_prefix: int = 24
    node_cluster_id: int = 0
    # 节点自动升级开关（manager 侧策略，默认关）：开启后 manager 自身升级重启、
    # 节点重连对账发现落后即自动下发 $node_upgrade 提议；关闭时只标 outdated
    # 不下发，升级走 admin 手动端点（手动路径不受此开关限制）。
    node_auto_upgrade: bool = False
    node_heartbeat_interval: int = 10
    # 托管容器 liveness 扫描间隔（秒）：Node 周期 list+inspect 托管容器，崩溃/被删上报 CRASHED
    node_container_scan_interval: int = 3
    node_reconnect_fast_interval: int = 5
    node_reconnect_slow_interval: int = 30
    node_reconnect_fast_window: int = 60
    node_heartbeat_timeout: int = 60
    # Node 本地任务持久化（aiosqlite；重启恢复任务上下文，payload 不含 secrets）
    node_task_db: str = "data/node_tasks.db"

    # ===== 部署执行器（Node 侧）=====
    # 按 env LLMPOD_DEPLOYER_TYPE 选 Node 内的 Deployer 后端：
    #   docker（默认）→ DockerDeployer；k8s（将来）→ K8sDeployer（本次未实现）
    deployer_type: str = "docker"

    # ===== GPU 缓冲区（仅 manager 端）=====
    gpu_flush_interval: int = 10
    gpu_vram_change_threshold: float = 1.0  # GB 单位
    gpu_util_change_threshold: float = 0.1  # 10% 变化幅度
    gpu_stale_timeout: int = 120

    # ===== 模型分发（V1：HF 自动下载 + local）=====
    # upload/ 子目录为 POST /admin/models/upload 通用上传端点的落地路径（manager 本地）。
    # 所有 Node 必须配同一 model_store_dir，canonical path = {model_store_dir}/{name-slug}-{recipe-digest}，
    # 保多机 TP 路径一致（命名细节见 deployment/placement.py）。
    # 必须是**绝对路径**：该路径由 manager 算好下发 node，且 docker 卷挂载要求绝对（相对会 invalid mount path 崩）。
    model_store_dir: str = "/data/models"
    # 模型宿主侧真实目录（Node 侧卷改写用）。默认空 = 与 model_store_dir 同路径
    # （引导脚本同路径 bind，恒等改写）。仅当宿主大盘挂在别处、引导参数 model_host_dir
    # 指定了不同路径时才非空：Node 创建推理容器（兄弟容器）时按它把卷 host 侧前缀
    # 改写为宿主真实路径——容器内 canonical 路径不变，下载与推理依然对齐。
    model_host_store_dir: str = ""
    download_min_free_gb: float = 10.0  # Node 下载前磁盘剩余空间下限（GB，不足即 INSUFFICIENT_DISK）
    pull_exhaust_cooldown_s: int = 3600  # PULL 连续失败超上限后的冷却（秒），过后自动重开一轮重试

    # ===== 模型分发 V1.1（local 源：Manager 文件服务下发）=====
    # TTL 取 24h：令牌在每次 attempt 下发时重铸，任务执行受 12h 看门狗上限约束，
    # 24h 覆盖整个任务窗口（含下发排队），单窗口内不存在过期问题。
    download_token_ttl: int = 86400  # local 源下载令牌（HMAC）默认 24 小时
    download_token_max_ttl: int = 86400  # 签发时 clamp 上限（与任务看门狗上限同量级）
    download_chunk_size: int = 1048576  # 1 MiB（_manager 下载器分片读写）
    download_manifest_cache_ttl: int = 60  # manifest 进程内缓存秒数

    # ===== 切片上传（/admin/uploads/*：分片 + 断点续传 + 多文件）=====
    # 通用上传层与「模型」解耦，任意文件集合均可分片落地到 manager 盘。
    upload_max_bytes: int = 214748364800  # 单会话声明总大小上限（200 GB）
    upload_part_max_bytes: int = 67108864  # 单分片上限（64 MB）
    upload_session_ttl_seconds: int = 86400  # 会话 Redis TTL（24h，断点续传窗口）
    upload_session_tombstone_ttl_seconds: int = 300  # abort 取消标记 TTL（5min，拦截迟到的分片）
    upload_sweep_interval_seconds: int = 600  # 后台清理扫描周期（10min，清过期/孤儿分片目录）
    upload_finalize_stale_seconds: int = 600  # finalizing 态目录 mtime 滞留阈值（合并任务崩溃判死）

    # ===== 模型源探测（inspect：抓 config.json / 文件清单，不下载）=====
    # 国内 huggingface.co 默认不可达 → 改 https://hf-mirror.com
    model_source_hf_endpoint: str = "https://huggingface.co"
    model_source_probe_timeout: int = 10  # 秒

    # ===== 遥测（每日一次匿名遥测，仅管理端角色；false = 后台任务不启动）=====
    telemetry_enabled: bool = True

    # ===== 跨域资源共享 =====
    # 默认空 = 同源策略（前端由本服务托管，同源访问不受影响）；需要浏览器跨域
    # 直连（如独立前端域名）时显式配置来源白名单。["*"] 仍可显式配置（无 Cookie
    # 会话，实际风险有限），但不再作为默认值。
    cors_origins: list[str] = []

    # ===== 日志 =====
    log_level: str = "info"

    # ===== API 文档 =====
    docs_enabled: bool = False  # 是否启用 Swagger UI（生产环境建议关闭）

    # ===== 前端静态资源 =====
    serve_frontend: bool = True  # 是否由管理端提供前端 UI
    frontend_dist: str = ""  # 构建产物目录；空=包内 llmpod/static

    model_config = SettingsConfigDict(env_file=".env", env_prefix="LLMPOD_", extra="ignore")

    @property
    def pg_url(self) -> str:
        """SQLAlchemy 异步 PostgreSQL URL。"""
        return (
            f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @property
    def redis_url(self) -> str:
        """Redis 连接 URL。"""
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"


# 模块级单例
settings = Settings()
