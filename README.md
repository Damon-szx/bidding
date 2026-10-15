# Bidding Agent

第一阶段：对话驱动的招标来源搜索、公告读取、附件发现与下载。暂不实现知识库、评分、供应商查询和投标文件生成。

基本聊天界面位于 `http://127.0.0.1:8010/`，提供会话列表、新建会话、消息发送、后台任务进度和工具参数/结果查看。刷新后从数据库恢复所选会话；模型未配置时会显示具体错误。前端为本地静态 HTML/CSS/JavaScript，由同一个后端提供，无须额外启动 Node 服务。接口文档仍在 `/docs`。

前端浏览器验证脚本是 `tests/chat_ui.cjs` 和 `tests/batch_ui.cjs`，需要 Node 和 Playwright：`node tests/chat_ui.cjs`、`node tests/batch_ui.cjs`（服务需先启动）。可通过 `PLAYWRIGHT_MODULE` 指定已有 Playwright 安装位置，`PLAYWRIGHT_CHANNEL=chrome` 使用本机 Chrome。脚本用模拟 API 验证两轮对话、历史恢复、日志展开、范围确认、批量控制、移动布局和请求失败，不调用真实模型或修改业务数据；截图写入 `data/ui-checks/`。

## 启动

### 专业 Agent 与失败诊断

主协调、搜索、页面与文件、反思、任务规划及上下文摘要的自然语言提示统一使用
简体中文，工具与参数说明、证据读取提示也使用中文。执行说明、交接、计划说明和
决策摘要要求使用中文；工具名、JSON 字段、枚举、标识和准确原文引用保持原样。
历史模型记录按原样保留，提示更新不会翻译已经保存的模型输出。

上下文摘要按预期收益触发：可压缩历史预计至少减少 2000 个估算 Token 且占总输入
10% 时才调用摘要模型，门槛可由 `CONTEXT_MIN_COMPACTION_GAIN_TOKENS` 配置。
一次摘要后，输入需比该次摘要前再增长至少这个门槛才会再次摘要；接近硬上限时
优先从检查点重建。相同 URL、标题且完整正文一致的详情页观察只在模型工作集中
展示最新快照，旧日志仍可追溯；正文变化和不完整快照不会合并。执行修复只向模型
交接状态和结果引用，完整计划、参数、原文及执行结果仍保存在数据库。尚无核验
通过项目的语义下载任务不会向主 Agent 提供创建下载队列工具。

文件获取入口检查会记录 JavaScript 弹窗的类型和文字（不接受确认或填写输入），
保存在 `access_dialogs`；明确拒绝获取的提示还返回 `access_error` 并标记
`document_access_status=blocked`。这些字段保留在工具摘要中；没有创建下载队列时，
最终下载失败说明优先展示已观察到的网站拒绝原话，避免被通用范围提示覆盖。

搜索 Agent 在普通决策轮中初筛已观察候选并报告缺口，不额外调用反思。初筛不等于原文
核验；有待核验候选时先交给页面 Agent。搜索批次最多两个检索式，之后须整理进度。
初始额度默认 6 次，主 Agent 可申请最多三批补搜，每批默认 2 次；不重置总预算。
已满足项目数量、未处理现有线索、重复策略、时间不足或未预留 8000 Token 时不追加。
连续两轮无新增值得核验线索先评审新策略，策略评审最多三次。
子 Agent 每步保存交接检查点；输出截断只尝试一次无工具的短交接修复，失败仍返回
已有证据和部分完成状态，不重复搜索。

主协调 Agent 使用 `delegate_agent` 将有界子任务交给搜索 Agent 或页面与文件 Agent。
角色说明位于 `src/bidding/agents/*/AGENT.md`，按需加载；子 Agent 只有自己的工具白名单，
不允许继续委派。独立搜索可由任务依赖图并行调度，共享浏览器页面任务串行。
各角色共享父任务的时间、Token、搜索和工具尝试额度，不能通过委派刷新预算。

独立反思 Agent 在检索/下载任务的结果阶段检查用户条件与执行证据，返回通过、修正或需补充信息。
主 Agent 根据修正意见继续补证据；评审失败不视为通过。默认最多两轮评审，
带语义筛选的下载任务在创建队列前评审；未通过时不排队，并向执行循环反馈补证据要求。
`REFLECTION_ENABLED=false` 可关闭评审；`AGENT_MAX_DELEGATIONS` 默认 6，
`SPECIALIST_MAX_ROUNDS` 默认 5，`REFLECTION_MAX_ROUNDS` 默认 2。

开发者记录按当前请求展示执行时间线：压缩尝试、异常类型/校验字段/脱敏堆栈、
关联模型调用、格式修复、降级恢复、前后上下文大小、Agent 分工与评审反馈。
摘要输出长度以供应商 Token 用量为准，不与 UTF-8 字节估算混用。格式错误或截断只追加一次修复；
工具摘要仍失败则采用可追溯预览；历史摘要修复仍失败时立即打开本轮摘要熔断，
从持久化任务目标、步骤检查点、证据索引和最新用户请求重建上下文。重建仍超限才停止，
不会删除数据库里的原始工具结果。记录的是可审计决策摘要，不是模型私有逐字推理。

规划中的步骤输出统一使用 `{"$ref":"s1.entries.0.entry_id"}`。已知步骤的普通字符串引用、
数组下标和遗漏依赖可在本地修正，循环依赖仍拒绝；授权原话仍须由模型从当前用户请求中选取，
不会自动补造授权。模型每轮仅接收去重的计划工作集、已完成检查点和目标缺口；重复读取的相同
证据片段在工作上下文中去重，原文和完整计划仍保存在日志中。预算记录区分累计用量、剩余额度、
请求输入估算及输出预留；剩余额度不足以容纳下一次请求时提前停止，不代表实际用量恰好达到上限。

