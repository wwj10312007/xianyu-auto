"""启动全部账号：python run_all.py [账号名 ...]
不带名字就启动 accounts 里所有已经登录的号；每个号一个独立进程，互不影响。
- 某个号的程序挂了：自动拉起，并通知机主
- 新加了账号（添加账号.bat）：30 秒内自动启动，不用重启
"""
import os
import subprocess
import sys
import threading
import time

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS = os.path.join(BASE, 'accounts')
os.chdir(BASE)
load_dotenv(os.path.join(BASE, '.env'))
import notifier  # noqa: E402  需要先加载 .env 里的推送配置

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

procs = {}      # 账号 -> 子进程
crashes = {}    # 账号 -> 连续异常退出次数
gave_up = set()  # 退出码 3（缺配置）的号，不反复拉起


def logged_in(name):
    p = os.path.join(ACCOUNTS, name, '.env')
    if not os.path.exists(p):
        return False
    with open(p, encoding='utf-8') as f:
        return 'COOKIES_STR=' in f.read() and 'unb=' in open(p, encoding='utf-8').read()


def pump(name, proc):
    for line in proc.stdout:
        print(line.rstrip('\n'), flush=True)


def start(name):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    proc = subprocess.Popen([sys.executable, '-u', 'main.py', name], cwd=BASE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    threading.Thread(target=pump, args=(name, proc), daemon=True).start()
    proc.started_at = time.time()
    procs[name] = proc
    print(f"[启动器] 账号「{name}」已启动", flush=True)


def single_instance():
    f = open(os.path.join(BASE, '.run_all.lock'), 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def main():
    only = sys.argv[1:]
    lock = single_instance()
    if lock is None:
        print("[启动器] 已经有一个在运行了，这个窗口可以关掉。", flush=True)
        return
    print("[启动器] 运行中，别关这个窗口。账号挂了会自动拉起并通知你。", flush=True)
    while True:
        names = only or sorted(d for d in os.listdir(ACCOUNTS) if os.path.isdir(os.path.join(ACCOUNTS, d))) if os.path.isdir(ACCOUNTS) else []
        for name in names:
            if name in gave_up or not logged_in(name):
                continue
            proc = procs.get(name)
            if proc is None:
                start(name)
            elif proc.poll() is not None:
                code = proc.returncode
                if code == 3:
                    gave_up.add(name)
                    print(f"[启动器] 账号「{name}」缺配置，不再重启，请看上面的提示", flush=True)
                    continue
                crashes[name] = crashes.get(name, 0) + 1
                print(f"[启动器] 账号「{name}」退出了（代码 {code}），10 秒后重启，第 {crashes[name]} 次", flush=True)
                threading.Thread(target=notifier.alert, args=(
                    f"crash:{name}", f"闲鱼自动回复：{name} 程序挂了",
                    f"程序异常退出（代码 {code}），已自动重启（第 {crashes[name]} 次）。如果反复出现请联系我们。", 600), daemon=True).start()
                time.sleep(10)
                start(name)
            elif time.time() - getattr(proc, 'started_at', time.time()) > 600:
                crashes[name] = 0
        time.sleep(30 if not only else 5)


if __name__ == '__main__':
    main()
