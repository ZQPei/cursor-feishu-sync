#!/usr/bin/env python3
"""
Cursor Dashboard → 飞书签名 动态同步脚本

直接调用 Cursor API 获取使用数据，
通过 l.garyyang.work 的 slot API 动态更新飞书个性签名。

用法:
    python cursor_feishu_sync.py --setup           # 配置向导
    python cursor_feishu_sync.py --dry-run         # 预览（不写飞书）
    python cursor_feishu_sync.py --once            # 单次同步
    python cursor_feishu_sync.py --loop -i 60      # 每 60 分钟同步

也支持通过环境变量传入配置（用于 CI/CD）:
    CURSOR_COOKIE, LARK_CREDENTIAL, LARK_SLOT_ID
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import platform
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, quote

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / ".state"
CONFIG_FILE = SCRIPT_DIR / "config.json"
SYNC_STATE_FILE = STATE_DIR / "sync_state.json"

CURSOR_BASE = "https://cursor.com"
LARK_SLOT_API = "https://l.garyyang.work/api/slot/update"


def _read_cursor_ide_cookie() -> Optional[str]:
    """从 Cursor IDE 本地数据库读取 accessToken 并拼成 web cookie 格式。
    Cursor 每次启动时自动刷新 token，所以只要你在用 Cursor，token 就不会过期。"""
    if platform.system() == "Darwin":
        db_path = Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    elif platform.system() == "Linux":
        db_path = Path.home() / ".config/Cursor/User/globalStorage/state.vscdb"
    else:
        db_path = Path.home() / "AppData/Roaming/Cursor/User/globalStorage/state.vscdb"

    if not db_path.exists():
        return None

    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'"
        ).fetchone()
        conn.close()
        if not row:
            return None

        token = row[0]
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.b64decode(payload))
        user_id = data["sub"].split("|")[1]

        cookie = f"{quote(user_id, safe='')}%3A%3A{token}"
        exp_ts = data.get("exp", 0)
        if exp_ts:
            remaining = exp_ts - time.time()
            if remaining < 0:
                log.warning("Cursor IDE token 已过期，请重新打开 Cursor")
                return None
            log.info("  从 Cursor IDE 读取 token (剩余 %d 天)", remaining / 86400)
        return cookie
    except Exception as e:
        log.debug("读取 Cursor IDE token 失败: %s", e)
        return None


def _compact(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    if n >= 1_000:
        return f"{n:,}"
    return str(n)


@dataclass
class CursorStats:
    agent_lines: int
    rank: int
    total_users: int
    display_name: str
    requests_used: int = 0
    requests_limit: int = 0
    tokens: int = 0

    def format_signature(self) -> str:
        """精简签名：本月已蹬17.2K行 #377 · 362req · 277M tok"""
        parts = [f"本月已蹬{_compact(self.agent_lines)}行 #{self.rank}"]
        if self.requests_limit > 0:
            parts.append(f"{self.requests_used}/{self.requests_limit}req")
        if self.tokens > 0:
            parts.append(f"{_compact(self.tokens)} tok")
        return " · ".join(parts)


@dataclass
class Config:
    cursor_cookie: str = ""
    lark_credential: str = ""
    lark_slot_id: str = ""

    @staticmethod
    def load() -> "Config":
        """加载优先级: 环境变量 > config.json > Cursor IDE 本地 token。"""
        env_cookie = os.environ.get("CURSOR_COOKIE", "")
        env_cred = os.environ.get("LARK_CREDENTIAL", "")
        env_slot = os.environ.get("LARK_SLOT_ID", "")

        if env_cookie:
            return Config(
                cursor_cookie=env_cookie,
                lark_credential=env_cred,
                lark_slot_id=env_slot,
            )

        file_cookie = ""
        file_cred = ""
        file_slot = ""
        if CONFIG_FILE.exists():
            d = json.loads(CONFIG_FILE.read_text())
            file_cookie = d.get("cursor_cookie", "")
            file_cred = d.get("lark_credential", "")
            file_slot = d.get("lark_slot_id", "")

        cookie = file_cookie or _read_cursor_ide_cookie() or ""

        return Config(
            cursor_cookie=cookie,
            lark_credential=file_cred,
            lark_slot_id=file_slot,
        )

    def save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({
            "cursor_cookie": self.cursor_cookie,
            "lark_credential": self.lark_credential,
            "lark_slot_id": self.lark_slot_id,
        }, indent=2, ensure_ascii=False))

    @property
    def has_cursor(self) -> bool:
        return bool(self.cursor_cookie)

    @property
    def has_lark(self) -> bool:
        return bool(self.lark_credential and self.lark_slot_id)


def _load_sync_state() -> dict:
    if SYNC_STATE_FILE.exists():
        return json.loads(SYNC_STATE_FILE.read_text())
    return {}


