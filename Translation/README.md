# 汉化流程

SweetHomeMaid（DMM 页游）的汉化工作区。原文放在 `Resources/`，成品放在 `Translation/Resources/`，
游戏里由根目录的油猴脚本 `SweetHomeMaidCN.user.js` 读取成品替换日文。

更新翻译有两条路线，二选一：

- **手动**：自己抓资源 → 跑 `batch_translate.py` → push 成品 → 游戏里油猴脚本生效。
- **Skill**：`$sweethomemaid-update` 一条龙（拿 config → 下资源 → 起模型 → 跑翻译 → 校验）。

最短路径（游戏刚更新、模型已经在跑）：

```powershell
python Translation/tools/bundle_map.py --bundle AdvStory missing   # 看缺什么
python Translation/tools/make_download_userscript.py                # 生成一次性下载脚本
# 浏览器里装脚本、点「下载缺失的资源」、zip 解压到 Resources/
python Translation/tools/batch_translate.py                         # 翻译（标题一起翻）
git add Translation/Resources && git commit -m "update translations" && git push
```

## 0. 目录速查

| 路径 | 内容 |
| --- | --- |
| `Resources/{Card,Event,Main,Ordinary,Battle,Garden}/` | 按 story id 整理好的原文（游戏更新后往这里加） |
| `Resources/config.*.json` | 各资源包的索引（AdvStory / Garden / main），可保留多份 |
| `Translation/Resources/<目录>/<story id>_cn.json` | 成品译文，油猴脚本读的就是这里 |
| `Translation/Resources/titles_cn.json` | 剧情列表标题表（脚本按 story id 取） |
| `Translation/tools/batch_translate.py` | 主翻译脚本（按 story id 批量翻） |
| `Translation/tools/bundle_map.py` | 读 config，列缺资源 / 互查路径 |
| `Translation/tools/titles.py` | 标题表维护（抽取 / 翻译 / 注入 / 回收） |
| `Translation/work/` | 中间产物和日志（`batch_status.jsonl`、`missing_advstory.tsv`、`titles.tsv` …） |
| `Translation/config.json` | Sakura 接口与批处理参数 |
| `Translation/glossary.txt` | 术语表 |

## 1. 准备模型

模型在 `D:\workspace\SakuraLLM`：

- `Sakura-Galtransl-14B-v3.8-Q4_K_M.gguf`（约 9G）
- `Sakura_Launcher_GUI_v1.2.0-beta.exe`，真正的服务是 `llama\llama-server.exe`

### 用 GUI

打开启动器 → 选模型和显卡 → 「高级设置」里调：

| 项 | 建议 | 说明 |
| --- | --- | --- |
| 上下文长度 `-c` | 8192 ～ 16384 | **总**上下文，每个工作线程分到 `c / np` |
| 并发数量 `-np` | 2 ～ 4 | 要和 `Translation/config.json` 的 `workers` 相等 |
| GPU 层数 `-ngl` | 200（全上卡）或 28～33（保守） | 12G 卡 + 桌面占用时，全量上卡比较贴顶 |
| Flash Attention `-fa` | 开 | |
| `--no-mmap` | 开 | |

### 用命令行（可复制）

```powershell
Start-Process cmd.exe -ArgumentList '/k', 'title Sakura LLM Server && D:\workspace\SakuraLLM\llama\llama-server.exe -m D:\workspace\SakuraLLM\Sakura-Galtransl-14B-v3.8-Q4_K_M.gguf --host 127.0.0.1 --port 8080 -c 8192 -np 4 -ngl 200 -fa --no-mmap -n 512' -WorkingDirectory 'D:\workspace\SakuraLLM\llama' -WindowStyle Normal
```

参数取舍（重要）：

- `-c` 是**总**上下文，每槽 = `-c / -np`。每次请求都带整份术语表（约 4.5KB，1.5～2k token），
  再加正文、历史、最多 512 token 输出，**每槽至少 4096** 才稳；每槽 2048 时超出部分会从开头截断，
  最先被丢的就是术语表。