临时 `read_tool_result`、附件发现、文件入口检查和队列查询仍完整审计，但不会新增业务计划节点。
同参数成功查阅和附件发现直接复用本轮缓存，审计返回 `reused` 和原调用引用；不再消耗实际尝试次数。
已读证据进入最多 16 条、约 8000 估算 Token 的去重工作集；模型请求不重复发送较早工具交换里的同一原文。
短公告正文保留，`text_complete`、`entries_complete`、`attachments_complete` 明确覆盖范围；
并行批次允许最多四份单工具预算，避免把三个项目的短正文全部裁掉。完整原文仍可按日志 ID 定位。
初始规划的核验占位节点及依赖它们的下游节点延后，读到证据后动态补充。
`expected_count` 表示任务项目数量，`files_per_project` 可约束每个项目应选择的文件数量。
反思的 `repair_steps` 和 `remove_step_ids` 经协调器校验，仅允许有界读取、附件入口检查和原文核验；
不能授权下载、控制队列或委派。连续无进展仍至多恢复三轮。
恢复评审携带精确工具参数 Schema、已观察入口 ID 和近期错误反馈，一次只提出一个可立即执行的修复节点。
相同不可重试的失败参数直接返回原错误并要求改参；未变化的计划和空队列不再重复查询。
摘要熔断后在硬上下文限制以内保留当前反馈，只有真正接近硬限制才重建；过小的历史前缀不调用模型摘要。
任务结束生成不额外调用模型的增量交接摘要，保存目标、结果、未完成检查点、批次及证据引用；
原始对话和工具记录不会删除。主循环、专业 Agent 和反思请求都进行上下文大小检查。
国网“获取招标文件”等按钮以 `document_access` 返回，可用 `inspect_document_access` 检查一次。
只允许已观察到的文件获取入口，不提交凭据、验证码、购买或投标表单；发现登录限制时明确返回
`requires_user_action`，不能把公告下载按钮或排队状态当作完整标书下载成功。
“下载公告文件”和“下载变更公告文件”同时作为未分类附件候选，由下载过程校验实际格式；
泛指公告附件的目标不默认升级成完整标书，明确要求完整招标文件时仍严格核验文件类型。

这是同一进程内的逻辑多 Agent；不等于分布式工作集群。下载任务是否完成仍以后台文件校验
和实际进度为准，创建队列或评审通过都不代表文件已经下载。

需要 Python 3.10+ 和 uv。在项目目录运行：

```sh
uv sync --extra test
uv run python -m bidding.service start
uv run python -m bidding.service status
uv run python -m bidding.service stop
```

后台模式默认地址为 `http://127.0.0.1:8010/docs`，可用 `start --port 8011` 换端口。进程号保存在 `data/api.pid`，运行日志保存在 `data/api.log`；stop 只向该脚本启动并核验过的进程发送 SIGTERM，不会强制杀死其他服务。停止可能等待当前任务结束，可通过 status 查询。

前台调试时运行 `uv run uvicorn bidding.api:app --host 127.0.0.1 --port 8010`，在该终端按 `Ctrl+C` 停止。前台进程不由 service stop 管理。不要同时启动多个实例或多个 worker，共享数据库的启动恢复逻辑目前按单进程设计。

在 `.env` 中填写 `.env.example` 中的配置。`MODEL_BASE_URL`、`MODEL_API_KEY`、`MODEL_NAME` 指向支持 Chat Completions tool calling 的模型服务。模型名由所用服务决定。模型与搜索服务独立。

模型渠道通过策略工厂选择：`MODEL_PROVIDER=qwen` 使用 `QWEN_API_KEY`（也支持 `DASHSCOPE_API_KEY`）、`QWEN_NAME=qwen3.7-flash` 和 `QWEN_BASE_URL=https://maas.qianwenaiapi.com/compatible-mode/v1`。`QWEN_THINKING=disabled` 默认关闭思考模式，发送 `enable_thinking=false`，不会发送 DeepSeek 专用参数。主 Agent、子 Agent、反思和摘要统一使用选中的渠道，不自动跨渠道重试。

