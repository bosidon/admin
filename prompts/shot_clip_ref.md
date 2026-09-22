# 分镜片段 · H3 多图参考（ref2va）提词模板

出片段时由代码按分镜字段填充下面的 `{xxx}` 占位符 —— 措辞你可以随便改，**但别删占位符**（删了对应内容就不会出现在提词里）。

可用占位符：
- `{ref_map}` 参考图编号对应表（例：`<Picture 1>=场景「禅意书房」、<Picture 2>=人物「我」…`）
- `{subjects}` 主体定义逐条（`<Subject N> 是 <Picture M> 中的…：外观`）
- `{summary}` 一句话概况（景别 + 运镜）
- `{retention}` 特征保留/防串脸约束
- `{style}` 整体风格句（代码里 `H3REF_STYLE`，画质/质感词）
- `{shot_type}` 景别（全景/中景/特写/细节/运动）
- `{camera_move}` 运镜词（固定/推近/拉远/左移/右移/升降/俯仰/环绕）
- `{visual}` 动作与场面（分镜 visual 原文）
- `{camera_directive}` 运镜的硬约束句（按运镜词生成，如「镜头全程不动，不推不摇不变焦不换机位」）
- `{dialogue}` 台词（`<d>[Chinese] 台词</d>`；该镜无台词则为「本镜全程不说话（无对白）。」）
- `{end_state}` 镜头结束状态
- `{soundscape}` 环境音
- `{music}` 配乐（取分镜 music_hint，空则「无配乐」）

⚠️ 实测要点（2026-09-23，6000D）：台词必须走在 `{dialogue}` 里输出 `<d>[Chinese] …</d>`；不写标记时模型会拿本模板正文的碎片瞎念。

---

subject_definitions:
参考图与内容一一对应：{ref_map}。
{subjects}

summary:
[reference generation] {summary}

retention_analysis:
{retention}

detailed_description:
{style}
[Shot 1] {shot_type}。{visual}，{camera_directive}。

台词：{dialogue}

镜头结束状态：{end_state}

overall_soundscape:
{soundscape}

non_diegetic_music:
{music}
