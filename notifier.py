"""通知：程序挂了 / cookie 掉线 / 机器人答不了。

渠道（有哪个配置就走哪个，电脑桌面弹窗一直有）：
  PUSHPLUS_TOKEN   微信推送（pushplus.plus，微信扫码登录后复制 token）；
                   要多人收就写多个 token，用英文逗号隔开：token1,token2
  SERVERCHAN_KEY   微信推送（Server酱 sct.ftqq.com 的 SendKey）
同一类通知默认 1 小时内只发一次（key 相同算同一类），避免刷屏。
"""
import os
import re
import subprocess
import time
import threading

import requests
from loguru import logger

_last_sent = {}
_lock = threading.Lock()


def _desktop(title, content):
    """右下角弹一个系统通知（Windows 自带 PowerShell，不装任何东西）"""
    if os.name != 'nt':
        return
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
        "[System.Media.SystemSounds]::Exclamation.Play();"
        "$n.ShowBalloonTip(15000,$env:XY_NT,$env:XY_NC,'Warning');"
        "Start-Sleep 16;$n.Dispose()"
    )
    env = dict(os.environ, XY_NT=title, XY_NC=content[:240])
    subprocess.Popen(['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', ps],
                     env=env, creationflags=0x08000000)


def _pushplus(token, title, content, md=False):
    r = requests.post('https://www.pushplus.plus/send', json={
        'token': token, 'title': title, 'content': content, 'template': 'markdown' if md else 'txt'}, timeout=15)
    data = r.json()
    if data.get('code') != 200:
        raise RuntimeError(f"PushPlus 返回：{data}")


def _serverchan(key, title, content, md=False):
    r = requests.post(f'https://sctapi.ftqq.com/{key}.send', data={'title': title, 'desp': content}, timeout=15)
    data = r.json()
    if data.get('code') != 0:
        raise RuntimeError(f"Server酱返回：{data}")


def send(title, content, md=False):
    """马上发到所有已配置的渠道；某个渠道失败只记日志，不影响别的"""
    logger.warning(f"[通知] {title} | {content}")
    try:
        _desktop(title, content)
    except Exception as e:
        logger.error(f"桌面弹窗失败: {e!r}")
    sent = False
    for name, env, fn in (('PushPlus', 'PUSHPLUS_TOKEN', _pushplus), ('Server酱', 'SERVERCHAN_KEY', _serverchan)):
        vals = [v.strip() for v in re.split(r'[,，;；\s]+', os.getenv(env, '')) if v.strip()]
        for val in vals:
            try:
                fn(val, title, content, md)
                sent = True
            except Exception as e:
                logger.error(f"{name} 推送失败（{val[:6]}…）: {e!r}")
    if not sent and not (os.getenv('PUSHPLUS_TOKEN') or os.getenv('SERVERCHAN_KEY')):
        logger.warning("还没配置微信推送（.env 里的 PUSHPLUS_TOKEN），这次只弹了电脑通知")
    return sent


def alert(key, title, content, cooldown=3600):
    """带冷却的通知：同一个 key 在 cooldown 秒内只发一次"""
    with _lock:
        now = time.time()
        if now - _last_sent.get(key, 0) < cooldown:
            logger.info(f"[通知] {key} 冷却中，跳过")
            return False
        _last_sent[key] = now
    send(title, content)
    return True
