"""添加账号 / 换登录：弹出一个浏览器窗口打开闲鱼网页版，用该账号的闲鱼 App 扫码登录，
登录信息（cookie）自动存进 accounts\\<账号名>\\.env，不用你复制粘贴。

  python add_account.py            问你账号名
  python add_account.py 账号2       直接给账号名；账号已存在就是「换登录」
"""
import os
import re
import sys
import time
import urllib.parse

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS = os.path.join(BASE, 'accounts')
os.chdir(BASE)
load_dotenv(os.path.join(BASE, '.env'))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def ask_name():
    existing = sorted(d for d in os.listdir(ACCOUNTS) if os.path.isdir(os.path.join(ACCOUNTS, d))) if os.path.isdir(ACCOUNTS) else []
    if existing:
        print("已有账号：" + "、".join(existing))
    print("要换登录的账号，就输入它的名字；新加账号就输入一个新名字（比如 账号2）。")
    name = input("账号名：").strip()
    return name or f"账号{len(existing) + 1}"


def cookie_string(cookies):
    """goofish.com 域下的 cookie 拼成请求头格式，同名取后出现的"""
    picked = {}
    for c in cookies:
        if c['domain'].lstrip('.').endswith('goofish.com'):
            picked[c['name']] = c['value']
    return '; '.join(f"{k}={v}" for k, v in picked.items()), picked


def nick_of(picked):
    raw = urllib.parse.unquote(picked.get('tracknick', '') or picked.get('_nk_', ''))
    return raw.encode('utf-8').decode('unicode_escape') if '\\u' in raw else raw


def save_cookie(name, cookie_str):
    d = os.path.join(ACCOUNTS, name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, '.env')
    lines = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            lines = [l for l in f.read().splitlines() if not l.startswith('COOKIES_STR=')]
    lines.append(f"COOKIES_STR={cookie_str}")
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return path


def validate(name, cookie_str):
    """拿这串 cookie 真的去换一次闲鱼聊天 token，换得到才算可用"""
    from XianyuApis import XianyuApis
    from utils.xianyu_utils import generate_device_id
    api = XianyuApis(env_path=os.path.join(ACCOUNTS, name, '.env'), account=name)
    api.apply_cookie_str(cookie_str)
    unb = api.session.cookies.get('unb')
    api.get_token(generate_device_id(unb), wait=False)


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else ask_name()
    from playwright.sync_api import sync_playwright
    print(f"\n正在打开闲鱼登录窗口（账号「{name}」）……")
    print("请用【这个账号】的闲鱼 App 扫窗口里的二维码登录（手机点首页左上角「扫一扫」）。")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=False, args=['--window-size=1100,800'])
        context = browser.new_context(no_viewport=True)
        page = context.new_page()
        page.goto('https://www.goofish.com/', wait_until='domcontentloaded')
        deadline = time.time() + 300
        last_err = ''
        cookie_str = picked = None
        while time.time() < deadline:
            time.sleep(2)
            cookie_str, picked = cookie_string(context.cookies())
            if 'unb' not in picked:
                continue
            time.sleep(3)  # 等登录后页面把剩下的 cookie 种完
            cookie_str, picked = cookie_string(context.cookies())
            try:
                validate(name, cookie_str)
                break
            except Exception as e:  # 还没登录完整，接着等
                last_err = repr(e)
                cookie_str = None
        else:
            browser.close()
            print(f"\n5 分钟内没有登录成功。{last_err}\n重新双击「添加账号.bat」再试一次。")
            sys.exit(1)
        browser.close()
    path = save_cookie(name, cookie_str)
    print(f"\n登录成功：{nick_of(picked) or picked.get('unb')}（账号「{name}」）")
    print(f"登录信息已保存：{path}")
    print("如果程序在运行，新账号 30 秒内自动启动；已掉线的账号会自动恢复，不用重启。")


if __name__ == '__main__':
    main()
