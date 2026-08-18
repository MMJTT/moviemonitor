# Agent Mail Worker 验证证明设计

日期：2026-08-18  
状态：待用户最终确认

## 1. 背景与问题

生产部署把 Agent Mail CLI、Linux keyring 和持久化凭据仅挂载给 Worker，Web 镜像不安装 CLI，也不挂载 keyring。这是正确的最小权限边界。

真实服务器验收发现，现有 `create_task()` 会在 Web 请求中同步调用 `verify_agent_mail()`。因此 Web 即使看到数据库中的已验证状态，也会因为缺少 CLI 而返回 `agent-mail-cli-unavailable`，导致用户无法通过页面创建任何监控任务。邮件设置页的同步测试按钮存在相同问题。

本设计保留 Worker 独占凭据的安全边界，让 Worker 生成有时效的验证证明，Web 只消费证明和 Worker 健康状态。

## 2. 目标与非目标

### 目标

- Web 在不接触 Agent Mail CLI、keyring 或 OAuth token 的情况下创建任务。
- Worker 在启动、定期检查和用户请求时验证固定发件人身份。
- 用户可以从邮件设置页请求后台验证和真实测试邮件，并看到等待、成功或失败状态。
- Redis 暂时不可用时，验证请求仍保存在 PostgreSQL，随后可被 Worker 处理。
- 身份过期、Worker 停止或验证失败时，新任务创建失败关闭；已有任务继续监控。
- 实际发送每封邮件前仍由 Worker 执行实时身份校验。

### 非目标

- 不把 CLI、keyring 或 OAuth token 暴露给 Web。
- 不建立通用同步 RPC 框架。
- 不在本次改动中实现多租户邮箱或用户自定义 SMTP。
- 不在页面内实现 Agent Mail OAuth 登录；凭据失效时仍通过受控的服务器设备登录流程恢复。

## 3. 方案选择

采用“Worker 持久化验证证明”方案：

- Worker 是唯一可以调用 Agent Mail CLI 的进程。
- PostgreSQL 中的 `AgentMailConfig` 保存验证请求、租约、结果和证明时间。
- Web 创建任务时只读取数据库证明、固定地址和 Worker 心跳。
- 邮件设置页把测试动作写入 PostgreSQL，Worker 异步执行。

未采用以下方案：

- Redis 同步 RPC：会把任务创建可用性绑定到一次实时往返、Worker 空闲程度和 Redis 状态，增加超时与并发复杂度。
- Web 挂载凭据：扩大攻击面，并可能产生多个 keyring 使用者的并发问题。

## 4. 数据模型

在 `AgentMailConfig` 上新增以下字段，并通过 `0008` migration 部署：

- `verification_status`：`PENDING`、`VERIFIED`、`FAILED`，默认根据现有 `is_verified` 数据迁移。
- `verification_requested_at`：最近一次用户请求后台测试的时间，可为空。
- `verification_completed_at`：最近一次后台请求完成的时间，可为空。
- `verification_claim_token`：Worker 领取请求的 UUID，可为空。
- `verification_claim_expires_at`：请求租约到期时间，可为空。

保留并继续使用：

- `is_verified`：Web 快速判定和现有兼容接口。
- `verified_at`：最近一次成功确认固定主邮箱身份的时间。
- `last_error`：只保存既有的去敏错误码。

租约默认两分钟。Worker 崩溃后，过期租约可被同一 Worker 或替代 Worker 回收。完成更新必须携带匹配的 claim token，防止旧执行结果覆盖新请求。

## 5. 验证时效与可用性规则

- Worker 启动时执行一次不发邮件的 `+me` 身份验证。
- Worker 运行期间每 6 小时重新验证一次身份。
- 成功证明对 Web 创建任务有效 24 小时。
- 实际发送邮件时继续执行现有实时 `verify_agent_mail()`，不因已有证明而跳过。
- 创建任务要求同时满足：
  - `is_verified=True`；
  - `verification_status=VERIFIED`；
  - `verified_at` 不早于当前时间 24 小时；
  - 发件人为 `mijiatong@agent.qq.com`；
  - 收件人为 `850634546@qq.com`；
  - Worker 心跳处于现有健康阈值内。

时效常量通过 Django 设置提供，生产默认值固定为 6 小时复验、24 小时证明有效期；本次不增加用户界面配置，避免把运行安全参数与业务轮询设置混在一起。

## 6. 数据流

### Worker 启动和定期验证

1. Worker 启动，在进入正常循环前调用身份验证服务。
2. 成功时原子更新 `is_verified=True`、`verification_status=VERIFIED`、`verified_at`，并清空错误。
3. 失败时更新 `is_verified=False`、`verification_status=FAILED` 和去敏错误码，但 Worker 继续运行并检查电影任务。
4. 每轮扫描判断距上次成功验证是否达到 6 小时；到期后重复身份验证。

邮件认证故障不能停止电影监控。已有任务继续检查；需要发送的通知遵循现有重试和人工复核规则。

### 页面请求验证和测试邮件

