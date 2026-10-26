# 给后续Agent的实现交接

## 目标与已确认决定

只清洗已有回答的SFT；8份旧RL题库和评测集不参与。6份原SFT/证明候选加UltraData-SFT-2605、Nemotron Chat v3，另登记Ultra-FineWeb-L3显式QA，共9个来源入口。原始数据只读，不改答案、不自动生成CoT。FineWeb正文/格式未知部分单独保留deferred。

上游固定DataTrove v0.10.1/1977fbb0f3c163d43cace334b073dda17fa3ef26。应用代码与上游核心隔离；实现分支codex/sft-cleaning-v1。不要自动追随upstream main或改写历史。个人代理禁止默认使用，尤其下载代码、依赖、数据；HTTP模型客户端trust_env=False。

## 文件职责

| 文件 | 职责 |
|---|---|
| config/server.example.json | 9来源登记、loss策略、规则、阶段路径、API并发与阈值；无实际数据路径和秘密 |
| config/deployment.json | 独立代码/venv/配置/launcher位置及直连pip索引 |
| sft_cleaner/core.py | 配置检查、显式格式适配、原文信封、loss mask、保守重复指标与签名 |
| sft_cleaner/steps.py | 真实DataTrove Reader/机械检查/Writer；不是stub |
| sft_cleaner/pipeline.py | staging、manifest、精确去重、阶段恢复、API标签对齐与候选输出 |
| sft_cleaner/judge.py | 完整prompt、endpoint规范、模型schema、一个HTTP并发池、chunk请求恢复 |
| sft_cleaner/__main__.py | configure/add-input/doctor/inspect/mechanical/pilot/run/api-check/self-test |
| scripts/deploy_datatrove_sft.py | 不改全局配置的共享安装器；保护用户配置、快进、无生产请求 |
| deploy.sh / run.sh | 独立子进程入口，拒绝source，短命令 |
| tests/ | 合成单元与真实DataTrove+本机HTTP集成测试，无数据/模型访问 |

## 关键机制

1. `normalize`保留original，并生成messages/train_turns，未知格式deferred。Chat v3 split=chat强制只监督最后assistant；source metadata或顶层mask冲突时不能猜。split必须显式，不能误用train默认值把chat当全回合训练。
2. `SFTReader`不经过默认reader的空text跳过逻辑，空/坏行计入统计。一个DataTrove task拥有完整文件，不自动把单大JSONL按字节切给多worker。
3. 单worker使用DataTrove公共PipelineStep调用接口直接消费；多worker使用LocalPipelineExecutor。不要为Windows自测强制启动不必要Manager；Linux多worker由部署测试覆盖。
4. 精确重复键为完整规范对话+监督mask，不能改成只按问题删掉其他正确解法。holdout是去掉最后答案后的完整上下文精确签名；并非全面模糊去污染。
5. SQLite只存小型签名/定位索引，位于配置scratch，8GiB预算，原文在SFS。没有自动递归删除，失败缓存保留。
6. 质量分是整行被监督回合的最差情况；metadata监督回合数包含assistant工具调用，不等于纯QA数。API结果按原行顺序写出，即使请求异步完成。
7. chunk reserved先落盘，成功短结果随后写段日志。中断时未确认请求不自动重发；--retry-uncertain是潜在重复计费的显式授权。严禁修改成普通异常后无限重试。
8. 候选training_view带mask；original只用于追溯，不可丢掉mask直接训练。模型分数不是执行验真或训练器schema认证。

## 状态与未完成项

- implemented：输入适配、机械检查、全局精确去重、明确holdout精确匹配、短JSON模型评审、请求恢复与阶段计数、候选输出、一键独立部署。
- locally verified：固定DataTrove包、单worker真实块、模拟HTTP端到端、Parquet（可选依赖安装后）、保留配置和不重复请求等测试。以最终运行记录为准。
- server runtime：本轮SSH连接被拒绝，尚未部署/连接真实Qwen服务，不宣称真实数据吞吐或质量。
- unknown：实际下载路径、来源revision、格式变体、真实API地址/served name、输入容量和全量费用。
- not implemented：Lean/mathlib编译、代码/SQL sandbox测试、数学符号答案验证、MinHash/语义重复、自由文本FineWeb QA分割、外部seed prompt恢复、全量tokenizer测长、具体训练器导出。

不能把这些未实现内容写成已验证。新增来源样本应先inspect/小规模mechanical，再写映射与合成回归，不要依据数据卡一句“带CoT”拼接未知字段。

## 下一步顺序

先部署自测和doctor，再填写实际路径/接口；inspect看缺字段，处理deferred比例；运行mechanical；约2K分层人工审阅校准（现有pilot仅文件前缀不是随机评估样本）；10K测速与token费用统计；最后再run。真实验收应独立核对题答正确性、来源/任务/语言、mask、holdout和API技术错误。

若需要正式UniMoE交付，先重新完整读取当时的data_format.md，再实现writer与训练mask对应；不要用内部信封或当前README替代正式合同。