def _save_sync_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SYNC_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def _cursor_headers(cookie: str) -> dict:
    return {
        "Cookie": f"WorkosCursorSessionToken={cookie}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/130.0.0.0",
        "Accept": "application/json",
        "Referer": f"{CURSOR_BASE}/cn/dashboard/usage",
    }


def _extract_user_id(cookie: str) -> str:
    decoded = unquote(cookie)
    return decoded.split("::")[0] if "::" in decoded else ""


# ---------------------------------------------------------------------------
# Cursor API calls
# ---------------------------------------------------------------------------

async def _get_team_info(client: httpx.AsyncClient, cookie: str) -> Optional[dict]:
    resp = await client.get(
        f"{CURSOR_BASE}/api/dashboard/teams",
        headers=_cursor_headers(cookie),
    )
    if resp.status_code == 401:
        log.error("Cookie 已过期，请重新获取 WorkosCursorSessionToken")
        return None
    resp.raise_for_status()
    teams = resp.json().get("teams", [])
    if not teams:
        log.error("未找到团队信息")
        return None
    team = teams[0]
    return {
        "team_id": team["id"],
        "billing_start_ms": int(team.get("billingCycleStart", "0")),
    }


async def _get_leaderboard(
    client: httpx.AsyncClient, cookie: str,
    team_id: int, start_date: str, end_date: str,
) -> Optional[dict]:
    resp = await client.get(
        f"{CURSOR_BASE}/api/v2/analytics/team/leaderboard",
        headers=_cursor_headers(cookie),
        params={
            "startDate": start_date,
            "endDate": end_date,
            "pageSize": "10",
            "teamId": str(team_id),
            "leaderboardSortBy": "composer_lines",
        },
    )
    if resp.status_code == 401:
        log.error("Cookie 已过期")
        return None
    resp.raise_for_status()
    return resp.json()


async def _get_usage(
    client: httpx.AsyncClient, cookie: str, user_id: str,
) -> dict:
    """返回 {requests_used, requests_limit, tokens}。"""
    try:
        resp = await client.get(
            f"{CURSOR_BASE}/api/usage",
            headers=_cursor_headers(cookie),
            params={"user": user_id},
        )
        resp.raise_for_status()
        data = resp.json()
        gpt4 = data.get("gpt-4", {})
        return {
            "requests_used": gpt4.get("numRequests", 0),
            "requests_limit": gpt4.get("maxRequestUsage", 0),
            "tokens": gpt4.get("numTokens", 0),
        }
    except Exception as e:
        log.warning("获取用量失败: %s", e)
        return {"requests_used": 0, "requests_limit": 0, "tokens": 0}


async def fetch_cursor_stats(cookie: str) -> Optional[CursorStats]:
    log.info("正在获取 Cursor 数据...")
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        team_info = await _get_team_info(client, cookie)
        if not team_info:
            return None

        team_id = team_info["team_id"]
        billing_start_ms = team_info["billing_start_ms"]

        if billing_start_ms > 0:
            start_dt = datetime.fromtimestamp(billing_start_ms / 1000, tz=timezone.utc)
            start_date = start_dt.strftime("%Y-%m-%d")
        else:
            start_date = date.today().replace(day=1).isoformat()

        end_date = date.today().isoformat()
        user_id = _extract_user_id(cookie)
        log.info("  周期: %s ~ %s", start_date, end_date)

        lb_data, usage = await asyncio.gather(
            _get_leaderboard(client, cookie, team_id, start_date, end_date),
            _get_usage(client, cookie, user_id),
        )
        if not lb_data:
            return None

        lb = lb_data.get("composer_leaderboard", {})
        entries = lb.get("data", [])
        if not entries:
            log.error("leaderboard 数据为空")
            return None

        me = entries[-1]
        stats = CursorStats(
            agent_lines=me.get("total_composer_lines_accepted", 0),
            rank=me.get("rank", 0),
            total_users=lb.get("total_users", 0),
            display_name=me.get("display_name", me.get("email", "unknown")),
            requests_used=usage["requests_used"],
            requests_limit=usage["requests_limit"],
            tokens=usage["tokens"],
        )
        log.info("  %s | Lines %s #%d/%d | %d/%d req | %s tok",
                 stats.display_name,
                 f"{stats.agent_lines:,}", stats.rank, stats.total_users,
                 stats.requests_used, stats.requests_limit,
                 _compact(stats.tokens))
        return stats


# ---------------------------------------------------------------------------
# Feishu slot
# ---------------------------------------------------------------------------

async def update_lark_slot(config: Config, value: str) -> bool:
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                LARK_SLOT_API,
                headers={
                    "Authorization": f"Bearer {config.lark_credential}",
                    "Content-Type": "application/json",
                },
                json={"slotId": config.lark_slot_id, "value": value},
            )
            if resp.status_code == 401:
                log.error("飞书 credential 无效 (401)")
                return False
            if resp.status_code == 429:
                log.warning("飞书写入限流 (429)")
                return False
            resp.raise_for_status()
            log.info("飞书签名已更新: %s", value)
            return True
        except Exception as e:
            log.error("飞书 slot 更新失败: %s", e)
            return False


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

