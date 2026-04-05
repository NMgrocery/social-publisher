#!/usr/bin/env python3
"""
小红书自动发布脚本 - Scrapling 增强版
结合 Playwright 的自动化能力和 Scrapling 的反检测 + 自适应解析

改进点：
1. Scrapling StealthyFetcher 做页面预检（反爬检测）
2. Playwright 做实际自动化交互
3. 自适应选择器（网站变更时更鲁棒）
4. 增强重试和容错机制
"""

import asyncio
import argparse
import json
import os
import sys
import re
import time
from pathlib import Path
from playwright.async_api import async_playwright
from typing import Optional, List, Callable

# ── 默认配置 ──────────────────────────────────────────────────────────────────
DEFAULT_PROFILE = os.path.expanduser("~/.catpaw/xhs_browser_profile")
PUBLISH_URL     = "https://creator.xiaohongshu.com/publish/publish?source=official"

# ── Scrapling 增强模块 ─────────────────────────────────────────────────────────

def try_scrapling_stealth_fetch(url: str, timeout: int = 30000) -> dict:
    """
    使用 Scrapling StealthyFetcher 检测页面是否可访问（反爬检测）
    返回 {"accessible": bool, "content": str, "error": str}
    """
    try:
        from scrapling.fetchers import StealthyFetcher
        fetcher = StealthyFetcher(headless=True, timeout=timeout/1000)
        page = fetcher.fetch(url)
        return {
            "accessible": True,
            "title": page.css('title::text').get() or "",
            "url": url,
            "content": page.text[:500] if page.text else ""
        }
    except Exception as e:
        return {"accessible": False, "error": str(e)[:100]}


def adaptive_find_element(page, selectors: List[str], role: str = None, name: str = None, timeout_ms: int = 5000) -> Optional[object]:
    """
    自适应元素查找 - 尝试多个选择器，支持 find_similar 降级
    
    Args:
        page: Playwright page object
        selectors: CSS 选择器列表（按优先级排序）
        role: ARIA role（可选）
        name: ARIA name（可选）
        timeout_ms: 超时时间
    
    Returns:
        Playwright locator 或 None
    """
    # 先尝试 role/name 的无障碍定位
    if role or name:
        try:
            if role and name:
                locator = page.get_by_role(role, name=name, exact=False)
            elif role:
                locator = page.get_by_role(role)
            else:
                locator = page.get_by_name(name)
            
            if locator.count() > 0:
                return locator
        except Exception:
            pass
    
    # 尝试 CSS 选择器列表
    for sel in selectors:
        try:
            locator = page.locator(sel).first
            if locator.count() > 0 and locator.is_visible(timeout=1000):
                return locator
        except Exception:
            continue
    
    return None


def wait_with_retry(
    condition: Callable[[], bool],
    interval_ms: int = 200,
    max_wait_ms: int = 10000,
    retry_count: int = 3
) -> bool:
    """带重试的条件等待"""
    for attempt in range(retry_count):
        elapsed = 0
        while elapsed < max_wait_ms:
            if condition():
                return True
            time.sleep(interval_ms / 1000)
            elapsed += interval_ms
        # 一次重试后还没成功，短暂休息
        if attempt < retry_count - 1:
            time.sleep(0.5)
    return False


# ── 核心工具函数 ──────────────────────────────────────────────────────────────

def truncate_title(title: str, max_len: int = 20) -> str:
    if len(title) <= max_len:
        return title
    return title[:max_len]


def validate_title(title: str) -> str:
    if len(title) > 20:
        truncated = truncate_title(title, 20)
        print(f"[WARN] 标题过长（{len(title)}字），已截断为: {truncated}")
        return truncated
    return title


def clear_profile_locks(profile_dir: str):
    """清除 Chrome 单例锁文件"""
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try:
            os.remove(os.path.join(profile_dir, lock))
        except FileNotFoundError:
            pass


def load_content_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for key in ["title", "content"]:
        if key not in data:
            raise ValueError(f"content.json 缺少必填字段: {key}")
    return data


