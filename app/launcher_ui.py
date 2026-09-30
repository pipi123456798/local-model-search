"""轻量启动反馈和浏览器回退控制窗口，不依赖 WebView 就绪。"""
from __future__ import annotations

import json
import subprocess
import sys
import webbrowser

from runtime import open_folder, user_data_dir


def show_error(message):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('本地模型检索', str(message), parent=root)
        root.destroy()
    except Exception:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(message), '本地模型检索', 0x10)
        elif sys.platform == 'darwin':
            script = 'display alert "本地模型检索" message %s' % json.dumps(str(message), ensure_ascii=False)
            subprocess.run(['/usr/bin/osascript', '-e', script], check=False)


class StartupWindow:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.root = tk.Tk()
        self.root.title('本地模型检索')
        self.root.geometry('480x160')
        self.cancelled = False
        self.root.protocol('WM_DELETE_WINDOW', self.cancel)
        ttk.Label(self.root, text='正在启动本地模型检索…\n运行环境与权重已内置，无需下载。',
                  padding=20).pack()
        progress = ttk.Progressbar(self.root, mode='indeterminate', length=400)
        progress.pack()
        progress.start(12)
        self.tick()

    def cancel(self):
        self.cancelled = True

    def tick(self):
        self.root.update()
        if self.cancelled:
            raise KeyboardInterrupt()

    def close(self):
        self.root.destroy()


def browser_controls(url, process):
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title('本地模型检索 · 浏览器模式')
    root.geometry('480x220')
    ttk.Label(root, text='界面已在浏览器打开。\n关闭本窗口即可退出后台服务。', padding=20).pack()
    ttk.Button(root, text='重新打开界面', command=lambda: webbrowser.open(url)).pack(pady=4)
    ttk.Button(root, text='打开配置与日志目录', command=lambda: open_folder(user_data_dir())).pack(pady=4)
    ttk.Button(root, text='退出', command=root.destroy).pack(pady=4)

    def watch():
        if process.poll() is not None:
            root.destroy()
        else:
            root.after(500, watch)

    root.after(500, watch)
    root.mainloop()
