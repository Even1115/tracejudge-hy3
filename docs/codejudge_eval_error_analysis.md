# CodeJudge-Eval full-v1 误判错误分析

日期：2026-09-05  
对象：`data/codejudge_eval/runs/full-v1`（90 条 held-out，全部 valid_judgment）  
性质：judge-only 错误分析；不含过程正确率/首错定位/证书重放结论。

## 功能判错总览（4/90）

| # | 样本 | 金标 | Judge 判定 | 置信 | 分析结论 |
| --- | --- | --- | --- | --- | --- |
| 1 | easy/135（wenxin 候选） | A=AC | TLE | 0.95 | **Judge 真错（FP）**（解析论证，见下） |
| 2 | easy/4123（gpt4 候选） | C=Not AC | AC | 1.00 | **金标噪声**（沙箱差分执行确认） |
| 3 | medium/83（magiccoder 候选） | C=WA | AC | 0.99 | **金标噪声**（沙箱差分执行确认） |
| 4 | hard/171（codegeex 候选） | C=WA | AC | 1.00 | **金标噪声**（沙箱差分执行确认） |

结论：对金标的 accuracy 为 86/90 = 95.6%（Wilson [89.1%, 98.3%]；注意 gold 分布
AC=4/非 AC=86，该值与 majority baseline 完全相同，不能单独作为能力证据）。
4 条分歧中 1 条是 Judge 真实失效，3 条经沙箱差分执行判定为金标噪声。
**对 4 个功能分歧样本做事后复核后，judge 判定与复核结论在 89/90 上一致
（98.9%）——这是探索性结果，不是修正后的完整 ground truth**：只有 4 个分歧样本
接受了复核，其余 86 条 judge/gold 一致样本没有全部接受独立执行复核。FPR 仍按
金标口径报告（1/4，分母过小，见选择协议限制）。

## Case 1（已确证）：easy/135 — Judge 复杂度推断的浅层失效

- 题意：判断 n mod i（1≤i≤k）是否两两不同，n,k ≤ 10^18。候选用 O(k) 循环 +
  遇重复提前退出。
- Judge 解释：`k` 可达 1e18，O(k) 必 TLE —— **忽略了提前退出**。
- 数学事实：余数链 r_i = i−1 持续当且仅当 i | n+1；链一断（n mod i ≤ i−2）立即
  落在已有余数集 {0..i−2} 中而退出。最不利输入 n+1 = lcm(2..42) ≈ 2.19×10^17
  也在 i=43 处退出（自写代码数值验证：随机 2 万例最坏 11 次迭代，对抗构造 43 次）。
- 结论：候选正确，金标 AC 正确，**Judge 的"看复杂度上界不看提前退出"是真实
  失效模式**。这对应 TraceJudge taxonomy 中 P03_COMPLEXITY_MISMATCH 方向的误判
  （本实验按协议不填写 normalized 结论，仅作定性记录）。

## Case 2–4（判为金标噪声；证据分级）

证据分三级，结论强度递减：

1. **解析层**：题面与候选代码的静态分析未发现违反规格之处；
2. **有限差分测试**（`scripts/differential_verify_codejudge.py`，结果存
   `data/codejudge_eval/differential_verification.json`）：候选代码在项目
   Docker 隔离姿态下执行（no network、只读挂载、drop ALL capabilities、
   no-new-privileges、256m/1cpu/128pids、每用例 2s alarm），与自写参考实现
   差分——覆盖有限，**不是充分性证明**。这是一次独立的可执行验证分析，
   不属于也不宣称证书重放指标。
3. **残留风险**：官方 harness 未重放。差分验证覆盖的是**题面规格**；若数据集
   官方 harness 的输入编码与规格不符（如 CRLF 残留 `\r`），其金标仍可能因
   harness 细节而异——这本身即标签噪声的一种来源。

- **easy/4123**（二字串频率）：穷举 {A,B,C}^2..10 共 88,569 例，候选输出全部
  落在可接受答案集内（0 不匹配/超时/异常）。
- **medium/83**（除法计数）：10 条定向 + 5,000 条小规模随机 + 200 条 n≈100 随机，
  共 5,210 例，打印的 d 全部通过题目条件的算术复核（或正确输出 0）。
- **hard/171**（密码校验）：穷举 {A,a,1,!}^1..8（覆盖大写/小写/数字/特殊符的
  全部决策组合）+ 2,000 条全字符集随机，共 89,380 例，与严格 ASCII 参考完全一致。

## 错误类型混淆矩阵（gold → judge，90 条）

| gold | →AC | →CE | →WA | →RE | →TLE | →MIXED |
| --- | --- | --- | --- | --- | --- | --- |
| AC (4) | 3 | | | | 1 | |
| CE (4) | | 4 | | | | |
| WA (35) | 2 | | 25 | | | 8 |
| RE (10) | | | | 10 | | |
| TLE (2) | | | | | 2 | |
| MIXED (9) | | | 2 | | | 7 |
| NOT_AC_UNSPECIFIED (26) | 1 | | 12 | 4 | 2 | 7 |

观察：

1. **CE/RE/TLE 单类判定完美**（4/4、10/10、2/2）。
2. **Judge 系统性过度预测 MIXED**（fp=15）：gold 为单类 WA 时有 8 条被判 MIXED，
   gold 为 NOT_AC 时有 7 条判 MIXED——judge 倾向于同时怀疑多种错误。
3. easy 的粗粒度 NOT_AC 被 judge 细化为 WA 12 / MIXED 7 / RE 4 / TLE 2，
   与 hard 文件的细粒度分布方向一致，说明 crosswalk 中 easy C 保持 unmapped
   是正确的保守选择。
4. WA↔AC 的 2 条混淆即 Case 3/4（事后复核判为标签噪声，探索性结论）。
5. 本矩阵混合了 judge 可输出与不可输出的 gold 标签：由其计算的任何全局
   macro-F1 只能作诊断（zero_division=0 下七类机械值为 0.590），不能称为
   CodeJudge-Eval 原生七类分类能力；兼容子集（64 条、6 类）的 coarse
   macro-F1 为 0.809。执行结果一致性应使用 verdict 推导的
   execution-outcome 指标（37/47 = 78.7%，Wilson [65.1%, 88.0%]），不得用
   根因 `error_type` 与执行结果 gold 直接比较。
