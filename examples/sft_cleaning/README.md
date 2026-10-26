# DataTrove SFT文本质量清洗配方

这是`Tiachi/datatrove`的`codex/sft-cleaning-v1`分支中的独立配方。上游基线为DataTrove `v0.10.1`，提交`1977fbb0f3c163d43cace334b073dda17fa3ef26`；上游核心文件不改写，代码集中在`examples/sft_cleaning/`。

**用途：清洗已有回答的SFT。** 不处理RL题库，不生成新回答，不自动补CoT，不下载语料/模型，不打开媒体。模型只作文本质量评审；数学、代码、SQL和Lean的执行验真不在本版实现范围。

## 1. 已登记来源

| 来源 | 处理 |
|---|---|
| Modotte/MathX-5M | 有/无思考均保留其原训练意图 |
| open-thoughts/OpenThoughts3-1.2M | 保留多轮、数学/代码/科学内容 |
| nvidia/Nemotron-SFT-Science-v2 | 保留工具定义、调用、返回和最终回答 |
| nvidia/Nemotron-SFT-Competitive-Programming-v2 | Python/C++/SQL等分量可单独登记文件 |
| nvidia/Nemotron-SFT-Math-v4 | 完整解答原文保留，模型分数不代替答案验证 |
| nvidia/Nemotron-Math-Proofs-v1 | 已有显式QA或配置字段映射的部分；未知证明格式进入deferred |
| openbmb/UltraData-SFT-2605 | 必填能力config及think/no_think split |
| openbmb/Ultra-FineWeb-L3 | **仅显式QA结构**，纯正文/多风格改写进入deferred |
| nvidia/Nemotron-SFT-Instruction-Following-Chat-v3 | 必填chat或instruction_following；chat只监督最后一个assistant |

登记不代表已经下载、获授权、核对所有版本字段或验收数据。默认没有启用任何文件，必须填写现有标注路径；不猜测服务器路径。原validation/test和LongBench等评测不进入训练。

