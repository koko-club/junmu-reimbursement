# 更新日志

## v1.0.1 - 2026-09-09

- 完成君牧在线报销系统的认证、报销填写、报销记录和回收站流程。
- 完成 Apple/Aurora Glass 风格界面、统一图标、Logo 和响应式交互。
- 增加报销统计卡片、报销记录展示字段和文件下载按钮。
- 增加里程截图开发提示悬浮窗，并保留原文件选择逻辑供后续启用。

## 后续版本规范

- 修复问题或小幅兼容性调整：递增补丁号，例如 `v1.0.2`。
- 增加向后兼容功能：递增次版本号，例如 `v1.1.0`。
- 发生不兼容变更：递增主版本号，例如 `v2.0.0`。

每次发布都应同步更新 `VERSION` 和本文件，然后创建同名 Git 标签：

```sh
git add VERSION CHANGELOG.md
git commit -m "chore: release vX.Y.Z"
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin HEAD --follow-tags
```
