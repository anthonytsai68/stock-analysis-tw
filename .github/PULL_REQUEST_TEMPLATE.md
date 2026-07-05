<!--
For Chinese contributors: 请直接用中文填写。
For English contributors: please fill in English. All fields marked (EN) accept English.
-->

## PR Type

- [ ] fix
- [ ] feat
- [ ] refactor
- [ ] docs
- [ ] chore
- [ ] test

## Background And Problem

请描述当前问题、影响范围与触发场景。
*(EN) Describe the problem, its impact, and what triggers it.*

## Scope Of Change

请按实际 `git diff` 全量列出本次变更的模块与文件范围（建议注明文件总数）。

- 文件总数：
- 文件清单：
  - 

若本 PR 包含 `.github/**`、`AGENTS.md`、`CLAUDE.md`、`.github/instructions/**`、`.claude/skills/**` 等协作与治理文件，请补充“变更原因 + 影响面 + 回滚方式（默认 revert）”。

## Issue Link

必须填写以下之一：
- `Fixes #<issue_number>`
- `Refs #<issue_number>`
- 无 Issue 时说明原因与验收标准。

## Visual Evidence (if applicable)

若修改了报告格式、报告渲染效果或 Web UI，请在此处附受影响报告页/页面截图；前后对比优先。

- 截图链接（或外部可访问证据）：
- 产物路径（示例）：`apps/dsa-web/test-results/**/*.png`
- 复现命令（示例）：
  - `cd apps/dsa-web && npx playwright test e2e/smoke.spec.ts --grep "settings page"`
- 无法截图时需写明原因与可复现替代证据。

## Verification Commands And Results

请填写你实际执行过的命令和关键结果（不要只写“已测试”）。

- ai-governance：pass / fail（链接）
- backend-gate：pass / fail（链接）
- docker-build：pass / fail（链接）
- web-gate：pass / fail（链接）

- Head/CI 结果（建议粘贴一行）：`当前 Head CI：ai-governance:..., backend-gate:..., docker-build:..., web-gate:...`

## Compatibility And Risk

请说明兼容性影响、潜在风险（如无请写 `None`）。

- 是否触及运行时配置、路由或迁移语义：是 / 否
- 若触及，说明受影响的调用链与回滚方式。
- 若仅为展示字段（如历史快照元数据）改动，请在此明确写明：历史展示字段不影响运行时调用路径、持久化清理与迁移。

## Rollback Plan

请写一条可执行的回滚方案（必填）。

- 默认可回退方案：`revert this PR`，并说明是否需要外部配置回填。

## Checklist

- [ ] 本 PR 有明确动机和业务价值 / This PR has a clear motivation and value
- [ ] 已提供可复现的验证命令与结果 / Reproducible verification commands and results are included
- [ ] 已评估兼容性与风险 / Compatibility and risk have been assessed
- [ ] 已提供回滚方案 / A rollback plan is provided
- [ ] 若修改报告格式或 Web UI 界面，已在 PR 正文/评论附受影响页面截图，不提交一次性截图文件到仓库 / If report formatting or Web UI changed, attach screenshots in PR body/comments and do not commit one-off screenshots in the repo
- [ ] 若涉及用户可见变更，已同步更新相关文档与 `docs/CHANGELOG.md`；`README.md` 仅用于首页级信息变化，细节优先写入 `docs/*.md` / If user-visible changes are included, relevant docs and `docs/CHANGELOG.md` are updated
