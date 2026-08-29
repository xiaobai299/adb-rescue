# -*- coding: utf-8 -*-
"""验证破坏性操作的二次确认对话框（安全闸门）。"""
import os
import sys
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ.setdefault("ADB_RESCUE_HOME", os.path.join(HERE, "config"))
sys.path.insert(0, ROOT)

from ui.widgets import ask_confirm  # noqa: E402

results = []


def find_widget(widget, cls):
    """安全遍历：对话框被销毁后立即停止，避免 TclError。"""
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return
    for c in children:
        try:
            is_target = isinstance(c, cls)
        except tk.TclError:
            return
        if is_target:
            yield c
        yield from find_widget(c, cls)


def auto_answer(root, action: str, typed: str = ""):
    """弹窗出现后自动操作：填文字 → 勾选确认框 → 点击目标按钮。"""
    def go():
        tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        if not tops:
            root.after(80, go)
            return
        top = tops[-1]
        if typed:
            for e in find_widget(top, ttk.Entry):
                try:
                    e.delete(0, "end")
                    e.insert(0, typed)
                except tk.TclError:
                    return
        for cb in find_widget(top, ttk.Checkbutton):
            try:
                cb.invoke()
            except tk.TclError:
                return
        for btn in find_widget(top, ttk.Button):
            try:
                if btn.cget("text") == action:
                    btn.invoke()
                    return
            except tk.TclError:
                return
        root.after(80, go)
    root.after(250, go)


root = tk.Tk()
root.withdraw()

# 用例 1：勾选风险确认框后点“确认执行” → True
auto_answer(root, "确认执行")
r1 = ask_confirm(root, title="确认重启", message="设备将立即重启。", risk="danger")
results.append(("勾选风险确认框后点确认 → 放行(True)", r1 is True))

# 用例 2：点“取消” → False
auto_answer(root, "取消")
r2 = ask_confirm(root, title="确认卸载", message="将卸载应用并删除数据。", risk="danger")
results.append(("点取消 → 拦截(False)", r2 is False))

# 用例 3：需手动输入危险文字，输入正确 → True
auto_answer(root, "确认执行", typed="清除数据")
r3 = ask_confirm(root, title="清除数据", message="将清除应用数据。",
                 risk="danger", require_text="清除数据")
results.append(("输入正确校验文字 → 放行(True)", r3 is True))

# 用例 4：需手动输入危险文字，输入错误 → False（弹警告后取消）
auto_answer(root, "取消", typed="随便写")
r4 = ask_confirm(root, title="清除数据", message="将清除应用数据。",
                 risk="danger", require_text="清除数据")
results.append(("输入错误校验文字 → 拦截(False)", r4 is False))

try:
    root.destroy()
except tk.TclError:
    pass

for name, ok in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
failed = [n for n, ok in results if not ok]
print(f"\n结果：通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
