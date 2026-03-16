# tg-search

> 一次批量搜索 Telegram 中的 **Bot、群组、频道** — 支持多关键词，自动去重，结果保存到本地文件。

---

## 功能介绍

`search_telegram.py` 是一个基于 [Telethon](https://github.com/LonamiWebs/Telethon) 的 Telegram 批量搜索脚本。

你只需要填入一组关键词，脚本会自动：

- 对每个关键词调用 Telegram 官方搜索接口
- 收集所有匹配的 **Bot**、**群组**、**频道**
- 跨关键词自动去重（同一个 Bot/群组不会重复出现）
- 在终端实时输出搜索进度
- 将完整结果保存到 `found_results.txt`

**适用场景：**

- 快速摸底某个领域/品牌在 Telegram 上的 Bot 和社群分布
- 竞品调研：一次性找出所有相关 Bot 和群组
- 关键词监控：了解某个词汇在 Telegram 生态中的覆盖范围
- 批量整理公开社群列表

---

## 环境要求

- Python 3.8+
- Telegram 账号（用于登录并调用搜索接口）

---

## 安装依赖

```bash
pip install telethon
```

---

## 获取 API 凭证

1. 打开 [https://my.telegram.org](https://my.telegram.org)
2. 登录你的 Telegram 账号
3. 进入 **API development tools**
4. 创建一个应用（名称随意），获得：
   - `api_id`（整数）
   - `api_hash`（字符串）

---

## 配置脚本

打开 `search_telegram.py`，修改顶部的三个配置项：

```python
API_ID   = YOUR_API_ID          # 替换为你的 api_id（整数）
API_HASH = "YOUR_API_HASH"      # 替换为你的 api_hash（字符串）
PHONE    = "+YOUR_PHONE"        # 你的手机号，含国际区号，如 +8613800001234
```

然后修改关键词列表：

```python
KEYWORDS = [
    "openclaw",
    "claude",
    "chatgpt",
    "你的关键词",
]
```

**搜索模式（可选）：**

```python
BOTS_ONLY   = False   # True = 只搜 Bot
GROUPS_ONLY = False   # True = 只搜群组/频道
# 两个都为 False → Bot + 群组 + 频道全部收录
```

---

## 运行

```bash
python search_telegram.py
```

首次运行会要求输入手机验证码（Telegram 登录验证），验证后生成本地 `session_search.session` 文件，之后无需重复验证。

---

## 输出示例

终端：

```
✅ 登录成功

🔍 正在搜索关键词: openclaw
  🤖 Bot:   @OpenClawBot
  💬 群组:  OpenClaw Community  @openclaw_chat

🔍 正在搜索关键词: claude
  🤖 Bot:   @ClaudeAIBot
  💬 频道:  Claude Official  @claude_official

============================================================
📋 汇总：Bot 2 个 | 群组/频道 2 个（已去重）
============================================================

── Bot 列表 ──
1. OpenClaw Bot
   用户名: @OpenClawBot
   链接:   https://t.me/OpenClawBot
   Bot ID: 123456789

── 群组 / 频道列表 ──
1. [群组] OpenClaw Community
   用户名: @openclaw_chat
   链接:   https://t.me/openclaw_chat
   ID:     -1001234567890

📁 结果已保存到 found_results.txt
```

`found_results.txt` 为纯文本格式，可直接分享或导入其他工具。

---

## 注意事项

- 每个关键词搜索之间有 **2 秒延迟**，避免触发 Telegram 频率限制
- 搜索结果来自 Telegram 官方接口，限制每次最多返回 100 条（`limit=100`）
- `session_search.session` 文件包含登录状态，**请勿上传到公开仓库**
- API 凭证（`api_id` / `api_hash`）属于私密信息，**请勿硬编码后提交代码**

---

## .gitignore 建议

```
*.session
*.session-journal
found_results.txt
found_bots.txt
```

---

## License

MIT
