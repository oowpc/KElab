# 内嵌 KE 语法（ke_syntax.lark）

- 来源：`工作项目\NL2KE与KE\KE-matcher\KE-matcher\ke_syntax.lark`（KE-matcher 仓声明的 normative 语法，spec 0.1.0+nl2sql）
- 内嵌时间：2026-10-21；md5：`cefad34d71fb90c4dafb21908b7034e1`
- 为什么内嵌：kelang-nl2sql 仓自带的旧版语法解析 conic 风格 KE 0/20 全失败，必须用 KE-matcher 版；
  平台需要自包含（换机器不依赖外部绝对路径）。domain 配置 `grammar_md5` 锁定内容，防漂移。
- 与上游同步：如需更新，用新文件覆盖本目录文件，并同步更新 `configs/domains/*.toml` 里的 `grammar_md5`
  （或在配置页留空 md5 字段，保存时自动重算）。
