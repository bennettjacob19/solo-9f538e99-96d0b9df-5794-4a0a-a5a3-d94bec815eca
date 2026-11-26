# 大页连续内存整理演练（Flask + SQLite）

一个可交互的内存整理/页缓存回源演练台：用 SQLite 表模拟 16 个 4 KiB 内存槽和后备盘，
演示连续 4 槽目标区、干净页释放、脏页搬迁/写回、整理暂停期间虚拟机读写、缺页重缓存、
直读直写、写回失败后的整批事务回滚，以及四槽腾齐后立即分配给新虚拟机。

## 启动

### Docker Compose

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在 Compose 命名卷 `memory-drill-data` 中，数据库文件为容器内 `/data/sim.db`。
宿主机端口可用 `PORT=18000 docker compose up --build` 覆盖。

### 本地 Python

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
# 默认 http://127.0.0.1:8000；也可：
DB_PATH=/tmp/memory-drill.db PORT=8000 python app.py
```

运行自动化测试：

```bash
python -m unittest discover -s tests -v
```

测试只依赖标准库即可运行核心测试；安装 Flask 后会额外运行 HTTP/模板测试。

## 初始样例

| 槽 | 归属 | 磁盘页 | 状态 |
|---:|---|---:|---|
| 0 | 原 VM 缓存 | P00 | 脏 |
| 1 | 原 VM 缓存 | P01 | 干净 |
| 2 | 原 VM 缓存 | P02 | 脏 |
| 3 | 空 | — | 目标区内空槽 |
| 4–14 | 原 VM 干净缓存 | P03–P13 | 干净 |
| 15 | 空 | — | 默认目标 0–3 的区外唯一空槽 |

后备盘初始化 P00–P31；P14 起没有缓存槽，可用于演示缺页。

目标首槽只能是 `0/4/8/12`，即编号必须为 4 的倍数。

## 推荐演练：暂停期改写 + 第二次写回失败

1. 点击“重置样例”。
2. 选择目标 `0 — 3 槽`，搬页限额 `1`，写回限额 `2`，勾选“模拟第 `2` 次写回失败”，
   然后点击“选定本批动作并暂停整理”。
   - 选定时只有两个脏页，因此初定方案是搬 1、写回 1、释放干净页 1。
3. 在“原虚拟机按磁盘页号读写”中：
   - 向 P01 写入 `PAUSE-DIRTY-P1`：干净页在暂停期间变脏；
   - 向 P00 写入 `PAUSE-LATEST-P0`：更新待搬页的最新内容。
4. 点击“继续：重算并执行本批”。
   - 继续时会重新规划为：P00 搬到槽 15，P01、P02 写回；
   - 第 2 次写回（P02）触发模拟错误；
   - 整个批次在一个 SQLite `BEGIN IMMEDIATE` 事务中回滚：
     槽 0/1/2 仍是原 VM 页，槽 15 仍空，事务内的第一次写回也撤销；
   - 但暂停期间已经独立提交的 P00/P01 改写仍保留。
5. 点击“清除故障后继续”。
   - 系统再次按当前三个脏页和一个区外空槽规划；
   - 实际搬页 1、写回 2、干净释放 0；
   - 槽 0–3 立即变为 `new-vm / hugepage`，原 VM 的 `page_id` 映射被清空。
6. 再让原 VM 读 P01：
   - P01 的缓存槽已分配给新 VM，槽 15 又被 P00 占用，因此结果是 `direct_disk`；
   - 读 P00 会在槽 15 命中，并看到暂停期间写入的最新内容。

缺页路径可单独验证：重置并创建目标 0 的批次后，读 P14 会重缓存到区外槽 15；
再读/写 P15 时区外已无空槽，会直接读/写后备盘且不占用受保护的目标槽。

## HTTP/JSON API

```bash
curl -s http://127.0.0.1:8000/api/state | jq

curl -s -X POST http://127.0.0.1:8000/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"target_start":0,"move_quota":1,"writeback_quota":2,"fail_on_writeback_index":2}'

curl -s -X POST http://127.0.0.1:8000/api/guest/write \
  -H 'Content-Type: application/json' \
  -d '{"page_id":1,"content":"pause edit"}'

curl -s -X POST http://127.0.0.1:8000/api/batches/1/resume \
  -H 'Content-Type: application/json' -d '{}'

curl -s -X POST http://127.0.0.1:8000/api/batches/1/resume \
  -H 'Content-Type: application/json' -d '{"clear_fault":true}'

curl -s -X POST http://127.0.0.1:8000/api/guest/read \
  -H 'Content-Type: application/json' -d '{"page_id":14}'