切回 DeepSeek 只需设置 `MODEL_PROVIDER=deepseek` 并重启服务，原有 `MODEL_*` 配置保留；也可用 `DEEPSEEK_API_KEY`、`DEEPSEEK_NAME`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_THINKING` 覆盖。其他兼容接口选择 `openai_compatible`。`MODEL_TIMEOUT_SECONDS` 同时约束单次请求的总时长和网络等待；临时超时仍按有限重试策略处理。API Key 只保存在本地 `.env`，不要提交或分享。

`TASK_TOKEN_BUDGET` 是整个任务多次模型调用的累计成本上限，不是单次上下文窗口。
配置 `TASK_TOKEN_BUDGET_MODE=soft` 后，它改为软预算（本地 300000）：超过后记录进度检查，
不是立即中断。有新证据时继续；主 Agent 对照只提供建议的反思结果收口。
`TASK_TOKEN_HARD_LIMIT` 是累计成本硬兜底（本地 1000000），与单次上下文窗口无关。
工具次数、执行时间、无进展恢复次数限制仍保留。到软预算的 75% 时提前尝试精简历史请求。
进度监测每四个调度轮次按需触发、最多三次；零新增观察持续三轮或连续两次停止建议且
没有业务推进时，主 Agent 停止并保留结果。打开十个新详情不等于核验零个就是死循环。
用户明确授权公告备用时，任务设置 `fallback_file_goal=announcement`，保存原话授权；
只有确认标书入口受限或没有可观察标书入口后选择公告。下载文件保留 announcement_document
类型，不冒充完整标书。默认不允许回退。
仅在 hard 模式下，下一次请求只差少量额度时，自动补充一次受限额度：最多原预算的 5%，且不超过
`TASK_TOKEN_GRACE_TOKENS`（默认 15000，设为 0 可关闭）。补充会记入开发者记录，
不会反复增加预算，也不会放宽上下文窗口、工具次数、时间或用户授权限制。
项目查询达到已核验数量后直接进入最终检查，不再额外请求主 Agent 执行过时节点。

上下文硬上限与累计任务预算是两个独立配置。核实具体模型及接口支持长上下文后，
可设置 `CONTEXT_HARD_LIMIT_TOKENS=128000`、`CONTEXT_COMPACT_THRESHOLD_TOKENS=64000`。
不能仅凭 DeepSeek 或 Qwen 品牌名假设所有型号和代理接口都支持 1M。
澄清后的文件目标不能退化成查询；已观察来源随确认传递，浏览器附件需按原详情 URL 刷新。
登录障碍仅针对具体文件入口。子 Agent 不持久化父会话快照，候选工作集有界展示，
完整搜索结果和被省略候选仍保留在原始日志中供按需读取。

每个任务分别保存 `user_request`（原文、请求 ID、SHA-256）与 `intent_contract`（已接受约束快照）。
模型请求以结构化 JSON 区分原始要求、约束、执行状态及评估；摘要仅为执行记忆，不能写回原要求或授权。
明确数字月份的“10月份以前/10月之前”按月初边界检查，不可漂移到当天；未给年份时标明当前年假设。
主调度请求默认保留最近四组完整交流（`CONTEXT_RECENT_EXCHANGES`），旧交流先用无模型投影移出上下文，
保留证据引用、最近用户要求与当前工作集，原始工具日志不删除。更多内容可按原始结果 ID 按需读取。

页面 Agent 的交接以真实核验记录和选定文件为准，不采信模型文字中的“已核验/已下载”；未登记核验的已读详情优先闭环，再打开下一候选。委派直接携带有界原文预览，工具轮次结束后由代码生成交接，不额外占用一轮模型来写总结。已核验详情在主工作集中只保留来源索引，完整原文仍可查阅。
计划引用统一为 `{"$ref":"step_id.field"}`，兼容模型常见的 `$ref:step_id.field`、`$.step_id.result.field` 写法；扫描和委派的不存在字段在执行前拒绝。仅删除过时节点可省略 `steps`，排序依赖自动解除；引用被删节点输出的下游必须同时修正，不能绕过证据。引文核验错误返回具体条件、错误引文和本页有界原文，供一次定向修正，不放宽原文一致性要求。

### 动态招标网站

`scan_tender_listing` 是共用的有界列表扫描工具，最多十页，按已接受的发布时间上界初筛。
仅明确标为发布时间的日期字段可自动排除，未知字段保留补证；其他条件由模型依据列表原文判断。
支持下一页超链接、JS 下一页和加载更多；保存条目指纹与已观察游标，详情返回后可恢复原列表位置。
无限滚动、任意页码跳转和直接接口重放尚未支持，不猜接口参数，也不将未识别下一页当作全量穷尽。
主调度在准备提前结束日期筛选任务时，最多补两批扫描；仍受工具次数和任务时间限制。
未知日期行单独列入 `unresolved_candidates`，不触发候选就绪，也不被删除；扫描继续到已核实日期候选或页面边界。
主 Agent 对多项目列表最多直接补读两次证据（`COORDINATOR_MAX_EVIDENCE_READS`），其余补证委派给 page_files。
页面 Agent 返回扫描范围、最新游标、核验记录、选定文件、入口障碍和缺口；下载队列仅由主 Agent 创建。
同会话已答复的确认链可保留原下载授权引文，但不能据此增加数量、允许公告替代或改变文件用途。
完整技能定义在 `skills/tender-acquisition/SKILL.md`；站点提取适配器与通用扫描流程分离，不为每个站点复制技能。

已接入 Python Playwright。macOS 默认使用已安装的 Google Chrome；其他系统默认使用 Playwright Chromium。可通过 `BROWSER_CHANNEL=chrome` 选择 Chrome，或配置空值并安装 Chromium：

```sh
uv run playwright install chromium
```

`fetch_tender_detail` 自动处理 `#/` 前端路由和常见空应用外壳；静态内容只有导航时，Agent 可调用 `render_tender_page` 强制渲染。返回项目列表 `entries`、详情类型、附件操作和分页入口。`navigate_tender_entry(announcement_id, entry_id)` 只能跟随该页面实际发现的入口，支持点击行、前端路由、新标签和下一页，不接受任意脚本或 CSS 选择器。国网 ECP 已适配项目编号、详情日期和资格预审公告类型；列表的创建时间单独保存在 `listed_date`，不当作已核实的发布日期。

附件可能没有普通链接。`discover_tender_files` 返回的 `browser-attachment:...` 是已观察到的下载操作标识，直接原样传给 `download_tender_file` 即可。点击下载与详情读取使用同一公开浏览器会话；普通附件直链下载按每个跳转目标获取匹配的浏览器 Cookie，Cookie 不写入模型输入或审计日志。支持浏览器生成的下载事件和 Blob 文件，并复用原有文件签名、100 MB 限额、哈希去重及来源记录。

