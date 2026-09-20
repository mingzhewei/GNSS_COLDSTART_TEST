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
    magic: str = ""        # 必须含有的报文特征（如 #BESTGNSSPOSA）
    forbid: str = ""       # 必须不含的报文特征（如华测文件不应有 #BESTGNSSPOSA）


# 产品注册表：华测数据到位后，把 enabled 改为 True 并补充 script 即可
PRODUCTS: dict[str, ProductConfig] = {
    "beiyun": ProductConfig(
        key="beiyun",
        display_name="北云 BY（UG016 组合惯导，COM1 ASCII .log）",
        script="BY_coldstart.py",
        filetypes=(("北云 COM1 日志", "*.log"), ("所有文件", "*.*")),
        color="#1a5276",
        enabled=True,
        magic="#BESTGNSSPOSA",
    ),
    "huace": ProductConfig(
        key="huace",
        display_name="华测 HUACE（M720，COM1 ASCII #BESTPOSA .log）",
        script="HUACE_coldstart.py",
        filetypes=(("华测 COM1 日志", "*.log"), ("所有文件", "*.*")),
        color="#b03a2e",
        enabled=True,
        magic="#BESTPOSA",
        forbid="#BESTGNSSPOSA",   # 北云文件同时含 BESTGNSSPOSA，用它区分
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

        self.files: list[tuple[str, str]] = []   # (文件路径, 产品key)
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
        tk.Label(self.root, text="按产品多选数据文件加入同一分析组，一次性输出 HTML / MD / 图表；连续录制文件自动按启动切片逐段分析",
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

        # 文件列表（左=文件名，右=产品类型）
        list_frame = ttk.LabelFrame(self.root, text="分析组（同类型文件一起出一份报告；不同类型分别出报告）")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))

        cols = ("file", "prod")
        self.file_list = ttk.Treeview(list_frame, columns=cols, show="headings",
                                      selectmode="extended", height=10)
        self.file_list.heading("file", text="文件名")
        self.file_list.heading("prod", text="产品类型")
        self.file_list.column("file", width=560, anchor=tk.W)
        self.file_list.column("prod", width=260, anchor=tk.W)
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

        # 录制模式：独立冷启动包（默认）/ 一次性录制 N 次冷启动（切片）
        self.split_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="一次性录制了 N 次冷启动（自动切片）",
                        variable=self.split_var).pack(side=tk.RIGHT, padx=8)

        # 场景：室外冷启动（默认）/ 室内冷启动（需北云+华测配对，对齐共同 t0，出联合报告）
        self.scene_var = tk.StringVar(value="outdoor")
        scene_frame = ttk.LabelFrame(self.root, text="冷启动场景（必选）")
        scene_frame.pack(fill=tk.X, padx=20, pady=(0, 8))
        ttk.Radiobutton(scene_frame, text="室外冷启动（t=0 为启动时刻，全程计入）",
                        value="outdoor", variable=self.scene_var).pack(side=tk.LEFT, padx=12, pady=6)
        ttk.Radiobutton(scene_frame, text="室内冷启动（北云+华测配对，取两家最早拿时标时刻为共同 t0，出联合报告）",
                        value="indoor", variable=self.scene_var).pack(side=tk.LEFT, padx=12, pady=6)

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

    def _check_file_type(self, path: str, prod: ProductConfig) -> bool:
        """用报文特征校验文件是否属于所选产品（读首条匹配行，最多扫 2MB）。
        返回 True=匹配 / False=不匹配。"""
        try:
            with open(path, 'rb') as fp:
                head = fp.read(2 * 1024 * 1024)
        except OSError:
            return False
        if prod.magic and prod.magic.encode('ascii') not in head:
            return False
        if prod.forbid and prod.forbid.encode('ascii') in head:
            return False
        return True

    def browse_files(self) -> None:
        prod = self._current_product()
        if not prod.enabled:
            messagebox.showinfo(APP_TITLE, prod.note or "该产品暂不可用")
            return
        paths = filedialog.askopenfilenames(
            title=f"选择{prod.display_name}数据文件",
            filetypes=list(prod.filetypes),
        )
        added, skipped = 0, 0
        for path in paths:
            if any(f[0] == path for f in self.files):
                continue
            if not self._check_file_type(path, prod):
                skipped += 1
                self._log(f"跳过（类型不符，未检出 {prod.magic}）：{os.path.basename(path)}\n", "error")
                continue
            self.files.append((path, prod.key))
            self.file_list.insert("", tk.END, values=(os.path.basename(path), prod.display_name))
            added += 1
        self._refresh_count()
        if added:
            self._log(f"添加 {added} 个{prod.display_name}文件\n", "info")
        if skipped:
            messagebox.showwarning(APP_TITLE,
                f"{skipped} 个文件与所选类型「{prod.display_name}」不符（未检出 {prod.magic} 报文），已跳过。\n"
                "如需分析，请先将下拉菜单切换到对应产品再添加。")

    def remove_selected(self) -> None:
        items = self.file_list.selection()
        if not items:
            return
        # 按索引删除（Treeview 行序与 self.files 一致）
        idxs = sorted((self.file_list.index(it) for it in items), reverse=True)
        for i in idxs:
            self.file_list.delete(self.file_list.get_children()[i])
            del self.files[i]
        self._refresh_count()

    def clear_files(self) -> None:
        for it in self.file_list.get_children():
            self.file_list.delete(it)
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
        split = self.split_var.get()
        indoor = (self.scene_var.get() == "indoor")
        # 按产品分组（同类型一起出一份报告；不同类型各出一份）
        groups: dict[str, list[str]] = {}
        for path, key in self.files:
            groups.setdefault(key, []).append(path)

        if indoor and not ("beiyun" in groups and "huace" in groups):
            messagebox.showwarning(APP_TITLE,
                "室内冷启动需要北云和华测的数据配对分析。\n"
                "请先将下拉菜单分别切到北云、华测，各添加至少 1 个数据文件。")
            return

        self.running = True
        self.start_button.configure(state=tk.DISABLED)
        self.browse_button.configure(state=tk.DISABLED)
        self.open_report_button.configure(state=tk.DISABLED)
        self.open_dir_button.configure(state=tk.DISABLED)
        self.status_var.set("分析中...")
        self._log(f"\n===== 开始分析 {len(self.files)} 个文件（{len(groups)} 种产品）→ {out_dir} =====\n", "info")
        self._log("场景：" + ("室内冷启动（配对对齐共同 t0）" if indoor else "室外冷启动") + "；" +
                  ("一次性录制 N 次冷启动（自动切片）" if split else "独立冷启动包（逐文件分析）") + "\n", "info")

        self.worker = threading.Thread(target=self._run, args=(groups, out_dir, auto_open, split, indoor), daemon=True)
        self.worker.start()

    def _run(self, groups: dict, out_dir: str, auto_open: bool, split: bool, indoor: bool = False) -> None:
        reports = []
        try:
            if indoor:
                by_files = groups.get("beiyun", [])
                hc_files = groups.get("huace", [])
                if len(by_files) != 1 or len(hc_files) != 1:
                    self.ui_queue.put(("log", "室内模式当前要求北云、华测各选 1 个连续录制文件（各含 N 次冷启动，按序号配对）。\n", "error"))
                    self.ui_queue.put(("done", ""))
                    return
                script = BASE_DIR / "indoor_coldstart.py"
                cmd = [sys.executable, "-X", "utf8", str(script), out_dir]
                if split:
                    cmd.append("--split")
                cmd.extend([by_files[0], hc_files[0]])
                self.ui_queue.put(("log", f"--- 室内联合分析：北云 {os.path.basename(by_files[0])} + 华测 {os.path.basename(hc_files[0])} ---\n", "info"))
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
                    reports.append(report)
                    if auto_open:
                        open_path(report)
                else:
                    self.ui_queue.put(("log", f"室内联合分析失败，返回码 {rc}\n", "error"))
                self.ui_queue.put(("done", reports[-1] if reports else ""))
                return
            for key, files in groups.items():
                prod = PRODUCTS.get(key)
                if prod is None:
                    self.ui_queue.put(("log", f"未知产品 {key}，跳过\n", "error"))
                    continue
                # 多产品时按产品名建子目录，单产品时直接用 out_dir
                sub_out = out_dir if len(groups) == 1 else os.path.join(out_dir, prod.key)
                os.makedirs(sub_out, exist_ok=True)
                script = BASE_DIR / prod.script
                cmd = [sys.executable, "-X", "utf8", str(script), sub_out]
                if split:
                    cmd.append("--split")
                cmd.extend(files)
                self.ui_queue.put(("log", f"--- {prod.display_name}：{len(files)} 个文件 → {sub_out} ---\n", "info"))
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
                    continue
                assert process.stdout is not None
                for line in process.stdout:
                    self.ui_queue.put(("log", line, "info"))
                rc = process.wait()
                report = os.path.join(sub_out, "report.html")
                if rc == 0 and os.path.isfile(report):
                    self.ui_queue.put(("log", f"分析完成：{report}\n", "success"))
                    reports.append(report)
                    if auto_open:
                        open_path(report)
                else:
                    self.ui_queue.put(("log", f"{prod.display_name} 分析失败，返回码 {rc}\n", "error"))
        finally:
            self.ui_queue.put(("done", reports[-1] if reports else ""))

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