async def sync_once(config: Config, dry_run: bool = False) -> bool:
    if not config.has_cursor:
        log.error("未配置 Cursor cookie，请运行: python %s --setup", sys.argv[0])
        return False

    stats = await fetch_cursor_stats(config.cursor_cookie)
    if not stats:
        return False

    signature = stats.format_signature()
    log.info("签名: %s", signature)

    if dry_run:
        log.info("[dry-run] 不执行飞书写入")
        return True

    if not config.has_lark:
        log.warning("飞书 slot 未配置，跳过写入")
        return True

    sync_state = _load_sync_state()
    if sync_state.get("last_value") == signature:
        log.info("数据未变化，跳过写入")
        return True
    last_sync = sync_state.get("last_sync_at", 0)
    if time.time() - last_sync < 55:
        log.info("距上次同步不足 60 秒，跳过")
        return True

    ok = await update_lark_slot(config, signature)
    if ok:
        _save_sync_state({
            "last_value": signature,
            "last_sync_at": time.time(),
            "last_sync_time": datetime.now().isoformat(),
        })
    return ok


async def sync_loop(config: Config, interval_minutes: int):
    log.info("启动持续同步，间隔 %d 分钟 (Ctrl+C 退出)", interval_minutes)
    while True:
        try:
            await sync_once(config)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("同步异常: %s", e)
        log.info("等待 %d 分钟...", interval_minutes)
        await asyncio.sleep(interval_minutes * 60)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def _mask(s: str) -> str:
    if not s:
        return "未设置"
    if len(s) <= 12:
        return s[:4] + "..."
    return s[:6] + "..." + s[-4:]


def setup_wizard():
    config = Config.load()
    print("""
╔════════════════════════════════════════════════════════╗
║      Cursor Dashboard → 飞书签名 配置向导             ║
╚════════════════════════════════════════════════════════╝

【Cursor Cookie】
  1. 浏览器打开 https://cursor.com/dashboard
  2. F12 → Application → Cookies → cursor.com
  3. 复制 WorkosCursorSessionToken 的值
""")
    cookie = input(f"WorkosCursorSessionToken [{_mask(config.cursor_cookie)}]: ").strip()
    if cookie:
        config.cursor_cookie = cookie

    print(f"""
【飞书 Slot】
  credential: {_mask(config.lark_credential)}
  slot_id:    {config.lark_slot_id or '未设置'}
""")
    change = input("需要修改飞书配置吗？(y/N): ").strip().lower()
    if change == "y":
        cred = input(f"credential [{_mask(config.lark_credential)}]: ").strip()
        if cred:
            config.lark_credential = cred
        sid = input(f"slot_id [{config.lark_slot_id}]: ").strip()
        if sid:
            config.lark_slot_id = sid

    config.save()
    print(f"\n✓ 配置已保存到 {CONFIG_FILE}")

    if config.has_lark:
        print(f"\n飞书签名 URL:")
        print(f'  https://l.garyyang.work/?t=%7B%7Bslot%20id%3D%22{config.lark_slot_id}%22%7D%7D')

    print(f"\n下一步: python {sys.argv[0]} --dry-run")


def main():
    parser = argparse.ArgumentParser(
        description="Cursor Dashboard → 飞书签名 动态同步",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python cursor_feishu_sync.py --setup           # 配置
  python cursor_feishu_sync.py --dry-run         # 预览
  python cursor_feishu_sync.py --once            # 单次同步
  python cursor_feishu_sync.py --loop -i 60      # 每 60 分钟同步

也支持环境变量 (用于 GitHub Actions 等 CI):
  CURSOR_COOKIE, LARK_CREDENTIAL, LARK_SLOT_ID
""",
    )
    parser.add_argument("--setup", action="store_true", help="配置向导")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写飞书")
    parser.add_argument("--once", action="store_true", help="单次同步")
    parser.add_argument("--loop", action="store_true", help="持续同步")
    parser.add_argument("-i", "--interval", type=int, default=60, help="同步间隔（分钟，默认 60）")
    args = parser.parse_args()

    if not any([args.setup, args.dry_run, args.once, args.loop]):
        parser.print_help()
        sys.exit(0)

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.setup:
        setup_wizard()
        return

    config = Config.load()
    if args.dry_run:
        asyncio.run(sync_once(config, dry_run=True))
    elif args.once:
        asyncio.run(sync_once(config))
    elif args.loop:
        try:
            asyncio.run(sync_loop(config, args.interval))
        except KeyboardInterrupt:
            log.info("已退出")


if __name__ == "__main__":
    main()