浏览器会话只存在内存中，空闲超过 15 分钟或服务重启后需重新读取列表；最多四个会话，满额时回收空闲会话。`BROWSER_MAX_PAGES` 默认每个会话 32 个标签，范围 2–100；成功导航复用页面检查点，达到容量时仅回收没有交互文件入口的详情页，保留数据库证据、当前来源页和文件操作页。`BROWSER_TIMEOUT_SECONDS` 默认 40，范围 5–90 秒。所有 HTTP(S) 浏览器请求经过公开地址检查，拦截私网目标，禁用 Service Worker 和 WebSocket；不处理登录、验证码破解、付费或表单提交。当前仍没有 DNS 固定绑定，不能视为完整的生产级 SSRF 隔离。

页面 HTML 仅用于内存解析，不再写入文件或保存页面截图。提取后的公告正文、脱敏工具参数、网络 URL/状态及导航关系仍在数据库留痕；模型只接收精简项目条目和附件入口。下载结果包含 `local_download_url`，对应 `/files/{file_id}`，聊天中可用文件名链接下载本地已保存附件。此接口只读取文件记录且限制在文件目录内，不接受任意路径；仍只适用于可信本机环境，生产部署需授权校验。测试命令：`RUN_BROWSER_TESTS=1 BROWSER_CHANNEL=chrome uv run pytest tests/test_browser.py -q`。

搜索通过 Streamable HTTP MCP 接入：设置 `SEARCH_MCP_URL`、`SEARCH_MCP_TOOL`，必要时设置令牌及查询参数名。服务必须真正提供联网搜索；默认单字符串 `query` 参数，复杂参数服务需要扩展 `search.py`。百度的文本记录及常见结构化搜索结果会先转换为候选条目，标题和来源链接独立保留，正文做短摘要；原始响应完整留在调用日志中，候选不等于已核实公告。未知格式仍保留原始响应供模型判读。

聊天区只展示业务进度和最终结果，开发者记录保留工具参数、原始结果、模型执行消息、耗时、Token 用量及压缩记录。查询概况不强制下载附件；项目详情和下载请求分别走对应路径。未指定的“近期”默认近 30 天，由模型核对发布日期。相同工具与参数最多实际执行两次，超出后记录阻止原因并要求调整策略；同一份原始结果最多读取四次，可用 `contains` 关键词定位正文，空字段返回原始字段目录。搜索调用默认最多六次（`AGENT_MAX_SEARCH_CALLS`）；达到搜索上限或最后一轮时不提供工具，要求整理已有结果；任务超时或预算耗尽时保留已获得的候选线索。

前端使用本地固定版本 marked / DOMPurify 渲染并清洗 Markdown，来源链接只允许 HTTP(S)。当前开发者面板是本地调试视图，不是权限隔离：接口仍仅适用于受信任的本机环境，生产部署前需要加入用户认证、会话归属校验与开发者角色授权。

没有模型密钥时，可通过 `/tools/*` 直接采集给定公告 URL。没有搜索配置时搜索明确失败；不会生成虚构网址。

```sh
curl -X POST http://127.0.0.1:8010/chat -H 'Content-Type: application/json' -d '{"message":"查找软件采购招标公告并下载公开附件"}'
curl http://127.0.0.1:8010/tasks/TASK_ID
curl -X POST http://127.0.0.1:8010/tools/fetch_tender_detail -H 'Content-Type: application/json' -d '{"arguments":{"url":"https://example.org/announcement"}}'
```

附件下载必须使用公告中发现的链接及 announcement_id。支持 PDF、DOCX 和安全 ZIP 文档包，实际格式必须符合 TaskSpec.file_formats；未指定格式时由模型明确选择支持格式，不会自动放宽 PDF-only 要求。多附件详情页用 plan_acquisition.attachment_urls 选择符合用户用途的入口，已核验项目按 assessment.files 选择，不能因格式受支持就全下载。ZIP 原包保存，不自动解压或执行；检查 CRC、危险路径、加密/符号链接、文件数、解压大小和压缩比，包内支持 PDF/DOCX/XLSX。嵌套 Office 文件还检查内部结构、CRC、宏、嵌入对象及展开上限，不支持含活动或嵌入内容的文件。兼容国网 ZIP 的 GBK 目录与 UTF-8 文件头混合编码，只有名称编码往返与实际文件头一致才接受。

文件入口检查同时读取页面和 JavaScript 弹窗，并保留原项目 announcement_id、source_url 与入口 entry_id。已确认登录限制时向用户说明并跳过项目，不把登录页作为项目证据，不重复检查相同入口；只有改变文件类型或下载范围时才请求确认。用户直接提供页面且未产生搜索候选时，不开放搜索进度整理工具。

有效澄清回复必须对应同会话已答复的确认记录及 reply_task_id。规划会结合原业务目标、追问和当前回复继续判断下载或查询，不把补充回复变成 resume/batch_control；拒绝不会下载，过期或无法关联的回复不能沿用旧授权。编排工具不能作为业务步骤，规划阶段移除编排占位及其依赖，动态更新时拒绝递归编排。反思修复执行前校验页面及入口 ID，未知 ID 不加入计划。项目条件匹配数和符合文件用途的可下载项目数分别统计，均不代替实际文件成功数。
`data/files/` 按项目名称、北京时间下载时间及实际扩展名命名，保留原始名称、实际格式、文件头、大小和 ZIP 目录信息。同一项目相同内容复用文件；不同内容不覆盖。格式拒收会记录原始文件名、大小和文件头，不再把已发现但拒收的附件记作“没有附件”。网页 HTML 和截图不落盘，历史文件不自动删除或重命名。公告内存读取支持 5 MB，单文件支持 100 MB。直接工具接口返回 `{result, log_id}`。