curl -s -X POST http://127.0.0.1:8000/api/reset
```

HTML 表单路由为 `/ui/batches`、`/ui/batch/resume`、`/ui/batch/fault`、
`/ui/guest`、`/ui/cancel`、`/ui/reset`。

## 实现说明

### SQLite 表

- `slots`：16 个槽，字段包括 `owner`（`old-vm` / `new-vm` / `NULL`）、
  `role`、`page_id`、`dirty`、`content`。
- `disk_pages`：后备盘页号、内容、写盘次数。
- `batches`：每批的目标、限额、故障序号、状态、初定方案、继续时执行方案和实际结果。
- `events`：页面事件日志。
- `kv`：当前批次、活动目标区、最近一次新 VM 分配。
- 部分唯一索引保证同一个磁盘页在原 VM 下最多只有一个缓存槽。

### 限额内最大化腾槽

设目标区内：

- `F` 为原本已空槽数；
- `C` 为干净页数，均可直接释放，不占用搬页/写回限额；
- `D` 为脏页数；
- `E` 为目标区外当前空槽数；
- `M/W` 为搬页/写回限额。

系统先按槽号选择最多 `min(M,E,D)` 个脏页搬到区外空槽，再对剩余脏页写回最多
`min(W,D-moves)` 个。腾出的槽数为：

```text
F + C + moves + writebacks
```

因此在给定限额和区外空槽数下达到最大可释放数。当前平局策略是“能搬则先搬”，
随后写回；执行时不使用旧方案快照，而是重新读取已提交的脏位、内容和空槽。

### 暂停、读写与事务边界

- “选定本批动作并暂停”只生成并保存初定方案，不执行整理。
- 暂停期间的 VM 读/写是独立 SQLite 语句/事务，写入成功后立即落库。
- 继续时调用 `compute_plan()` 重算；搬移和写回都读取槽内当前最新文本。
- 真正执行批次时开启 `BEGIN IMMEDIATE`：干净释放、搬迁、盘页更新、源槽清理、
  四槽后的新 VM 分配和批次结果在同一事务中。
- 若指定序号的写回触发 `WritebackSimulated`，执行 `ROLLBACK`。
  因此本批中已经做过的释放、搬迁和第一次写回全部撤销；
  暂停期间在另一个事务中成功提交的 VM 改写不会回滚。
- 回滚后批次状态为 `rolled_back`，可保留故障重试以复现，或清除故障后继续。

### VM 地址翻译

按磁盘页号查找 `owner='old-vm'` 的槽：

1. 命中：读/写当前槽；写入置脏。
2. 未命中且目标区外有空槽：读取后备盘并在区外槽重建干净缓存；写缺页则建脏缓存。
3. 未命中且无区外空槽：读直接返回盘内容，写直接 upsert 后备盘。
4. 四槽腾齐后，同事务把四槽改成 `owner='new-vm', role='hugepage'` 并清空 `page_id`。
   原 VM 按页号不可能再命中这些槽，之后只能搬迁命中、区外重缓存或直读直写盘。

## 实测记录

在本机用临时依赖目录安装 Flask 3.1.3 后执行：

```text
Ran 11 tests in 0.823s

OK
```

覆盖：

- 初始 16 槽/样例脏干净布局；
- 首槽非 4 倍数返回校验错误；
- 暂停改写影响重算方案和搬迁内容；
- 第二次写回失败后整批回滚、暂停改写保留、清故障后成功；
- 缺页先使用区外槽、区外耗尽后直读直写；
- 写回不占区外槽时，原 VM 可在槽 15 重新缺页缓存；
- Flask 首页、JSON API 和 HTTP 409 回滚响应。

还实际启动 `python app.py`（临时 `DB_PATH`，端口 8011）并用 `curl` 试跑：

```text
创建批次：status=paused, outside=[15], clean=1, moves=1, wbs=1
暂停写入：P1=hit_dirty(slot 1), P0=hit_dirty(slot 0)
继续失败：HTTP 409，“第 2 次写回（P02，槽 2）模拟失败；本批整理已全部撤销……”
回滚检查：slot0/1/2 仍为 old-vm 且保留 PAUSE-LATEST/PAUSE-EDIT，slot15 为空；
          P0/P1/P2 的 write_count 均为 0，证明事务内第一次写回也已撤销。
清故障重试：moves=1, writebacks=2, allocated=true；
          slot0–3=new-vm/hugepage，slot15=old-vm/P0/dirty 且含最新内容。
分配后读 P1：mode=direct_disk, slot_no=null
分配后读 P0：mode=hit, slot_no=15, content=PAUSE-LATEST-P0-20261126
缺页 P14：miss_recached -> slot15
再访问 P15：read=direct_disk，write=direct_disk_write
target_start=7：HTTP 400，“首槽编号必须是 4 的倍数……”
```

HTML 表单流程也用带 Cookie 的 curl 跟随重定向验证：暂停页、暂停改写展示、
失败回滚 flash、清除故障后“四槽腾齐，已立即分配给新虚拟机”均通过。

`compose.yaml` 已用 PyYAML 解析校验；当前执行环境没有 `docker`/`docker compose`
可执行程序和守护进程，因此没有在这里实际执行 `docker compose up --build`。

## 已知简化和缺项

- 这是状态/事务演练，不是 Linux 内核、KVM 或真实 2 MiB 大页分配；没有真实 mmap、
  DMA、页表项或进程地址空间。
- “4 KiB 页”用短文本内容和 4096 字节写入上限模拟，没有把每页填充到完整 4096 字节。
- “暂停整理”是批次执行前的持久状态；批次内部仍由一个同步 SQLite 事务原子完成，
  没有模拟每条搬页/写回操作之间的任意并发交错。
- 写回失败是指定序号的应用层注入；没有模拟真实磁盘部分写、掉电、坏块或崩溃恢复。
- 为了演练清晰，没有加入登录鉴权、多租户权限、CSRF 防护或生产级 WSGI 调优；
  Compose/Gunicorn 适合本机或可信演练网络。
- 没有浏览器端 E2E 截图测试；当前验证由单元/HTTP 测试和 curl 表单流程组成。
- 当前平局策略固定为先搬移后写回；尚未提供“优先写回以保留区外槽”等可选策略。