- 12G 卡（RTX 3080）常用组合：
  - 稳：`-np 2 -c 8192 -ngl 33`
  - 兼顾并发：`-np 4 -c 16384 -ngl 28`
  - 最快（实测）：`-np 4 -c 8192 -ngl 200`，每槽 2048，术语表容易顶满
- 改完 `-np` 记得同步改 `Translation/config.json` 的 `workers`。

就绪检查：

```powershell
curl.exe -s http://127.0.0.1:8080/v1/models
```

返回 200 且有模型名就绪。显存不足时可把 `-ngl` 降到 26、24 再试。

## 2. 手动更新（油猴脚本路线）

### 2.1 拿最新的 config

游戏更新后 `assets/AdvStory` 会换一份 `config.<hash>.json`。两种拿法：

- 浏览器 Network 里找 `assets/AdvStory/config.<hash>.json`，另存到 `Resources/`；
- 或者用 skill 里的脚本自动顺链抓：

```powershell
python C:\Users\Faust\.codex\skills\sweethomemaid-update\scripts\fetch_config.py --resources D:\workspace\SweetHomeMaidTranslate\Resources
```

它会打印 `index.html → index.<hash>.js → application.<hash>.js → settings.<hash>.json` 的链路、
当前 `AdvStory` 版本号，并和上一份 config 对比新增 / 移除的资源。

`Resources/` 里同时留多份 config 没关系：`bundle_map.py` 会自动挑资源最多的那份，并打印用了哪份；
想固定某份就加 `--config Resources/config.<ver>.json`。

### 2.2 列出缺哪些资源

```powershell
python Translation/tools/bundle_map.py --bundle AdvStory missing
```

会打印「缺 N 个」并按目录分布，同时写出 `Translation/work/missing_advstory.tsv`。

### 2.3 下载缺的资源

**方式 A：一次性油猴下载脚本（在游戏页面里跑）**

```powershell
python Translation/tools/make_download_userscript.py
```

生成 `Translation/tools/download_missing.user.js` 后：

1. Tampermonkey 里临时装这个脚本；
2. 浏览器里正常打开游戏（保持登录）；
3. 点 Tampermonkey 菜单「下载缺失的资源」；
4. 左下角有进度（点一下可取消），跑完自动下载 `sweethomemaid-missing-<日期>.zip`；
5. 把 zip 解压到 `Resources/`（包内结构就是 `Card/storyXXXX.json`）；
6. 再跑一次 `missing` 确认「缺 0 个」，然后删掉这个临时脚本。

**方式 B：直接下载（不用油猴）**

资源地址是 `r/<token>/assets/AdvStory/import/...`，不需要登录态，可以直接抓：

```powershell
python C:\Users\Faust\.codex\skills\sweethomemaid-update\scripts\fetch_missing.py --root D:\workspace\SweetHomeMaidTranslate
```

它会校验 JSON 结构和 story id，已存在的跳过。

### 2.4 翻译

```powershell
python Translation/tools/batch_translate.py --list                # 看还有多少没翻
python Translation/tools/batch_translate.py --limit 3             # 先试几篇
python Translation/tools/batch_translate.py                       # 全量，随时 Ctrl-C
python Translation/tools/batch_translate.py --only Card           # 只翻 Card/
python Translation/tools/batch_translate.py --retry-failed        # 重翻上次有失败块的
python Translation/tools/batch_translate.py --redo story1110501   # 重翻某篇
python Translation/tools/batch_translate.py --redo-untranslated   # 重翻成品里还残留日文的篇
```

要点：