## 本地 MySQL

使用 SQLAlchemy 和 PyMySQL 连接本地 MySQL。在 `.env` 中配置：

```dotenv
DATABASE_URL=mysql+pymysql://bidding:YOUR_PASSWORD@localhost:3306/bidding?charset=utf8mb4
```

需要先创建数据库和有建表、读写权限的账号。例如在已有管理员会话执行（替换示例密码）：

```sql
CREATE DATABASE IF NOT EXISTS bidding CHARACTER SET utf8mb4;
CREATE USER 'bidding'@'localhost' IDENTIFIED BY 'YOUR_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX ON bidding.* TO 'bidding'@'localhost';
```

启动应用时自动创建所需表，不会创建 MySQL 数据库。密码包含 `@`、`/`、`#` 等字符时，在连接 URL 中进行百分号编码。配置后重启应用。空 DATABASE_URL 使用 SQLite，MySQL 配置错误会启动失败，不会悄悄切回 SQLite。切换数据库不会自动迁移已有 SQLite 数据。本地 MySQL 实例已完成连接、迁移和审计写入验证；凭据仅存于本地 `.env`。

将现有 SQLite 迁移至空 MySQL 库时，先停止服务、配置 DATABASE_URL，再执行：

```sh
.venv/bin/python -m bidding.migrate_mysql
```

迁移会先备份 SQLite，再复制所有表并校验行数。目标表必须为空，不覆盖已有 MySQL 数据；复制失败会回滚数据事务。成功后重新启动服务，文件继续保留在原本的本地目录。

表用途：`chat_sessions` 保存会话及当前任务，`chat_messages` 保存用户、助手和工具消息，`tool_calls` 保存工具调用留痕，`records` 保存任务、公告及附件记录。MySQL 中 records.payload 使用 LONGTEXT，消息和工具参数/结果使用 JSON；文件仍存本地。

统一排查入口是 `request_audit` 表：每个问题和关联事件按多行保存，`task_id` 对应一次用户请求，
`session_id` 对应会话。`event_type` 区分 request、message、tool、model、context 和 task。
工具事件在同一行记录开始/结束状态、原问题 `request_text`、工具名、完整 `arguments` / `result`、
`model_call_id`、错误和耗时。结构化密码、Token 等敏感字段脱敏；不截断原始结果。事件与原记录
在同一事务提交，配置 MySQL 后此表也写入 MySQL；未配置时仍写 SQLite，不声称已连接 MySQL。

历史记录可幂等补录（不会删除原表）：

```sh
.venv/bin/python -m bidding.audit
```

按单个问题排查：

```sql
SELECT event_type, tool, status, arguments, result, error, duration_ms
FROM request_audit WHERE task_id = '实际任务ID' ORDER BY created_at, event_id;
```

接口 `GET /request-audit?task_id=实际任务ID` 或 `?session_id=实际会话ID` 支持分页。

执行中发现影响下载目标或授权的歧义时，任务进入 `awaiting_clarification`，
`confirmation.expires_at` 为 3 分钟期限。可选放宽使用 `kind=relaxation`：超时仅拒绝本次放宽，
保留原任务授权、文件类型、日期、数量和检查点，自动按原要求继续；不重复询问放宽，也不重置累计 Token。
必要澄清使用 `kind=required`：缺少安全执行所需信息时保留检查点等待明确要求，不猜测授权。
批量范围尚未授权时仍不自动开始该批量下载。超时继续任务串行占用原会话，其他活动任务结束后才调度。
会话内回复通过 `/chat` 的 `confirmation_task_id` 绑定待确认请求（界面自动携带）。
拒绝回复不会自动授权，由下一轮结合原始问题及回复重新理解目标。
批次范围确认也有相同期限，过期批次不可确认或恢复，须重新发起任务。
下载任务最终无成功文件时，返回明确的 `NO_FILES_DOWNLOADED` 错误及原因；
等待确认、正在下载和已创建队列不作为最终下载失败或成功。

页面证据引用区分 `announcement_id`（页面快照）和 `original_result_id` /
`source_ref.result_id`（不可变的原始工具输出）。分页读取返回 `source_ref`、
`field`、`offset`、`end_offset`、`next_offset` 及稳定 `slice_id`；
切片日志 ID 只用于审计，继续读取时仍指向原始结果。相同读取及包装引用复用缓存，
不同字段和分页不共用固定读取次数限制，仍受全局工具、时间和 Token 预算约束。
读取计划节点仅在同一来源及字段的观察范围覆盖要求后完成，绑定记录见
`observation_binding`；不会把条目读取冒充完整正文读取。
唯一待执行的同角色委派允许主 Agent 细化指令，依赖和子 Agent 权限边界不变，
多个候选委派节点仍须明确选择。反思单步骤对象可规范化成数组，其余结构错误
最多修正一次，校验失败不能视为评审通过。
补录只处理当前配置数据库已有记录；切换到新的 MySQL 库不会自动迁移旧 SQLite 数据。