def extract_text_from_element(page, selector: str) -> str:
    """从元素提取文本（增强容错）"""
    try:
        el = page.locator(selector).first
        if el.count() > 0:
            return el.text_content()
    except Exception:
        pass
    return ""


# ── Scrapling 预检 ─────────────────────────────────────────────────────────────

async def precheck_with_scrapling(page_url: str) -> dict:
    """使用 Scrapling 做页面预检"""
    print(f"[SCRAPLING] 预检页面: {page_url}")
    result = try_scrapling_stealth_fetch(page_url)
    if result["accessible"]:
        print(f"[SCRAPLING] ✅ 页面可访问，标题: {result.get('title', 'N/A')[:50]}")
    else:
        print(f"[SCRAPLING] ⚠️ 预检结果: {result.get('error', 'unknown')[:100]}")
    return result


# ── Playwright 自动化核心 ───────────────────────────────────────────────────────

async def js_click_element(page, selector: str = None, text: str = None) -> bool:
    """JS 点击元素（支持选择器或文本）"""
    if text:
        script = """(text) => {
            const el = Array.from(document.querySelectorAll('*')).find(
                e => e.children.length === 0 && e.textContent.trim() === text
            ) || Array.from(document.querySelectorAll('*')).find(
                e => e.textContent.trim() === text
            );
            if (el) { el.click(); return 'clicked'; }
            return 'not found';
        }"""
        result = await page.evaluate(script, text)
        return result == 'clicked'
    elif selector:
        try:
            await page.locator(selector).first.click()
            return True
        except Exception:
            pass
    return False


async def js_fill_input(page, selector: str, value: str) -> bool:
    """JS 填写输入框"""
    script = """([sel, val]) => {
        const inp = document.querySelector(sel);
        if (!inp) return 'not found';
        inp.focus();
        inp.value = val;
        inp.dispatchEvent(new Event('input', {bubbles: true}));
        inp.dispatchEvent(new Event('change', {bubbles: true}));
        return 'filled';
    }"""
    try:
        result = await page.evaluate(script, [selector, value])
        return result == 'filled'
    except Exception:
        return False


async def wait_for_url_pattern(page, pattern: str, timeout_ms: int = 15000) -> bool:
    """等待 URL 匹配特定模式"""
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        if pattern in page.url:
            return True
        await page.wait_for_timeout(200)
    return False


# ── 主要业务流程 ────────────────────────────────────────────────────────────────

async def wait_for_login(page, timeout_ms=90000):
    """等待用户扫码登录"""
    if "login" not in page.url:
        return True
    
    print("[WARN] 未登录，请在弹出的浏览器窗口中手动扫码登录（等待最多 90 秒）...")
    try:
        await page.wait_for_url(lambda url: "login" not in url, timeout=timeout_ms)
        await page.wait_for_timeout(2000)
        print("[INFO] 登录成功！")
        return True
    except Exception:
        print("[ERROR] 登录超时")
        return False


async def switch_to_image_tab(page):
    """切换到「上传图文」Tab"""
    print("[STEP] 切换到「上传图文」Tab...")
    
    # 方法1: Playwright get_by_role（最可靠）
    try:
        tab = page.get_by_text("上传图文", exact=True)
        if await tab.count() > 0:
            await tab.click()
            print("[INFO] [Role] 上传图文 tab clicked")
            await page.wait_for_timeout(1500)
            return
    except Exception:
        pass
    
    # 方法2: JS 文本匹配
    ok = await js_click_element(page, text="上传图文")
    print(f"[INFO] [JS] 上传图文 tab: {'clicked' if ok else 'not found'}")
    await page.wait_for_timeout(1500)