- 成品 `Translation/Resources/<目录>/<story id>_cn.json` 不存在就算没翻。
- 一篇一个文件，翻完先写 `.tmp` 再原子改名，**Ctrl-C 停了重跑就接着翻**。
- 篇内并发看 `config.json` 的 `workers`，必须和 Sakura 的 `-np` 对齐。
- 每篇进度记在 `Translation/work/batch_status.jsonl`（块数 / 失败数 / 耗时）。
- 同一段 `@ShowCastMessage` 的两行被合成一行时会直接接受；只有译文比原文第一行长很多
  （`merge_split_ratio`，默认 1.5 倍）才自动从标点处拆回两行。
- 翻正文时会**顺手翻开头的标题**，写回成品注释 + `titles.tsv` + `titles_cn.json`（见第 4 节）。

### 2.5 上线（让油猴脚本读到）

油猴脚本从 GitHub 读成品，地址在脚本顶部 `CONFIG.BASES`：

1. `https://raw.githubusercontent.com/Mephistor666/SweetHomeMaidTranslate/refs/heads/main/Translation/Resources/`
2. `https://cdn.jsdelivr.net/gh/Mephistor666/SweetHomeMaidTranslate@main/Translation/Resources/`

所以只要把 `Translation/Resources/` 下的新文件 **push 到 main**，不用改脚本：

- 每篇剧情：`Translation/Resources/<目录>/<story id>_cn.json`
- 剧情列表标题：`Translation/Resources/titles_cn.json`（新增标题必须一起 push）
- 主页气泡：`Translation/Resources/Garden/MainGarden00XConfig_cn.json`

推完进游戏，`Ctrl+F5` 强刷（GitHub raw 有几分钟缓存）。

## 3. 油猴脚本怎么取译文

脚本位置：根目录 `SweetHomeMaidCN.user.js`（version 1.0）。只接管 `XMLHttpRequest`。

### 3.1 剧情资源

1. 游戏正常下载原文 `.json`；
2. 脚本从 `[5][0][1]` 读出 story id；
3. 按目录规则拼 `…/<story id>_cn.json` 去取；
4. 取到就替换响应，404 原样放行（同一篇只试一次）。

| story id 形态 | 目录 |
| --- | --- |
| `storyEV…` | `Event/` |
| `storyBT…` | `Battle/` |
| `story0…` | `Main/` |
| `story1…` | `Card/` |
| `story2…` | `Main/` 和 `Card/`（两个都试） |
| `default_…` / `tutorial…` | `Ordinary/` |
| `MainGarden00XConfig` | `Garden/` |

### 3.2 剧情列表的标题

标题不在剧情文件里，是接口 `/scenario/{card,event,main,mini-event}/scenarios/<id>` 下发的，
响应被游戏的 `EXSerializer` 加密成 JAES 容器：

```
"JAES" | IV(16 字节) | Base64( AES-256-CBC-PKCS7(JSON) )
```

密钥写死在游戏主包 `crypto-aes.ts` 里（客户端要自己解密）。脚本拿到响应后解密、按
`scenario_rid` 末尾的 story id 从 `titles_cn.json` 换掉 `scenario_title`，再原样加密回去。

### 3.3 其它行为

- 每替换一篇，右下角弹 `汉化已生效：<id>`（同一篇只弹一次，无背景色）。
- 控制台日志：`[汉化] 替换 <id>`、`[汉化] 没有译文，放行原文：<id>`、`[汉化] 替换剧情标题 × N`。
- R18 剧情的白屏闪烁（`@FlashScreen`）默认**不动**；菜单里的「去除闪光弹」开关打开后可选
  「移除全部闪光 / 移除短闪 / 移除长闪」，删除时会连带删掉紧跟的 `@WaitMethod`。
- 地址连续失败 3 次会本次停用（刷新重来），游戏照常跑日文。

## 4. 标题表

`Translation/work/titles.tsv` 三列：`story id`、日文、中文；`titles_cn.json` 是给脚本读的成品。