项目核验由模型根据原文作出判断。新核验参数会绑定至同一页面的未完成核验节点，保留该节点的
依赖关系；其他项目不会替它完成核验。依赖修复后重新计算阻塞状态。空的可执行步骤参数会被拒绝。
调度返回的 `execution_status` 和 `blockers` 区分调用正常返回与业务步骤真正完成。
`execution_progress` 记录新观察及连续无进展轮数，`execution_recovery` 记录有界反思与恢复；
仅改计划、重复读相同片段或改写核验理由不算进展。最多三次恢复，再无进展则保留部分结果停止。
恢复上下文不再重复系统工作集，只保留近期工具引用及证据片段；完整历史仍保存在数据库。
更大的任务仍应使用持久化批次及分工；提高 TASK_TOKEN_BUDGET 不会扩大单次上下文上限，
也不会解除工具尝试、轮数、时间及无进展限制。

执行接口中 `announcement_id` 是页面 ID，`full_result_id` / `log_id` 是日志 ID。
同一会话内可追溯的页面日志 ID 误传时，程序解析到真正页面 ID 并记录 reference_repair；
不会把其他会话的日志当成有效来源。`read_tool_result` 总是返回固定原始结果引用，分页的
`next_offset` 对应本次实际展示的条目；不能对一个切片重复使用原始偏移。
`update_task_plan` 现在局部合并步骤，自动保留未提交步骤及已完成检查点；通过
`remove_step_ids` 显式移除过时的未完成步骤，运行中或已完成步骤不能删除。

## 项目匹配核验

每轮先由模型生成结构化 `TaskSpec`，理解目标、数量、语义条件、文件要求和完成标准；有重要歧义时返回 `awaiting_clarification`，在用户回复前不访问外部网站。“今天开始招标”等表达不再由正则固定解释。行业归属、公告类型及证据含义由模型判断。模型调用 `record_project_assessment` 逐项给出 match/mismatch/unknown 和短原文引用，程序只校验来源、引用存在性、字段结构及已引用截止时间的有效性，不维护行业关键词筛选表。缺失证据不计入匹配结果，失败兜底只展示已核实项目。

`TaskSpec.steps` 是持久化依赖图。`execute_plan_steps` 按依赖执行就绪步骤，最多 3 个并行任务、同站点最多 2 个读取请求；导航串行，前置失败会阻塞依赖项，关键步骤失败需要重新规划。`update_task_plan` 可增加替代来源、修正参数和调整依赖，但不能改写已完成检查点或放宽用户目标。搜索配额耗尽后仍可核验已发现页面。步骤状态、计划更新、技术重试和模型核验分别留痕。用户明确继续时可通过 `resume_task` 复用同一会话未完成任务的已完成步骤；新一轮预算单独计算，过期浏览器动作需要重新获取。

运行中的对话任务可通过聊天栏停止按钮或 `POST /tasks/{task_id}/cancel`（JSON：`session_id`）取消，关联步骤和正在等待的工具一并取消，保存检查点并释放会话。已创建的下载队列独立运行，使用下载任务区域的暂停/取消按钮控制；停止对话不会悄悄取消其他已创建的队列。

下载计划保留模型核验的具体项目及原文指纹；Worker 在下载前比对内容和有效期，原文变化或截止时间过期时等待重新核验，不自行更换项目。完整标书任务只选择模型归类为 `tender_document` 的已观察附件，不把资格预审文件或其他 PDF 当作完整标书。没有可下载文件的项目留下缺口，不计作用户目标成功。

开发者面板默认查看最新一轮请求，可通过请求选择框切换；聊天消息旁的执行记录按钮直接定位该轮。工具列表展示内部名称、中文用途及所属 Skill，展开查看完整参数、结果和错误。同一轮的 Token 用量、上下文事件及模型执行消息关联展示。上下文压缩失败与总 Token 预算耗尽分别显示。普通公告使用保留日期和核验结论的结构化预览，完整正文仍在日志中。

## 多轮对话与日志

第一次 POST /chat 不传 session_id 时会自动创建会话，响应返回 session_id 和 task_id；后续传相同 session_id 即可继续上下文。也可先 POST /sessions 创建会话。

```sh
curl -X POST http://127.0.0.1:8010/chat -H 'Content-Type: application/json' -d '{"message":"查找广州软件采购项目"}'
# 等上一轮任务完成后，使用返回的 session_id
curl -X POST http://127.0.0.1:8010/chat -H 'Content-Type: application/json' -d '{"session_id":"SESSION_ID","message":"把刚才第二个项目的招标文件下载下来"}'
curl 'http://127.0.0.1:8010/sessions/SESSION_ID/messages?offset=0&limit=100'
curl 'http://127.0.0.1:8010/tool-calls?session_id=SESSION_ID'
curl 'http://127.0.0.1:8010/tool-calls?task_id=TASK_ID'
```

同一会话有未完成任务时返回 409，不同会话可以分别执行。模型使用摘要检查点和近期消息，而不是持续携带全部历史工具结果。失败或中断轮次保留完整审计记录，并以未完成任务摘要和工具日志 ID 带入上下文，避免产生缺少工具返回的调用序列。

## Agent Loop 与上下文压缩

所有 `/chat` 请求进入同一 Agent Loop。无需工具时一次模型回复即可完成；需要工具时循环执行模型决策、工具调用、结果压缩和上下文检查。直接 API/MCP 工具调用仍可独立使用。

工具执行结果先完整落库，模型接收精简版本。小结果直接保留；结构化大结果提取有限字段和有序列表预览；长文本超过阈值时使用模型生成结构化摘要。每次整理都在 `context_events` 留痕，包括原始日志 ID、压缩前后估算大小、精简结果和摘要/错误。长文本分块总结，默认最多三个块；未覆盖全文时明确标记 `summary_partial`，不将预览当作完整证据。摘要格式不合法、接口失败或预算不足时回退到有明确截断标记的结构化预览。