新增来源的依据：[UltraData-SFT-2605](https://huggingface.co/datasets/openbmb/UltraData-SFT-2605)、[Ultra-FineWeb-L3](https://huggingface.co/datasets/openbmb/Ultra-FineWeb-L3)、[Nemotron Chat v3](https://huggingface.co/datasets/nvidia/Nemotron-SFT-Instruction-Following-Chat-v3)。前者需要访问条件，后者部分原始提示依赖上游恢复；本配方不绕过访问限制。Chat中缺失的源提示进入deferred，不凭参考回答编造问题。

## 2. 部署与配置

在已经检出的本fork实现分支中，可以运行：

```bash
bash examples/sft_cleaning/deploy.sh
```

它使用当前检出提交为锁定版本，并核对远端分支、fork身份及上游祖先；只允许干净工作树、快进更新。初始代码获取须使用直连或用户批准的网络路线。已有Dataset工程的用户也可以用其SHA锁定bootstrap一次完成代码获取、安装和自测。

| 位置 | 默认值 |
|---|---|
| fork代码 | `/mnt/sfs/ltj/project/datatrove`（独立部署用当前checkout） |
| 独立venv | `/mnt/sfs/ltj/runtime/datatrove-sft-cleaner` |
| 用户配置 | `/mnt/sfs/ltj/text_cleaning/config/server.json` |
| 短命令入口 | `/mnt/sfs/ltj/text_cleaning/bin/sft-clean` |
| 分阶段结果 | `/mnt/sfs/ltj/text_cleaning/runs/` |
| 本地小型去重索引 | `/tmp/datatrove-sft-cleaner/`，默认预算8GiB |

部署只下载代码与依赖、运行合成自测；不会启动真实模型请求。安装环境与原模型Python/NPU环境隔离，使用已有Python3.10+，不安装torch/vLLM。Parquet依赖可在`config/deployment.json`关闭，默认安装。个人HTTP/HTTPS/ALL代理在子进程禁用，Git代理按子进程覆盖，不修改全局网络配置；pip使用配置的直连索引。

已有用户配置不会被默认值覆盖；不自动stash/reset/clean，不删除失败缓存或旧结果。配置目录与代码分开，升级不丢路径、阈值和API设置。

配置模型只需base URL和served model name：

```bash
/mnt/sfs/ltj/text_cleaning/bin/sft-clean configure --base-url http://IP:PORT/v1 --model qwen3.8-27b
/mnt/sfs/ltj/text_cleaning/bin/sft-clean api-check
```

`api-check`只GET `/v1/models`，核对名字，不生成文本。若服务要求认证，将key放入环境变量`SFT_CLEAN_API_KEY`；不放进JSON、命令参数、代码库或日志。HTTP客户端不读取系统代理，不自动重定向。

为真实文件登记来源；下面的路径是待替换示例，不会自动创建或下载：

```bash
/mnt/sfs/ltj/text_cleaning/bin/sft-clean add-input --dataset openbmb/UltraData-SFT-2605 --path /实际标注路径.jsonl --split think --hf-config Math
/mnt/sfs/ltj/text_cleaning/bin/sft-clean add-input --dataset nvidia/Nemotron-SFT-Instruction-Following-Chat-v3 --path /实际chat路径.jsonl --split chat
/mnt/sfs/ltj/text_cleaning/bin/sft-clean inspect
```

支持JSONL、顶层JSON数组、Parquet；显式文件清单，不递归扫媒体目录。可重复add-input添加多个分片；同一文件不会重复登记。用`--revision`记录实际数据版本。`inspect`每文件最多3条，只打印字段和结构，不打印QA。

`server.json`可配置全部输入、各run输出根、scratch预算、CPU workers、ngram规则、API并发/超时/上下文字符保护/短输出长度、质量阈值和明确holdout文件。CPU worker数与HTTP并发分别控制；模型请求只有一个全局并发池，不随CPU worker数放大。

## 3. 运行步骤与中间产物

```bash
/mnt/sfs/ltj/text_cleaning/bin/sft-clean doctor
/mnt/sfs/ltj/text_cleaning/bin/sft-clean mechanical
/mnt/sfs/ltj/text_cleaning/bin/sft-clean pilot --limit 2000
/mnt/sfs/ltj/text_cleaning/bin/sft-clean run
```

`mechanical`不调用API。`pilot`和`run`会提交真实QA评审；先小规模校准阈值与费用再放量。pilot按各文件前缀分配预算，**不是随机准确率样本**，不据其正确率外推全池。整行多轮保留，模型对被监督回合按最差情况给行级分数。

| 阶段 | 产物及统计 |
|---|---|
| `run.json` | 配置、输入文件大小/mtime、代码SHA、包版本、采样方式；不含API key值 |
| `01_mechanical` | candidate/review/deferred/rejected原记录信封；空字段、结构和ngram检查、来源/状态、被监督assistant回合数 |
| `02_exact_dedup` | retained、duplicates、holdout_matches；完整对话+loss mask精确重复及明确保留集的上下文精确匹配 |
| `03_quality` | 逐行模型标签、质量分、API技术失败原因、token用量、每chunk请求检查点 |
| `04_delivery` | 候选原文、训练视图、旁路属性、README和来源/领域/语言/CoT统计 |

每阶段以staging发布，已完成阶段复用；输入/规则/模型设置/代码变化会形成新的run，旧结果保留。文件身份默认绑定路径、大小和mtime，**不等于原始文件完整字节SHA验收**；输出写入时计算SHA，快速恢复检查身份与大小，不自动重读全部中间文件重hash。

多阶段原文会占用数倍标注空间，部署不会预留全量空间；放量前按实际文件容量准备SFS配额。只有小型hash索引放scratch，默认8GiB预算；原文不复制到本机临时盘。中断索引保留、预算满则停止，不自动删除目录。

### 格式与内容保护

- messages/conversations、明确question/answer、query/response、instruction/output、problem/solution、qa_pairs均有适配；其他字段可配置`field_map`。
- 不通过猜测“Q:”等自由文本标记把FineWeb正文变成SFT。结构不明进入deferred，仍保留原记录与定位。
- 保留`reasoning_content`和原工具轨迹；工具调用ID与返回对应不全则延后处理。未知Lean字段可映射，但不执行证明或冒充编译通过。
- 不因没有CoT而删除no_think；ngram疑似循环默认review，不一刀切删除正常代码/数学。
- model输入超字符保护阈值时待复核，不截断；实际模型上下文超限也不会当成正确样本。字符保护不是精确token长度保证。

### 模型输出与训练mask

完整评审prompt在[`sft_cleaner/judge.py`](sft_cleaner/judge.py)的`PROMPT`中。短JSON输出为match、error、refusal、quality、cot、language、reason；程序负责校验和写属性。

`quality`为1—10；`cot`为0已有有用推理、1没有且不适合/不需要补、2没有但适合补。只作分类，不生成CoT。默认≥8进候选、≤3或明显错配/错误/不合理拒答进拒绝，其余待复核；这些是**待校准工程起点，不是已证明最优阈值**。合理安全拒答不自动低分。

`MODEL_CANDIDATES_NOT_VERIFIED`明确表示文本模型代理评审，不代表数学真值、测试通过、Lean编译或训练格式认证。模型被要求不捏造这些验证。

**训练器必须使用`selected.training_view.jsonl`中的`train_turns` mask。** Chat v3完整历史保留，只有最后assistant为true；`selected.original.jsonl`是回溯副本，不能忽略mask全回合训练。视图是配方内部协议，接入具体训练器/UniMoE writer仍需验证，不能拿一个自定义JSON校验器冒充正式训练格式合规。

### 请求恢复

每chunk先durable保留请求键，再调用API。模型/输入/prompt不变的已成功请求复用；超时或进程中断留下结果不明的请求时，默认阻止重发。确认接受潜在重复计费后，才在**同一个命令/limit/run**显式加`--retry-uncertain`。这不是要求对整个任务重复跑一遍。

解析失败/HTTP错误进入review并统计，不当作低质量0分或通过。技术失败与质量判断分开。没有配置holdout不宣称已去污染；本版也未实现MinHash、语义去重或执行验证器。

## 4. 验证状态与开发入口

自测：`sft-clean self-test`。使用合成数据和本机模拟HTTP服务，没有生产API费用；测试覆盖恢复不重发、未知请求保护、Chat mask、FineWeb延后、精确去重、holdout、分阶段计数及配置保留。

Windows本地已验证单worker真实DataTrove块及HTTP闭环；Linux多worker测试在Linux部署自测中执行。SSH连接失败时不声称服务器部署完成，查看`DEPLOY_PASS`回执后再配置实际数据和接口。

实现边界与维护说明见[HANDOFF.md](HANDOFF.md)。上游DataTrove许可保持不变；数据集许可与授权独立核对，仓库中不放训练原文、令牌、模型或缓存。