async def enter_text_image_mode(page, text_for_image: str = "") -> bool:
    """
    文字配图模式完整流程（增强版）
    """
    print("[STEP] 进入文字配图模式...")
    
    # 1. 点击「文字配图」按钮
    clicked = False
    for text in ["文字配图", "文字配图 "]:
        try:
            btn = page.get_by_text(text, exact=False)
            if await btn.count() > 0:
                await btn.first.click()
                clicked = True
                print(f"[INFO] 点击「文字配图」成功")
                break
        except Exception:
            pass
    
    if not clicked:
        # JS 降级
        result = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
            const btn = btns.find(b => b.textContent.includes('文字配图'));
            if (btn) { btn.click(); return 'clicked'; }
            return 'not found';
        }""")
        clicked = 'clicked' in result
        print(f"[INFO] JS 点击文字配图: {result}")
    
    await page.wait_for_timeout(2000)
    
    # 2. 在编辑器中输入文字
    if text_for_image:
        print(f"[STEP] 输入配图文字: {text_for_image[:30]}...")
        try:
            # 尝试找到 contenteditable 元素
            editor = page.locator('[contenteditable="true"].ProseMirror').first
            if await editor.count() > 0:
                await editor.click()
                await page.wait_for_timeout(300)
                await page.evaluate(
                    "(text) => { document.execCommand('insertText', false, text); }",
                    text_for_image
                )
                print("[INFO] 配图文字已输入")
        except Exception as e:
            print(f"[WARN] 输入失败: {e}")
    
    await page.wait_for_timeout(500)
    
    # 3. 点击「生成图片」
    print("[STEP] 点击「生成图片」...")
    gen_clicked = False
    for sel in [".edit-text-button", "button:has-text('生成图片')", "[class*='generate']"]:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                await btn.click()
                gen_clicked = True
                print(f"[INFO] 点击生成图片: {sel}")
                break
        except Exception:
            pass
    
    if not gen_clicked:
        result = await page.evaluate("""() => {
            const btn = document.querySelector('.edit-text-button');
            if (btn) { btn.click(); return 'clicked'; }
            return 'not found';
        }""")
        gen_clicked = 'clicked' in result
        print(f"[INFO] JS 生成图片: {result}")
    
    # 4. 等待 AI 生成
    print("[INFO] 等待 AI 生成配图（5s）...")
    await page.wait_for_timeout(5000)
    
    # 5. 点击「下一步」（轮询等待，更可靠）
    print("[STEP] 点击「下一步」...")
    next_clicked = False
    
    # 轮询最多 15 秒，等待 .overview-footer 出现
    for _ in range(15):
        try:
            footer = page.locator('.overview-footer').first
            if await footer.count() > 0:
                btn = footer.locator('button').first
                if await btn.count() > 0:
                    await btn.click()
                    next_clicked = True
                    print("[INFO] 点击「下一步」成功")
                    break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    
    if not next_clicked:
        # JS 降级
        result = await page.evaluate("""() => {
            const footer = document.querySelector('.overview-footer');
            if (footer) {
                const btn = footer.querySelector('button');
                if (btn) { btn.click(); return 'footer clicked'; }
            }
            // 找包含"下一步"的按钮
            const allBtns = Array.from(document.querySelectorAll('button'));
            const nextBtn = allBtns.find(b => b.textContent.includes('下一步'));
            if (nextBtn) { nextBtn.click(); return 'next clicked'; }
            return 'not found';
        }""")
        next_clicked = 'clicked' in result or 'found' in result
        print(f"[INFO] JS 下一步: {result}")
    
    await page.wait_for_timeout(2000)
    return next_clicked


async def fill_title_robust(page, title: str) -> bool:
    """填写标题（增强版，多方法尝试）"""
    print(f"[STEP] 填写标题: {title}")
    
    # 方法1: Playwright get_by_placeholder（最可靠）
    for placeholder in ["标题", "填写标题", "笔记标题"]:
        try:
            inp = page.get_by_placeholder(placeholder)
            if await inp.count() > 0:
                await inp.scroll_into_view_if_needed()
                await inp.click()
                await inp.fill(title)
                print(f"[INFO] [Placeholder] 标题填写成功: {placeholder}")
                return True
        except Exception:
            pass
    
    # 方法2: CSS 选择器
    for sel in ['input[placeholder*="标题"]', 'input[class*="title"]', 'input[name*="title"]']:
        try:
            inp = page.locator(sel).first
            if await inp.count() > 0:
                await inp.scroll_into_view_if_needed()
                await inp.click()
                await inp.fill(title)
                print(f"[INFO] [CSS] 标题填写成功: {sel}")
                return True
        except Exception:
            pass
    
    # 方法3: JS 填写
    result = await page.evaluate("""(title) => {
        const inputs = Array.from(document.querySelectorAll('input'));
        const inp = inputs.find(i => i.placeholder && i.placeholder.includes('标题'));
        if (inp) {
            inp.value = title;
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
            return 'filled';
        }
        return 'not found';
    }""", title)
    print(f"[INFO] [JS] 填写标题: {result}")
    return result == 'filled'


async def fill_content_robust(page, content: str) -> bool:
    """填写正文（增强版）"""
    print(f"[STEP] 填写正文（{len(content)} 字）")
    
    # 找到编辑器
    editor_sel = 'div.tiptap.ProseMirror, div.ProseMirror, [contenteditable="true"][role="textbox"]'
    el = page.locator(editor_sel).first
    
    try:
        await el.scroll_into_view_if_needed()
        await el.click()
        await page.wait_for_timeout(300)
        
        # 全选清空
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(200)
        
        # 剪贴板粘贴（Playwright API 方式）
        try:
            await page.evaluate("""(text) => {
                navigator.clipboard.writeText(text);
            }""", content)
            await page.keyboard.press("Control+v")
            await page.wait_for_timeout(500)
            print("[INFO] 正文粘贴成功")
            return True
        except Exception as e:
            print(f"[WARN] 粘贴失败: {e}")
        
        # 降级：逐段输入
        paragraphs = content.split('\n')
        for para in paragraphs:
            if para:
                await page.keyboard.type(para, delay=5)
            await page.keyboard.press("Enter")
        print("[INFO] 正文逐段输入完成")
        return True
        
    except Exception as e:
        print(f"[WARN] 正文填写失败: {e}")
        return False


async def click_publish_robust(page) -> bool:
    """点击发布按钮（增强版）"""
    print("[STEP] 点击发布...")
    
    # 方法1: get_by_role（最可靠）
    try:
        btns = [
            page.get_by_role("button", name="发布"),
            page.get_by_role("button", name="立即发布"),
            page.get_by_text("发布", exact=False),
        ]
        for btn in btns:
            if await btn.count() > 0 and await btn.is_visible(timeout=1000):
                await btn.scroll_into_view_if_needed()
                await btn.click()
                print("[INFO] [Role] 发布按钮点击成功")
                return True
    except Exception:
        pass
    
    # 方法2: CSS + JS 降级
    result = await page.evaluate("""() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const publishBtn = btns.find(b => ['发布', '立即发布'].includes(b.textContent.trim()));
        if (publishBtn) {
            publishBtn.scrollIntoView();
            publishBtn.click();
            return 'clicked: ' + publishBtn.textContent.trim();
        }
        return 'not found, total buttons: ' + btns.length;
    }""")
    print(f"[INFO] [JS] 发布: {result}")
    return 'not found' not in result


# ── 主流程 ────────────────────────────────────────────────────────────────────

async def publish(
    title: str,
    content: str,
    image_path: str = None,
    text_for_image: str = None,
    profile_dir: str = DEFAULT_PROFILE,
    headless: bool = False,
    workspace: str = None,
    use_scrapling_precheck: bool = True
):
    """
    完整发布流程（Scrapling 增强版）
    """
    workspace = workspace or os.getcwd()
    screenshot_before = os.path.join(workspace, "before_publish.png")
    screenshot_after  = os.path.join(workspace, "after_publish.png")

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    clear_profile_locks(profile_dir)
    print(f"[INFO] Profile: {profile_dir}")

    # ── Scrapling 预检 ──
    if use_scrapling_precheck:
        await precheck_with_scrapling(PUBLISH_URL)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--start-maximized",
            ],
            ignore_default_args=["--enable-automation"],
            no_viewport=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        page = context.pages[0] if context.pages else await context.new_page()

        # 1. 打开发布页
        print("[STEP 1] 打开发布页...")
        await page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)

        # 2. 检查登录
        if not await wait_for_login(page):
            await context.close()
            return False
        print(f"[INFO] 当前页面: {page.url}")

        # 3. 切换到上传图文 Tab
        await switch_to_image_tab(page)

        # 4. 文字配图模式
        img_text = text_for_image or content[:20]
        print(f"[MODE] 文字配图模式，配图文字: {img_text[:20]}")
        
        if not await enter_text_image_mode(page, text_for_image=img_text):
            print("[ERROR] 进入文字配图模式失败")
            await page.screenshot(path=screenshot_before)
            await context.close()
            return False

        # 5. 填写标题
        if not await fill_title_robust(page, title):
            print("[ERROR] 标题填写失败")
        
        await page.wait_for_timeout(400)

        # 6. 填写正文
        if not await fill_content_robust(page, content):
            print("[ERROR] 正文填写失败")

        await page.wait_for_timeout(500)

        # 7. 发布前截图
        await page.screenshot(path=screenshot_before)
        print(f"[INFO] 发布前截图: {screenshot_before}")

        # 8. 点击发布
        if not await click_publish_robust(page):
            print("[ERROR] 发布按钮点击失败")
            await context.close()
            return False

        # 9. 等待结果
        try:
            await page.wait_for_url(
                lambda url: "success" in url or "manage" in url,
                timeout=15000
            )
        except Exception:
            pass

        await page.wait_for_timeout(2000)
        await page.screenshot(path=screenshot_after)
        final_url = page.url
        print(f"[INFO] 发布后截图: {screenshot_after}")
        print(f"[INFO] 最终 URL: {final_url}")

        success = "success" in final_url or "manage" in final_url
        if success:
            print("[SUCCESS] 🎉 笔记发布成功！")
        else:
            print("[INFO] 请查看截图确认发布状态")

        await page.wait_for_timeout(2000)
        await context.close()
        return success


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="小红书自动发布脚本 - Scrapling 增强版")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--title", help="笔记标题")
    group.add_argument("--content-file", metavar="FILE", help="JSON 文件路径")
    parser.add_argument("--content", default=None, help="笔记正文")
    parser.add_argument("--image", default=None, help="图片路径")
    parser.add_argument("--text-for-image", default=None, help="文字配图模式：配图文字")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="browser profile 目录")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--workspace", default=None, help="截图保存目录")
    parser.add_argument("--no-scrapling", dest="use_scrapling_precheck", action="store_false", default=True, help="禁用 Scrapling 预检")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.content_file:
        try:
            data = load_content_file(args.content_file)
            title          = validate_title(data["title"])
            content        = data["content"]
            image_path     = data.get("image", args.image)
            text_for_image = data.get("text_for_image", args.text_for_image)
        except Exception as e:
            print(f"[ERROR] 读取 content.json 失败: {e}")
            sys.exit(1)
    else:
        if not args.content:
            print("[ERROR] 使用 --title 模式时，--content 为必填项")
            sys.exit(1)
        title          = validate_title(args.title)
        content        = args.content
        image_path     = args.image
        text_for_image = args.text_for_image

    mode = "上传图片" if image_path else f"文字配图（{text_for_image or '正文前20字'}）"
    print("=" * 60)
    print("  小红书自动发布脚本 - Scrapling 增强版")
    print(f"  标题: {title}")
    print(f"  正文: {content[:40]}...")
    print(f"  模式: {mode}")
    print("=" * 60)

    ok = asyncio.run(publish(
        title=title,
        content=content,
        image_path=image_path,
        text_for_image=text_for_image,
        profile_dir=args.profile,
        headless=args.headless,
        workspace=args.workspace,
        use_scrapling_precheck=args.use_scrapling_precheck,
    ))
    sys.exit(0 if ok else 1)