Agent 可调用 `read_tool_result(log_id, field, offset, limit)` 读取原始工具结果的指定字段、列表页或文本片段。只允许读取当前会话的日志；无会话任务仅能读取本任务日志。分页读取也独立留痕。

每次主模型请求前检查上下文估算大小。达到阈值时，对较早的完整轮次分批总结，保存目标、用户约束、事实、已完成/待办和引用，保留近期对话。单轮内多个工具交换也能压缩，assistant 的一组 tool_calls 与对应返回不会拆散。当前用户请求单独保留；摘要失败保留原上下文，短期避免重复尝试同一失败压缩。硬限制仍无法满足时明确终止，保留原始记录。

摘要与保留消息保存为 `context_snapshots`，下一轮从最近成功任务的检查点恢复，再追加新消息。失败任务产生的检查点不会被复用。原始 `chat_messages` 和 `tool_calls` 不因压缩而删除。

`model_calls` 记录所有主循环和摘要模型请求的用途、模型、耗时、状态、输入估算及服务端 usage。任务汇总输入/输出 Token，摘要调用也计入预算。接口没有返回 usage（包括超时）时标记未知，以请求估算加输出预留计入预算，不能当作零费用。估算采用 UTF-8 字节数 / 2，属于保守启发式，不是模型精确分词，也不是费用账单；实际用量以 provider usage 为准。尚未实现按价格计算金额。

默认配置可在 `.env` 覆盖（完整列表见 `.env.example`）：

| 配置 | 默认值 | 含义 |
|---|---:|---|
| TOOL_RESULT_MAX_TOKENS | 2000 | 单个工具精简结果的估算上限 |
| CONTEXT_COMPACT_THRESHOLD_TOKENS | 24000 | 触发历史摘要的输入估算阈值 |
| CONTEXT_HARD_LIMIT_TOKENS | 32000 | 输入估算与输出预留的总上限 |
| CONTEXT_KEEP_RECENT_TURNS | 2 | 优先保留的近期用户轮次 |
| SUMMARY_MAX_TOKENS | 1500 | 每次摘要输出上限 |
| SUMMARY_INPUT_MAX_TOKENS | 8000 | 摘要批次的输入目标大小 |
| TOOL_SUMMARY_MAX_CHUNKS | 3 | 一次工具结果最多总结的块数 |
| TASK_TOKEN_BUDGET | 300000 | 单任务模型累计 Token 预算，可按成本要求配置；不是单次上下文上限 |
| TASK_TIMEOUT_SECONDS | 600 | 单任务总时限 |
| MODEL_TIMEOUT_SECONDS | 90 | 单次模型网络请求超时 |
| MODEL_MAX_OUTPUT_TOKENS | 2048 | 主模型每次输出预留 |
| AGENT_MAX_ROUNDS | 24 | 主模型循环次数上限 |
| AGENT_NO_PROGRESS_ROUNDS | 3 | 连续无新证据或文件进展的轮数，触发独立恢复评审 |
| AGENT_MAX_RECOVERY_ROUNDS | 3 | 无进展恢复最多三轮；达到上限保留部分结果停止 |
| AGENT_MAX_TOOL_CALLS | 48 | 工具调用次数上限 |
| AGENT_MAX_TOOL_ATTEMPTS | 72 | 含自动重试的工具尝试总上限 |
| TOOL_MAX_ATTEMPTS | 3 | 可安全重试工具的总尝试次数，包含首次 |
| MODEL_MAX_ATTEMPTS | 3 | 模型临时网络故障的总尝试次数 |
| TOOL_TIMEOUT_SECONDS | 90 | 单次普通工具执行超时 |
| RETRY_BASE_SECONDS | 0.5 | 指数退避基数，附带随机抖动 |

网络连接故障、超时及临时 5xx 有限重试；429 遵守 `Retry-After` 并降低该运行时的站点并发。等待不能超出任务总时限。401/403 需要权限或人工处理，404/410 应换来源；参数错误和业务不匹配不盲目重试。导航动作结果可能不确定，因此不自动重复点击。每次尝试独立保存完整工具日志，`tool_attempt` 关联同一逻辑操作；模型请求的重试也计入 Token 用量。计划结构错误最多修正三次，技术重试和模型换策略分别计数，均受整体预算约束。

DeepSeek 默认禁用 thinking，减少推理上下文占用；可用 MODEL_THINKING 调整。开启时保留 reasoning_content，遵循该服务工具调用协议。摘要也使用配置的模型。

前端工具记录面板显示会话累计已知 Token 用量、未知用量次数，以及工具结果压缩、历史摘要和模型调用记录。也可查询：

```sh
curl 'http://127.0.0.1:8010/model-calls?task_id=TASK_ID'
curl 'http://127.0.0.1:8010/context-events?session_id=SESSION_ID'
```

普通对话任务达到预算、循环次数或时间限制时分别返回 budget_exceeded 或 timed_out，保留已有文件和日志。下载任务另外使用持久化批量队列，见下节。

## 下载范围与批量任务

下载请求先读取源页面，再由 `plan_acquisition` 建立约定。单项目处理其公开附件；明确小列表未指定数量时默认前 3 个项目。大列表、有分页或数量未知时，未指定范围的请求进入 `awaiting_confirmation`，不下载文件。用户可在任务区域选择数量或全量；明确要求全量/数量的对话可直接创建任务。模型提出的全量范围不是授权，服务端也校验原始用户表达。

