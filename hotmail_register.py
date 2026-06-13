#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hotmail/Outlook 邮箱自动注册脚本
基于 ruyipage (Firefox + WebDriver BiDi) — 原生 isTrusted 事件，绕过 webdriver 检测

经过真实页面端到端测试验证的完整注册流程：
  1. 个人数据导出许可 → 点击"同意并继续"
  2. 输入邮箱用户名 + 选择域名（@hotmail.com / @outlook.com）→ 下一步
  3. 创建密码 → 下一步
  4. 添加生日（年份输入框 + 月/日 Fluent UI 下拉）→ 下一步
  5. 添加姓名（#lastNameInput / #firstNameInput）→ 下一步
  6. 人机验证（hsprotect.net iframe 长按按钮）
     - 自动模式：ruyipage BiDi 在 iframe context 内执行原生长按
     - 手动模式：等待用户在浏览器窗口手动完成（默认备用）
     - 打码模式：2captcha API（需 --captcha-key）
  7. 等待注册完成

关键设计：每注册一个账号都使用全新的 Firefox 实例 + 独立 Profile，
避免 Cookie/Session/指纹复用被微软识别为同一浏览器。

依赖安装：
    pip install ruyiPage faker requests
    apt install firefox  (Ubuntu) 或指定 --firefox-path

用法：
    python hotmail_register.py                        # 注册 1 个账号
    python hotmail_register.py --count 3               # 注册 3 个账号
    python hotmail_register.py --username myname       # 指定用户名
    python hotmail_register.py --password MyPass@123   # 指定密码
    python hotmail_register.py --domain outlook.com    # 使用 outlook.com 域名
    python hotmail_register.py --output accounts.txt   # 指定输出文件
    python hotmail_register.py --headless              # 无头模式
    python hotmail_register.py --captcha-key YOUR_KEY  # 2captcha 全自动打码
    python hotmail_register.py --proxy http://user:pass@host:port
    python hotmail_register.py --firefox-path /usr/lib/firefox/firefox
    python hotmail_register.py --delay 30              # 账号基础间隔 30 秒