1. 用户在邮件设置页点击“重新验证并发送测试邮件”。
2. Web 在事务中把 `verification_status` 设为 `PENDING`，写入新的 `verification_requested_at`，清空旧 claim，并在提交后尝试唤醒 Worker。
3. Redis 唤醒失败不会撤销数据库请求；Worker 的固定扫描仍会发现它。
4. Worker 使用带过期时间的数据库租约领取请求，在事务外调用 CLI、确认主邮箱并发送固定收件人的测试邮件。
5. Worker 使用 claim token 原子提交成功或失败结果。
6. 邮件设置页在 `PENDING` 时每两秒自动刷新；完成后显示验证时间或去敏错误及重新登录提示。

重复点击只更新单例请求的时间，不产生并行测试邮件。旧 claim 的结果不能完成较新的请求。

### 创建任务

1. Web 完成现有签名预览、日期和影院校验。
2. Web 调用纯数据库的邮件就绪检查，不导入或调用 Agent Mail subprocess 服务。
3. 就绪后创建任务并唤醒 Worker。
4. 证明过期时，Web 同时提交一次后台身份验证请求，并返回“邮箱验证已过期，正在重新验证”的可操作提示。
5. Worker 心跳异常时，Web 返回“后台 Worker 暂不可用”，不把邮箱状态错误地标记为未授权。

## 7. 错误处理与安全边界

- Web 镜像继续不包含 `agently-cli`，Compose Web 服务继续不挂载 `${AGENTLY_DATA_DIR}`。
- 所有 Agent Mail 异常在数据库和页面中仅使用现有安全错误码，不保存 CLI stdout/stderr、token 或完整邮件正文。
- 身份不匹配、认证失效或永久配置错误立即撤销 `is_verified`。
- 临时网络错误也使当前证明失效；下一次定期或用户请求可以恢复。
- Worker 启动验证失败不终止电影检查循环，避免邮箱故障造成监控盲区。
- Redis 只负责加速唤醒，不是验证请求的事实来源。
- PostgreSQL 行锁、claim token 和到期时间保证请求可以恢复且旧结果不能覆盖新结果。

## 8. 页面行为

- 邮件设置页显示固定发件人、固定收件人和 Worker 执行说明。
- `PENDING`：显示“等待后台 Worker 验证”，页面自动刷新。
- `VERIFIED`：显示最近验证成功时间，可以创建任务。
- `FAILED`：显示去敏错误和重新执行设备登录的提示。
- Worker 心跳异常时单独显示运行异常，不把它混同为邮箱授权失败。
- 任务创建失败返回表单内可操作提示，不清除用户的已签名预览选择。

## 9. 测试策略

### 单元与服务测试

- Web 环境没有 CLI 时，只要证明新鲜且 Worker 健康，任务创建成功。
- 证明过期、固定地址不符、未验证或 Worker 心跳过期时，任务创建被拒绝。
- 证明过期会持久化一次后台验证请求。
- 请求租约不可被两个 Worker 同时领取；过期租约可回收。
- 旧 claim 不能提交到更新后的请求。
- Worker 启动和 6 小时周期触发身份验证；未到期不重复调用 CLI。
- 身份验证失败只更新安全状态，Worker 仍执行电影检查。
- 测试邮件请求幂等，成功/失败状态正确。
- Redis 唤醒失败时数据库请求仍存在并可处理。

### Web 与 Compose 契约测试

- 邮件设置页覆盖等待、成功、失败和 Worker 异常状态。
- Web 镜像仍没有 CLI/keyring 依赖，Compose Web 无 Agent Mail 挂载。
- Worker 镜像和挂载保持 UID/GID 10001、0700 边界。

### 真实服务器验收

- 更新到审阅提交并执行 `0008` migration。
- Worker 启动验证成功，Web 在无 CLI 环境中创建真实猫眼任务。
- 重复检查只产生一条开票通知和一封邮件。
- Worker 重启后身份验证证明刷新，凭据仍然可用。
- Redis 中断期间后台验证请求不丢失。
- 最终继续完成三级频率、服务重启、备份恢复、端口与 16 项验收清单。

## 10. 部署与回滚

- 当前服务器保持在审阅提交 `522a3ee54ae6ed7b02625d009b3bf3a1e4be3f1c`，Web/PostgreSQL/Redis 健康，长期 Worker 停止且没有新建活动任务。
- 实现必须经过 TDD、完整回归和独立评审后，才通过既有校验和 Git bundle/镜像离线传输更新服务器。
- migration 仅增加可空状态字段并迁移现有验证状态，不删除数据。
- 应用回滚到旧版本前不得继续创建任务；数据库新增字段保留不会破坏旧代码读取。
- 若真实验收失败，停止 Worker 并保留当前数据库与验收证据，不恢复或覆盖用户迁移数据。

## 11. 验收条件

- Web 容器内不存在 Agent Mail CLI/keyring，仍能通过页面创建任务。
- Worker 是唯一执行身份校验和发送测试邮件的服务。
- 验证证明具有明确的 6 小时复验与 24 小时有效期。
- 认证失败、证明过期和 Worker 心跳异常均失败关闭并给出不同提示。
- Redis 中断不丢失验证请求。
- 真实服务器任务创建、猫眼检查、单次邮件、重启和备份链路全部通过。
