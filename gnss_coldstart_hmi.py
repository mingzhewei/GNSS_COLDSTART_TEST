# -*- coding: utf-8 -*-
"""
GNSS 冷启动分析 HMI（当前支持：北云 BY UG016）
=====================================================================
用法：python gnss_coldstart_hmi.py

功能：
  * 选择产品（当前仅北云可用；华测 HUACE 数据未到位，预留入口为禁用状态）
  * 多选该产品的数据文件加入“分析组”
  * 可多次添加、可移除/清空
  * 点击“开始分析”后调用 BY_coldstart.py 对整组文件出一份报告
  * 完成后可自动用默认浏览器打开 HTML 报告
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "GNSS 冷启动分析"
BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ProductConfig:
    key: str
    display_name: str
    script: str
    filetypes: Sequence[tuple[str, str]]
    color: str = "#34495e"
    enabled: bool = True
    note: str = ""


# 产品注册表：华测数据到位后，把 enabled 改为 True 并补充 script 即可
PRODUCTS: dict[str, ProductConfig] = {
    "beiyun": ProductConfig(
        key="beiyun",
        display_name="北云 BY（UG016 组合惯导，COM1 ASCII .log）",
        script="BY_coldstart.py",
        filetypes=(("北云 COM1 日志", "*.log"), ("所有文件", "*.*")),
        color="#1a5276",
        enabled=True,
    ),
    "huace": ProductConfig(
        key="huace",
        display_name="华测 HUACE（M720，COM1 ASCII #BESTPOSA .log）",
        script="HUACE_coldstart.py",
        filetypes=(("华测 COM1 日志", "*.log"), ("所有文件", "*.*")),
        color="#b03a2e",
        enabled=True,
    ),
}


def open_path(path: str) -> None:
    if not path:
        return
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class ColdstartHMI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1120x760")
        self.root.minsize(960, 640)

        self.files: list[str] = []
        self.running = False
        self.worker: threading.Thread | None = None
        self.ui_queue: queue.Queue[tuple] = queue.Queue()
        self.last_report: str = ""

        self._build_ui()
        self.root.after(100, self._poll_ui_queue)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.root.configure(bg="#f5f6fa")

        tk.Label(self.root, text=APP_TITLE, font=("微软雅黑", 18, "bold"),
                 bg="#f5f6fa", fg="#2c3e50").pack(pady=(16, 4))
        tk.Label(self.root, text="按产品多选数据文件加入同一分析组，一次性输出 HTML / MD / 图表（北云 / 华测）",
                 font=("微软雅黑", 10), bg="#f5f6fa", fg="#7f8c8d").pack(pady=(0, 12))

        # 产品选择
        prod_frame = ttk.LabelFrame(self.root, text="产品")
        prod_frame.pack(fill=tk.X, padx=20, pady=(0, 10))
        self.product_var = tk.StringVar(value=PRODUCTS["beiyun"].display_name)
        values = [p.display_name for p in PRODUCTS.values()]
        self.product_combo = ttk.Combobox(prod_frame, textvariable=self.product_var,
                                          state="readonly", values=values, width=64)
        self.product_combo.pack(side=tk.LEFT, padx=12, pady=10)
        self.product_combo.bind("<<ComboboxSelected>>", self._on_product_change)

        self.browse_button = ttk.Button(prod_frame, text="浏览数据文件（可多选）...", command=self.browse_files)
        self.browse_button.pack(side=tk.LEFT, padx=8)

        # 文件列表
        list_frame = ttk.LabelFrame(self.root, text="分析组（所有文件一起出一份报告）")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))

        self.file_list = tk.Listbox(list_frame, selectmode=tk.EXTENDED,
                                    font=("Consolas", 10), activestyle="none")
        yscroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=yscroll.set)
        self.file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=8)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=8)

        # 控制按钮
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill=tk.X, padx=20, pady=(0, 8))
        self.start_button = ttk.Button(ctrl, text="开始分析", command=self.start_analysis)
        self.start_button.pack(side=tk.LEFT, padx=(0, 8))
        self.remove_button = ttk.Button(ctrl, text="移除选中", command=self.remove_selected)
        self.remove_button.pack(side=tk.LEFT, padx=(0, 8))
        self.clear_button = ttk.Button(ctrl, text="清空", command=self.clear_files)
        self.clear_button.pack(side=tk.LEFT, padx=(0, 8))
        self.open_report_button = ttk.Button(ctrl, text="打开报告", command=self.open_report, state=tk.DISABLED)
        self.open_report_button.pack(side=tk.LEFT, padx=(0, 8))
        self.open_dir_button = ttk.Button(ctrl, text="打开输出目录", command=self.open_dir, state=tk.DISABLED)
        self.open_dir_button.pack(side=tk.LEFT)

        self.auto_open_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="完成后自动打开 HTML", variable=self.auto_open_var).pack(side=tk.RIGHT, padx=8)

        self.count_var = tk.StringVar(value="已选 0 个文件")
        ttk.Label(ctrl, textvariable=self.count_var, foreground="#7f8c8d").pack(side=tk.RIGHT)

        # 日志
        log_frame = ttk.LabelFrame(self.root, text="运行日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 14))
        self.log_text = tk.Text(log_frame, height=10, wrap=tk.WORD, font=("Consolas", 9),
                                state=tk.NORMAL, bg="#ffffff", relief=tk.FLAT)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=8)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=8)
        self.log_text.tag_config("info", foreground="#2c3e50")
        self.log_text.tag_config("success", foreground="#1e8449")
        self.log_text.tag_config("error", foreground="#c0392b")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W,
                  foreground="#7f8c8d").pack(fill=tk.X, padx=22, pady=(0, 10))

    # ------------------------------------------------------------------ helpers
    def _current_product(self) -> ProductConfig:
        for p in PRODUCTS.values():
            if p.display_name == self.product_var.get():
                return p
        return PRODUCTS["beiyun"]

    def _on_product_change(self, _evt=None) -> None:
        prod = self._current_product()
        if not prod.enabled:
            messagebox.showinfo(APP_TITLE, prod.note or "该产品暂不可用")
            self.product_var.set(PRODUCTS["beiyun"].display_name)

    def browse_files(self) -> None:
        prod = self._current_product()
        if not prod.enabled:
            messagebox.showinfo(APP_TITLE, prod.note or "该产品暂不可用")
            return
        paths = filedialog.askopenfilenames(
            title=f"选择{prod.display_name}数据文件",
            filetypes=list(prod.filetypes),
        )
        added = 0
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.file_list.insert(tk.END, os.path.basename(p))
                added += 1
        self._refresh_count()
        if added:
            self._log(f"添加 {added} 个文件\n", "info")

    def remove_selected(self) -> None:
        sel = sorted(self.file_list.curselection(), reverse=True)
        for i in sel:
            self.file_list.delete(i)
            del self.files[i]
        self._refresh_count()

    def clear_files(self) -> None:
        self.file_list.delete(0, tk.END)
        self.files.clear()
        self._refresh_count()

    def _refresh_count(self) -> None:
        self.count_var.set(f"已选 {len(self.files)} 个文件")

    def _log(self, msg: str, tag: str = "info") -> None:
        self.log_text.insert(tk.END, msg, tag)
        self.log_text.see(tk.END)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 4000:
            self.log_text.delete("1.0", f"{line_count - 4000}.0")

    # ------------------------------------------------------------------ run
    def start_analysis(self) -> None:
        if self.running:
            messagebox.showwarning(APP_TITLE, "分析正在进行中")
            return
        prod = self._current_product()
        if not prod.enabled:
            messagebox.showinfo(APP_TITLE, prod.note or "该产品暂不可用")
            return
        if not self.files:
            messagebox.showwarning(APP_TITLE, "请先添加数据文件")
            return

        out_dir = filedialog.askdirectory(title="选择输出目录（将生成 report.html / report.md / 图片 / summary.json）",
                                          initialdir=str(BASE_DIR / "reports"))
        if not out_dir:
            return

        auto_open = self.auto_open_var.get()
        files = list(self.files)

        self.running = True
        self.start_button.configure(state=tk.DISABLED)
        self.browse_button.configure(state=tk.DISABLED)
        self.open_report_button.configure(state=tk.DISABLED)
        self.open_dir_button.configure(state=tk.DISABLED)
        self.status_var.set("分析中...")
        self._log(f"\n===== 开始分析 {len(files)} 个文件 → {out_dir} =====\n", "info")

        self.worker = threading.Thread(target=self._run, args=(prod, files, out_dir, auto_open), daemon=True)
        self.worker.start()

    def _run(self, prod: ProductConfig, files: list[str], out_dir: str, auto_open: bool) -> None:
        try:
            script = BASE_DIR / prod.script
            cmd = [sys.executable, "-X", "utf8", str(script), out_dir, *files]
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                process = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                           errors="replace", env=env, creationflags=creationflags, bufsize=1)
            except Exception as exc:
                self.ui_queue.put(("log", f"启动分析进程失败：{exc}\n", "error"))
                self.ui_queue.put(("done", ""))
                return
            assert process.stdout is not None
            for line in process.stdout:
                self.ui_queue.put(("log", line, "info"))
            rc = process.wait()
            report = os.path.join(out_dir, "report.html")
            if rc == 0 and os.path.isfile(report):
                self.ui_queue.put(("log", f"分析完成：{report}\n", "success"))
                self.ui_queue.put(("done", report))
                if auto_open:
                    open_path(report)
            else:
                self.ui_queue.put(("log", f"分析失败，返回码 {rc}\n", "error"))
                self.ui_queue.put(("done", ""))
        finally:
            pass

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                event = self.ui_queue.get_nowait()
                if event[0] == "log":
                    _, line, tag = event
                    self._log(line, tag)
                elif event[0] == "done":
                    _, report = event
                    self.running = False
                    self.start_button.configure(state=tk.NORMAL)
                    self.browse_button.configure(state=tk.NORMAL)
                    self.status_var.set("分析完成" if report else "就绪")
                    if report:
                        self.last_report = report
                        self.open_report_button.configure(state=tk.NORMAL)
                        self.open_dir_button.configure(state=tk.NORMAL)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    def open_report(self) -> None:
        open_path(self.last_report)

    def open_dir(self) -> None:
        open_path(os.path.dirname(self.last_report))

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    ColdstartHMI().run()