"""

import argparse
import base64
import logging
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time
from datetime import datetime

# ── 第三方依赖检查 ────────────────────────────────────────────────────────────
try:
    from faker import Faker
except ImportError:
    print("请先安装依赖: pip install ruyiPage faker requests")
    sys.exit(1)

try:
    from ruyipage import FirefoxOptions, FirefoxPage
    from ruyipage._functions.settings import Settings
    from ruyipage._bidi import browsing_context as bidi_context
    from ruyipage._bidi import input_ as bidi_input
except ImportError:
    print("请先安装 ruyiPage: pip install ruyiPage")
    sys.exit(1)

# ── 日志配置 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("hotmail_register.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────
SIGNUP_URL = (
    "https://signup.live.com/signup"
    "?cobrandid=ab0455a0-8d03-46b9-b18b-df2f57b9e44c"
    "&contextid=7354FDAB8CB435DB"
    "&opid=DDEDCCB4AA202B2D"
    "&bk=1775907821"
    "&lw=dob,flname,wld"
    "&uiflavor=web"
    "&fluent=2"
    "&client_id=00000000487A244A"
    "&lic=1"
    "&mkt=ZH-CN"
    "&lc=2052"
)

fake = Faker(["en_US"])


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def gen_password(length: int = 12) -> str:
    """生成符合微软密码要求的随机密码（大写+小写+数字+特殊字符）"""
    chars = (
        random.choices(string.ascii_uppercase, k=2)
        + random.choices(string.ascii_lowercase, k=4)
        + random.choices(string.digits, k=3)
        + random.choices("@#$%!&*", k=1)
        + random.choices(string.ascii_letters + string.digits, k=length - 10)
    )
    random.shuffle(chars)
    return "".join(chars)


def gen_account(username=None, password=None, domain="hotmail.com") -> dict:
    """生成随机账号信息"""
    first_name = fake.first_name()
    last_name = fake.last_name()
    if not username:
        base = (first_name + last_name).lower()
        base = "".join(c for c in base if c.isalnum())[:16]
        username = base + str(random.randint(100, 9999))
    if not password:
        password = gen_password()
    return {
        "email": f"{username}@{domain}",
        "username": username,
        "password": password,
        "first_name": first_name,
        "last_name": last_name,
        "birth_year": random.randint(1978, 2000),
        "birth_month": random.randint(1, 12),
        "birth_day": random.randint(1, 28),
        "domain": domain,
    }


def save_account(account: dict, output_file: str):
    """将成功注册的账号追加写入文件"""
    line = (
        f"邮箱: {account['email']} | "
        f"密码: {account['password']} | "
        f"姓名: {account['last_name']} {account['first_name']} | "
        f"生日: {account['birth_year']}-{account['birth_month']:02d}-{account['birth_day']:02d} | "
        f"注册时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    with open(output_file, "a", encoding="utf-8") as f:
        f.write(line)
    log.info(f"账号已保存: {account['email']}")


def find_firefox() -> str:
    """自动查找 Firefox 可执行文件路径"""
    for p in [
        "/usr/lib/firefox/firefox",
        "/usr/bin/firefox",
        "/snap/bin/firefox",
        "/usr/local/bin/firefox",
    ]:
        if os.path.exists(p):
            return p
    try:
        result = subprocess.run(["which", "firefox"], capture_output=True, text=True)
        path = result.stdout.strip()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    return None


# ── 2captcha 打码 ─────────────────────────────────────────────────────────────

def solve_2captcha(api_key: str, page_url: str, timeout: int = 120):
    """调用 2captcha API 解决 PerimeterX/HUMAN 验证码"""
    try:
        import requests
    except ImportError:
        log.warning("requests 未安装，无法使用 2captcha")
        return None
    log.info("提交 2captcha 任务...")
    try:
        r = requests.post("https://2captcha.com/in.php", data={
            "key": api_key, "method": "funcaptcha",
            "publickey": "PXzC5j78di", "pageurl": page_url, "json": 1,
        }, timeout=30)
        res = r.json()
        if res.get("status") != 1:
            log.warning(f"2captcha 提交失败: {res}")
            return None
        task_id = res["request"]
        log.info(f"2captcha 任务 ID: {task_id}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            r2 = requests.get(
                f"https://2captcha.com/res.php?key={api_key}&action=get&id={task_id}&json=1",
                timeout=30
            )
            res2 = r2.json()
            if res2.get("status") == 1:
                log.info("2captcha 解码成功")
                return res2["request"]
            if res2.get("request") != "CAPCHA_NOT_READY":
                log.warning(f"2captcha 错误: {res2}")
                return None
        log.warning("2captcha 超时")
    except Exception as e:
        log.warning(f"2captcha 异常: {e}")
    return None


# ── 核心注册类 ────────────────────────────────────────────────────────────────

class HotmailRegistrar:
    def __init__(self, args):
        self.args = args
        self.page = None
        self.driver = None
        self._ff_process = None

    def _init_browser(self):
        """初始化 Firefox 实例，利用端口隔离确保完全独立的单例对象"""
        import socket
        Settings.bidi_timeout = 30

        free_port = None
        for port in range(9224, 9400):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(('127.0.0.1', port))
                    free_port = port
                    break
            except OSError:
                continue
        if free_port is None:
            free_port = 9224

        opts = FirefoxOptions()
        opts.set_port(free_port)

        if os.environ.get("CI"):
            log.info("检测到 CI 环境，为 Firefox 添加兼容参数")
            if not hasattr(opts, '_arguments'):
                opts._arguments = []
            opts._arguments.extend(['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'])

        ff_path = self.args.firefox_path or find_firefox()
        if ff_path:
            opts.set_browser_path(ff_path)

        if self.args.headless:
            opts.headless(True)

        if self.args.proxy:
            try:
                opts.set_proxy(self.args.proxy)
            except Exception as e:
                log.warning(f"代理设置失败: {e}")

        self.page = FirefoxPage(opts)
        self.driver = self.page._driver._browser_driver

        try:
            ff_inst = getattr(self.page, '_firefox', None) or getattr(
                getattr(self.page, 'browser', None), None, None)
            if ff_inst and hasattr(ff_inst, '_process'):
                self._ff_process = ff_inst._process
        except Exception:
            pass

    def quit(self):
        """完全关闭浏览器进程并清理类单例缓存"""
        ff_process = self._ff_process

        try:
            if self.page:
                self.page.quit()
        except Exception:
            pass

        if ff_process:
            try:
                ff_process.wait(timeout=10)
            except Exception:
                pid = getattr(ff_process, 'pid', None)
                if pid:
                    try:
                        subprocess.run(["kill", "-9", str(pid)], capture_output=True)
                        ff_process.wait(timeout=3)
                    except Exception:
                        pass

        try:
            from ruyipage._base.browser import Firefox as _Firefox
            with _Firefox._lock:
                dead_addrs = [addr for addr, inst in _Firefox._BROWSERS.items()
                              if not getattr(inst, '_initialized', False) or
                              (hasattr(inst, '_driver') and inst._driver and
                               not getattr(inst._driver, '_is_running', True))]
                for addr in dead_addrs:
                    _Firefox._BROWSERS.pop(addr, None)
                _Firefox._BROWSERS.clear()
        except Exception:
            pass

        self.page = None
        self.driver = None
        self._ff_process = None

    def _ss(self, name="debug"):
        path = f"reg_{name}_{int(time.time())}.png"
        try:
            data = self.page.screenshot(as_bytes=True)
            if data:
                with open(path, "wb") as f:
                    f.write(data)
        except Exception:
            pass

    def _wait(self, sel, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                el = self.page.ele(sel, timeout=2)
                if el and el.__class__.__name__ != "NoneElement":
                    return el
            except Exception:
                pass
            time.sleep(0.4)
        return None

    def _type(self, ele, text):
        ele.click_self()
        time.sleep(random.uniform(0.2, 0.5))
        for ch in text:
            ele.input(ch, clear=False)
            time.sleep(random.uniform(0.06, 0.18))

    def _submit(self):
        for sel in ["css:button[type='submit']", "css:input[type='submit']"]:
            btn = self._wait(sel, timeout=4)
            if btn:
                btn.click_self()
                return
        log.warning("未找到提交按钮")

    def _get_contexts(self):
        all_ctxs = []
        seen = set()

        def recurse(ctx_id, depth):
            if depth > 5 or ctx_id in seen:
                return
            seen.add(ctx_id)
            try:
                result = bidi_context.get_tree(self.driver, root=ctx_id)
                for ctx in result.get("contexts", []):
                    cid = ctx["context"]
                    if cid not in seen:
                        all_ctxs.append((depth, ctx))
                        seen.add(cid)
                    for child in ctx.get("children", []):
                        ccid = child["context"]
                        if ccid not in seen:
                            all_ctxs.append((depth + 1, child))
                            seen.add(ccid)
                        recurse(ccid, depth + 2)
            except Exception:
                pass

        recurse(self.page._context_id, 0)
        return all_ctxs

    def _find_hs_context(self, max_wait=20):
        deadline = time.time() + max_wait
        while time.time() < deadline:
            all_ctxs = self._get_contexts()
            for depth, ctx in all_ctxs:
                if "hsprotect" not in ctx.get("url", ""):
                    continue
                if depth != 1:
                    continue
                cid = ctx["context"]
                try:
                    result = bidi_context.capture_screenshot(self.driver, cid)
                    data = base64.b64decode(result.get("data", ""))
                    if len(data) > 1000:
                        return cid
                except Exception:
                    pass
            time.sleep(2)
        return None

    def _bidi_hold(self, ctx_id, x, y, hold_sec=10):
        try:
            move_action = [{
                "type": "pointer",
                "id": "mouse1",
                "parameters": {"pointerType": "mouse"},
                "actions": [{"type": "pointerMove", "x": x, "y": y, "duration": 100}]
            }]
            bidi_input.perform_actions(self.driver, ctx_id, move_action)
            time.sleep(0.3)

            down_action = [{
                "type": "pointer",
                "id": "mouse1",
                "parameters": {"pointerType": "mouse"},
                "actions": [{"type": "pointerDown", "button": 0}]
            }]
            bidi_input.perform_actions(self.driver, ctx_id, down_action)
            log.info(f"   长按 {hold_sec}s ...")
            time.sleep(hold_sec)

            up_action = [{
                "type": "pointer",
                "id": "mouse1",
                "parameters": {"pointerType": "mouse"},
                "actions": [{"type": "pointerUp", "button": 0}]
            }]
            bidi_input.perform_actions(self.driver, ctx_id, up_action)
            log.info("   长按释放")
            return True
        except Exception as e:
            log.warning(f"   BiDi 长按失败: {e}")
            return False

    def _handle_captcha(self):
        time.sleep(3)
        ctx_id = self._find_hs_context(max_wait=20)
        if ctx_id:
            log.info("尝试 BiDi 原生长按验证码...")
            coords = [(220, 45), (200, 45), (180, 45)]
            for x, y in coords:
                if self._bidi_hold(ctx_id, x, y, hold_sec=10):
                    time.sleep(4)
                    if "机器人" not in (self.page.title or ""):
                        log.info("   BiDi 长按验证码通过！")
                        return True
                    time.sleep(2)

        if self.args.captcha_key:
            log.info("尝试 2captcha 打码...")
            token = solve_2captcha(self.args.captcha_key, self.page.url or SIGNUP_URL)
            if token:
                try:
                    self.page.run_js(
                        f'document.querySelector(\'[name="px-captcha"]\') && '
                        f'(document.querySelector(\'[name="px-captcha"]\').value = \'{token}\');'
                    )
                    time.sleep(2)
                    if "机器人" not in (self.page.title or ""):
                        return True
                except Exception:
                    pass

        log.info("=" * 50)
        log.info("请在浏览器窗口中手动完成人机验证...")
        log.info("=" * 50)

        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            if "机器人" not in (self.page.title or ""):
                log.info("   手动验证完成！")
                return True
        return False

    def register_one(self, account: dict) -> bool:
        try:
            self.page.get(SIGNUP_URL, wait="complete")
            time.sleep(random.uniform(2.0, 3.5))

            consent_btn = self._wait("#nextButton", timeout=10)
            if consent_btn:
                consent_btn.click_self()
                time.sleep(random.uniform(1.5, 2.5))

            email_inp = self._wait("css:input[type='email']", timeout=12)
            if not email_inp:
                return False

            self._type(email_inp, account["username"])
            time.sleep(0.5)

            domain_btn = self._wait("#domainDropdownId", timeout=5)
            if domain_btn:
                domain_btn.click_self()
                time.sleep(random.uniform(0.6, 1.0))
                target_domain = f"@{account['domain']}"
                for opt in self.page.eles("css:[role='option']"):
                    try:
                        if target_domain in opt.text:
                            opt.click_self()
                            break
                    except Exception:
                        pass

            self._submit()
            time.sleep(random.uniform(2.0, 3.0))
            title = self.page.title or ""

            if "创建你的 Microsoft 帐户" in title or "create" in title.lower():
                return False

            pwd_inp = self._wait("css:input[type='password']", timeout=10)
            if not pwd_inp:
                return False

            self._type(pwd_inp, account["password"])
            time.sleep(0.5)
            self._submit()
            time.sleep(random.uniform(1.5, 2.5))

            year_inp = self._wait("css:input[name='BirthYear']", timeout=10)
            if not year_inp:
                return False

            year_inp.input(str(account["birth_year"]), clear=True)
            time.sleep(0.5)

            for sel in ["#BirthMonthDropdown", "css:button[name='BirthMonth']"]:
                mb = self._wait(sel, timeout=3)
                if mb:
                    mb.click_self()
                    time.sleep(random.uniform(0.7, 1.0))
                    target_month = f"{account['birth_month']}月"
                    for opt in self.page.eles("css:[role='option']"):
                        if opt.text.strip() == target_month:
                            opt.click_self()
                            break
                    break

            for sel in ["#BirthDayDropdown", "css:button[name='BirthDay']"]:
                db = self._wait(sel, timeout=3)
                if db:
                    db.click_self()
                    time.sleep(random.uniform(0.7, 1.0))
                    target_day = f"{account['birth_day']}日"
                    for opt in self.page.eles("css:[role='option']"):
                        if opt.text.strip() == target_day:
                            opt.click_self()
                            break
                    break

            self._submit()
            time.sleep(random.uniform(2.0, 3.0))

            last_inp = self._wait("#lastNameInput", timeout=10)
            if last_inp:
                last_inp.input(account["last_name"], clear=True)
                time.sleep(0.5)
                first_inp = self._wait("#firstNameInput", timeout=5)
                if first_inp:
                    first_inp.input(account["first_name"], clear=True)
                self._submit()
                time.sleep(random.uniform(2.0, 3.0))
                if "被阻止" in (self.page.title or ""):
                    return False

            if "机器人" in (self.page.title or ""):
                if not self._handle_captcha():
                    return False

            log.info("  [7] 等待注册完成...")
            for i in range(30):
                time.sleep(1)
                url = self.page.url or ""
                if any(x in url for x in ["outlook.com", "oauth20_authorize", "login.live.com"]):
                    return True

            return "signup.live.com" not in (self.page.url or "")
        except Exception as e:
            log.error(f"  注册异常: {e}")
            return False


# ── 命令行入口 ────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Hotmail/Outlook 自动注册脚本")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--username", type=str, default=None)
    p.add_argument("--password", type=str, default=None)
    p.add_argument("--domain", type=str, default="hotmail.com", choices=["hotmail.com", "outlook.com"])
    p.add_argument("--output", type=str, default="accounts.txt")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--proxy", type=str, default=None)
    p.add_argument("--captcha-key", type=str, default=None, dest="captcha_key")
    p.add_argument("--firefox-path", type=str, default=None, dest="firefox_path")
    p.add_argument("--delay", type=float, default=15.0)
    return p.parse_args()


def main():
    args = parse_args()

    log.info("=" * 60)
    log.info("Hotmail 自动注册脚本 (ruyipage + Firefox BiDi)")
    log.info("=" * 60)

    success = 0
    failed = 0

    for i in range(args.count):
        log.info(f"\n[{i+1}/{args.count}] 注册第 {i+1} 个账号")
        username = args.username if (args.count == 1 and args.username) else None
        account = gen_account(username, args.password, args.domain)

        registrar = HotmailRegistrar(args)
        ok = False
        try:
            for attempt in range(3):
                try:
                    registrar._init_browser()
                    break
                except Exception:
                    try: registrar.quit()
                    except Exception: pass
                    time.sleep(6)
            else:
                failed += 1
                continue

            ok = registrar.register_one(account)
        except KeyboardInterrupt:
            log.info("\n用户中断")
            try: registrar.quit()
            except Exception: pass
            break
        finally:
            try:
                # ── 你的最新修改逻辑 ─────────────────────────────────────
                registrar.quit()
                # 等待端口完全释放
                time.sleep(4)
                # ────────────────────────────────────────────────────────
            except Exception:
                pass

        if ok:
            save_account(account, args.output)
            success += 1
            log.info(f"  ✅ 注册成功")
        else:
            failed += 1
            log.warning(f"  ❌ 注册失败")

        # ── 你的最新修改逻辑（账号间隔，最后一个不需要等待） ─────────────
        if i < args.count - 1:
            delay = args.delay + random.uniform(0, 5)
            log.info(f"  等待 {delay:.1f}s 后注册下一个（让微软服务器冷却）...")
            time.sleep(delay)
        # ────────────────────────────────────────────────────────

    log.info("\n" + "=" * 60)
    log.info(f"注册完成: 成功 {success} 个，失败 {failed} 个")
    log.info(f"账号文件: {args.output}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