对话状态 `scheduled` 仅表示下载任务已创建，绝不表示下载完成。队列每个项目记录 `pending/running/downloaded/no_attachment/needs_input/failed`；已处理数和成功下载数分开。`completed` 要求约定项目范围的下载目标实际满足；无公开附件、失败、数量不足和容量/时间/分页限制保持 `partial_completed`，需人工处理是 `needs_input`。资格预审公告不是完整招标文件。

默认一次运行一个批量任务，同站点 2 个项目 Worker 并行；下载容量检查串行，避免并发超额。下载 Worker 不重复做模型语义判断，执行前检查保存的模型核验记录。每个可安全重试的工具最多尝试三次，不再叠加整项目重试，所有参数/完整结果仍落在原会话审计中（source=batch）。项目清单持久化在 `batch_items`，恢复时利用已保存文件和项目检查点去重；极端崩溃边界是至少一次执行，内容哈希去重，不承诺外部操作 exactly-once。

可暂停、取消、继续及仅重试失败项目，取消保留已保存文件。服务重启后批量任务转为暂停，需主动继续，避免重启触发未经确认的大量网络请求。分页枚举从入口重新走，利用已存清单去重；尚未发现的新项目继续补齐。

配置 `.env`：`BATCH_CONCURRENCY=2`（1..3），`BATCH_TIMEOUT_SECONDS=1800`（继续会重新获得时限），`BATCH_MAX_BYTES=2147483648`（累计容量不因继续重置）。每批最多 5000 项/200 页、单项目最多 30 个附件，单文件最多 100 MB。带语义条件的全量任务须分段核验并保存目标清单，不能用少量已核实项目冒充全量，也不能通过确认数量跳过筛选条件。

接口：

- `GET /batches?session_id=...` 查看当前会话批量任务。
- `GET /batches/{id}` 查看约定、进度、状态、原因。
- `GET /batches/{id}/items?offset=0&limit=100` 分页查看项目和文件。
- `GET /batches/{id}/manifest` 下载完整 JSON 项目与文件清单。
- `POST /batches/{id}/control`，JSON 包括 `session_id`、`action`（confirm/pause/cancel/resume/retry_failed）；confirm 还包括 `scope=count|all` 和 count 范围 1..5000。

仍按单 API 进程部署，不是分布式队列；未实现租户鉴权、动态并发调节、子 Agent 异常推理或多站点独立调度。生产部署应进一步拆分 Worker 服务、添加数据库租约/心跳、权限和站点级限流。这里的会话归属检查不是登录鉴权。

Agent、直接 API 和 MCP 的采集工具均通过同一个日志入口：执行前记录工具名、参数、来源、会话/任务/模型调用 ID 和开始时间，执行后记录结果、状态、结束时间、毫秒耗时和错误。进程异常退出后，未完成任务及调用在下次 API 启动时标记 interrupted。独立 API/MCP 调用没有会话 ID，仍可从日志查询。常见密钥字段会遮盖；不会记录模型请求中的 Authorization 头。工具日志落库失败时不继续执行工具，避免无记录操作。

开发者面板的工具记录每页 100 条，最新优先；展开时才读取完整原始结果，执行期间自动更新。`GET /tool-calls` 支持 `newest_first=true&include_results=false` 返回轻量列表，`GET /tool-calls/{log_id}` 返回完整参数/结果，避免批量任务反复加载所有网页原文。项目/文件面板每页 50 项，可逐页查看，也可导出完整清单。

## Skill 与 MCP

`skills/tender-acquisition/SKILL.md` 定义业务流程。`agent.py` 提供单 Skill 工具调用循环；模型按请求决定是否调用工具，不对所有问题自动搜索。`mcp_server.py` 将同一组采集工具通过 stdio 暴露给其他 Agent：

```sh
uv run python -m bidding.mcp_server
uv run pytest
```

## 当前边界

- 提供通用静态 HTML 适配器，以及中国政府采购网 HTML 适配器；后者提取发布时间、采购单位和采购项目名称，未知字段为 null。
- 静态解析加浏览器渲染不能保证识别所有站点。复杂筛选表单、iframe 内容、登录、报名、付费及验证码仍需额外站点适配或人工参与；已支持的分页只针对实际识别到的下一页入口。
- DeepSeek 模型已完成真实对话和摘要验证；搜索通过配置的 MCP Provider 接入，可用性取决于服务权限和配额。已用中国政府采购网和国家电网公开页面验证附件下载，不能保证任意网站均可采集。
- 对话推理仍使用进程内后台执行，重启后未完成对话标记 interrupted；批量下载清单和检查点持久化，重启转暂停后可继续。当前 API 仅绑定本机，未实现多租户、鉴权、DNS 固定绑定或分布式限流。
- 已支持模型计划、语义核验、依赖执行和有限技术重试；大型语义筛选任务仍须在预算内分段推进。运行时并发和限流是本进程范围，未实现跨进程分布式调度。
- Skill 目前从源码目录加载，按源码部署；单独安装 wheel 时需要另外分发 Skill。

后续可添加站点适配器、搜索 Provider、独立部署的 Worker；专业供应商 API 独立于通用搜索接入。

## 实站验证

2026-10-08，从中国政府采购网公告 `https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202609/t20260925_27402940.htm` 读取“中共广州市委政法委员会2026年广州政法办公平台智能辅助升级项目”公告，成功下载公告关联的公开 ZIP 招标文件和 PDF 采购附件到 `data/files/`。此公告仅用于验证采集，不代表推荐投标。
