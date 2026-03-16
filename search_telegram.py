# search_telegram.py
# 依赖安装：pip install telethon

import asyncio
from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest

# ============================================================
# 填入你的凭证（从 https://my.telegram.org 获取）
# ============================================================
API_ID   = YOUR_API_ID          # 替换为你的 api_id（整数）
API_HASH = "YOUR_API_HASH"      # 替换为你的 api_hash（字符串）
PHONE    = "+YOUR_PHONE"        # 你的手机号，含国际区号，如 +8613800001234

# ============================================================
# 搜索关键词列表（可随意增删）
# ============================================================
KEYWORDS = [
    "openclaw",
    "open_claw",
    "claude",
    "claudebot",
]

# ============================================================
# 搜索模式：True=只搜Bot，False=只搜群组，None=Bot+群组都搜
# ============================================================
BOTS_ONLY   = False   # 是否只搜索 Bot
GROUPS_ONLY = False   # 是否只搜索群组/频道
# 两个都为 False → Bot + 群组全部收录


async def search_all():
    client = TelegramClient("session_search", API_ID, API_HASH)
    await client.start(phone=PHONE)
    print("✅ 登录成功\n")

    found_bots   = {}  # key: username
    found_groups = {}  # key: username or id

    for keyword in KEYWORDS:
        print(f"🔍 正在搜索关键词: {keyword}")
        try:
            result = await client(SearchRequest(q=keyword, limit=100))

            # ---- Bot ----
            if not GROUPS_ONLY:
                for user in result.users:
                    if getattr(user, "bot", False):
                        key = user.username or f"id_{user.id}"
                        if key not in found_bots:
                            found_bots[key] = {
                                "id":         user.id,
                                "username":   user.username or "",
                                "first_name": user.first_name or "",
                                "last_name":  user.last_name  or "",
                                "link":       f"https://t.me/{user.username}" if user.username else "（无公开链接）",
                            }
                            print(f"  🤖 Bot:   @{key}")

            # ---- 群组 / 频道 ----
            if not BOTS_ONLY:
                for chat in result.chats:
                    key = getattr(chat, "username", None) or f"id_{chat.id}"
                    if key not in found_groups:
                        ctype = "频道" if getattr(chat, "broadcast", False) else "群组"
                        found_groups[key] = {
                            "id":       chat.id,
                            "username": getattr(chat, "username", "") or "",
                            "title":    getattr(chat, "title", "") or "",
                            "type":     ctype,
                            "link":     f"https://t.me/{chat.username}" if getattr(chat, "username", None) else "（无公开链接）",
                        }
                        print(f"  💬 {ctype}: {chat.title}  @{key}")

        except Exception as e:
            print(f"  ⚠️  搜索 '{keyword}' 时出错: {e}")

        await asyncio.sleep(2)  # 避免触发频率限制

    # ============================================================
    # 终端输出
    # ============================================================
    print("\n" + "=" * 60)
    print(f"📋 汇总：Bot {len(found_bots)} 个 | 群组/频道 {len(found_groups)} 个（已去重）")
    print("=" * 60)

    if found_bots:
        print("\n── Bot 列表 ──")
        for i, (uname, info) in enumerate(found_bots.items(), 1):
            name = f"{info['first_name']} {info['last_name']}".strip()
            print(f"\n{i}. {name}")
            print(f"   用户名: @{info['username']}")
            print(f"   链接:   {info['link']}")
            print(f"   Bot ID: {info['id']}")

    if found_groups:
        print("\n── 群组 / 频道列表 ──")
        for i, (uname, info) in enumerate(found_groups.items(), 1):
            print(f"\n{i}. [{info['type']}] {info['title']}")
            print(f"   用户名: @{info['username']}" if info["username"] else "   用户名: （无）")
            print(f"   链接:   {info['link']}")
            print(f"   ID:     {info['id']}")

    # ============================================================
    # 保存到文件
    # ============================================================
    with open("found_results.txt", "w", encoding="utf-8") as f:
        f.write(f"Telegram 搜索结果 — Bot {len(found_bots)} 个 | 群组/频道 {len(found_groups)} 个\n")
        f.write("=" * 60 + "\n\n")

        if found_bots:
            f.write("── Bot 列表 ──\n\n")
            for i, (uname, info) in enumerate(found_bots.items(), 1):
                name = f"{info['first_name']} {info['last_name']}".strip()
                f.write(f"{i}. {name}\n")
                f.write(f"   @{info['username']}\n")
                f.write(f"   {info['link']}\n\n")

        if found_groups:
            f.write("── 群组 / 频道列表 ──\n\n")
            for i, (uname, info) in enumerate(found_groups.items(), 1):
                f.write(f"{i}. [{info['type']}] {info['title']}\n")
                f.write(f"   @{info['username']}\n" if info["username"] else "   （无公开用户名）\n")
                f.write(f"   {info['link']}\n\n")

    print(f"\n📁 结果已保存到 found_results.txt")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(search_all())