```powershell
python Translation/tools/titles.py stats             # 看还差多少
python Translation/tools/titles.py extract           # 扫剧情注释，补新条目（不丢已翻）
python Translation/tools/titles.py translate         # 用 Sakura 翻空着的中文列
python Translation/tools/titles.py build             # 由 tsv 重建 titles_cn.json
python Translation/tools/titles.py import a.tsv      # 并入 <story id>\t<中文>
python Translation/tools/titles.py inject            # 把标题写进已翻好的成品剧本
python Translation/tools/titles.py sync --overwrite  # 从成品回收标题（强杀后救急）
```

注意：

- `batch_translate.py` 现在每翻出一条标题就立即落盘，强杀也不会丢。
- 如果更早被强杀过，跑 `sync --overwrite` 从成品回收；它兼容标题里自带「」的情况。
- 标题只在列表里显示；剧情文件里那行 `タイトル「…」` 只是注释，不影响游戏内文本。
- 只补标题、不重新翻译正文：`python Translation/tools/batch_translate.py --fill-titles`

## 5. 用 skill 一键更新

Skill：`sweethomemaid-update`，位置 `C:\Users\Faust\.codex\skills\sweethomemaid-update`。

调用方式：新对话里说「游戏更新了，帮我更新翻译」，或显式 `$sweethomemaid-update`。

它会按顺序：

1. 跑 `scripts\fetch_config.py`：抓最新 AdvStory config，存 `Resources/`，打印新增/移除清单；
2. 跑 `bundle_map.py --bundle AdvStory missing`，写 `missing_advstory.tsv`；
3. 跑 `scripts\fetch_missing.py` 直接下载缺的资源（校验 JSON 和 id）；
4. 用**可见控制台窗口**启动 `llama-server.exe`（`-c / -np / -ngl` 按显存取舍，见第 1 节）；
5. 用**可见控制台窗口**跑 `batch_translate.py`；
6. 收尾：确认「缺 0 个」、检查失败篇目、必要时 `--retry-failed`。

前提：项目在 `D:\workspace\SweetHomeMaidTranslate`，模型在 `D:\workspace\SakuraLLM`，
机器上装了 Python 3。token 变了的话，skill 文档里写了从内置浏览器 iframe 取新 token 的办法。

## 6. 其它工具

- `tools/jaes.py`：解密 / 加密游戏的 JAES 资源
  （`decrypt <文件>`、`encrypt <json>`、`selftest`），调试标题接口时用。
- `tools/garden_bubbles.py`：主页互动气泡，`TABLE` 手工译，`--check` 查漏。
- `tools/fix_dialogue_wrappers.py`：清掉 Sakura 复述出来的「说话人『台词』」外壳。
- `tools/bundle_map.py`：除了 `missing`，还有 `info` / `map` / `check` / `export` / `manifest`。
- `tools/sakura_translate.py`：最早的 uuid 资源流程（`extract` / `translate` / `check` / `inject`），
  现在一般用 `batch_translate.py`，它只在处理没整理成 story id 的资源时才需要。

## 7. 常见问题

| 现象 | 处理 |
| --- | --- |
| 游戏里还是日文 | 先 `Ctrl+F5`；确认 `titles_cn.json` / `_cn.json` 已 push；看控制台是否有 `[汉化] 替换` |
| 某些标题缺失 | `titles.py sync --overwrite` 从成品回收，再 push `titles_cn.json` |
| 有失败块 | `batch_translate.py --retry-failed`，或 `--redo <story id>` |
| 成品里还残留日文 | `batch_translate.py --redo-untranslated` |
| 术语/人称乱 | 检查每槽上下文是否 ≥4096；`-c / -np` 太小会截断术语表 |
| 服务端很慢 | `-ngl` 太低或太高都可能掉速；先看显存，再调 `-ngl` |
| `bundle_map` 挑错 config | 加 `--config Resources/config.<ver>.json` 明确指定 |

## 8. 版权

- 全部译文为**机器翻译**，仅供学习交流，请勿商用。
- Sakura 全系模型为 CC BY-NC-SA 4.0，禁止商用；发布时按作者要求在显著位置标注「机翻」并写明模型版本。
