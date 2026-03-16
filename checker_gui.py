#!/usr/bin/env python3
"""GUI for editing checker_config.json and viewing check results."""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from check_ftp import run_check


def _enable_high_dpi_mode() -> None:
    """Enable DPI awareness on Windows to avoid blurry Tk rendering."""
    if sys.platform != "win32":
        return

    try:
        import ctypes

        # Best option on modern Windows: Per-Monitor V2 DPI awareness.
        per_monitor_v2 = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(per_monitor_v2):
            return
    except Exception:
        pass

    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass

    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class CheckerGui:
    STATUS_PATTERN = re.compile(r"^(.*?)\s*->\s*(已完成|未完成)\s*\(文件数:\s*(\d+)\)\s*$")
    STATUS_COL_WIDTH = 90
    COUNT_COL_WIDTH = 80
    MIN_PATH_COL_WIDTH = 180
    TREE_ROW_HEIGHT = 28
    WINDOW_MARGIN = 80
    MIN_WINDOW_W = 960
    MIN_WINDOW_H = 640

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Homework Checker GUI")
        self._configure_ui_style()
        self._set_window_geometry()

        # In frozen executable mode, use EXE directory as workspace.
        if getattr(sys, "frozen", False):
            self.workspace = Path(sys.executable).resolve().parent
        else:
            self.workspace = Path(__file__).resolve().parent
        self.config_path = self.workspace / "checker_config.json"

        self.result_queue: queue.Queue[str] = queue.Queue()
        self.is_running = False

        self._build_ui()
        self._load_config_into_form()
        self.root.after(100, self._poll_result_queue)

    def _configure_ui_style(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Treeview", rowheight=self.TREE_ROW_HEIGHT)
        if sys.platform == "win32":
            try:
                style.theme_use("vista")
            except tk.TclError:
                pass

            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                try:
                    tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
                except tk.TclError:
                    pass

    def _set_window_geometry(self) -> None:
        # Keep a consistent 3:2 window ratio across different screen sizes.
        aspect_ratio = 3 / 2
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        max_w = int(screen_w * 0.82)
        max_h = int(screen_h * 0.82)

        width = max_w
        height = int(width / aspect_ratio)
        if height > max_h:
            height = max_h
            width = int(height * aspect_ratio)

        max_width = screen_w - self.WINDOW_MARGIN
        max_height = screen_h - self.WINDOW_MARGIN
        width = min(width, max_width)
        height = min(height, max_height)
        width = max(width, min(self.MIN_WINDOW_W, max_width))
        height = max(height, min(self.MIN_WINDOW_H, max_height))

        pos_x = (screen_w - width) // 2
        pos_y = (screen_h - height) // 2
        self.root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        self.root.minsize(width, height)
        self.root.maxsize(width, height)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root)
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left = ttk.Frame(container, padding=10)
        right = ttk.Frame(container, padding=10)
        # Fixed left:right = 1:2, and non-draggable by design.
        left.place(relx=0.0, rely=0.0, relwidth=1 / 3, relheight=1.0)
        right.place(relx=1 / 3, rely=0.0, relwidth=2 / 3, relheight=1.0)

        # Left panel: config editor
        row = 0
        ttk.Label(left, text="配置文件").grid(row=row, column=0, sticky="w")
        self.config_path_var = tk.StringVar(value=str(self.config_path))
        ttk.Entry(left, textvariable=self.config_path_var, width=46).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(left, text="选择", command=self._choose_config_file).grid(row=row, column=2)

        self.host_var, row = self._add_labeled_entry(left, row, "主机")
        self.port_var, row = self._add_labeled_entry(left, row, "端口")
        self.username_var, row = self._add_labeled_entry(left, row, "用户名")
        self.password_var, row = self._add_labeled_entry(left, row, "密码", show="*")
        self.key_var, row = self._add_labeled_entry(left, row, "关键字")
        self.timeout_var, row = self._add_labeled_entry(left, row, "超时")

        row += 1
        ttk.Label(left, text="路径列表（每行一个）").grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))

        row += 1
        self.paths_text = ScrolledText(left, height=14, wrap=tk.WORD)
        self.paths_text.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(4, 0))

        row += 1
        actions = ttk.Frame(left)
        actions.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="重新加载", command=self._load_config_into_form).pack(side=tk.LEFT)
        ttk.Button(actions, text="保存配置", command=self._save_form_to_config).pack(side=tk.LEFT, padx=8)
        self.run_button = ttk.Button(actions, text="保存并检查", command=self._save_and_run)
        self.run_button.pack(side=tk.LEFT)

        left.columnconfigure(1, weight=1)
        left.rowconfigure(row - 1, weight=1)

        # Right panel: graphical result list + log
        ttk.Label(right, text="检查结果（列表）").pack(anchor="w")

        tree_frame = ttk.Frame(right)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 8))

        columns = ("status", "path", "count")
        self.result_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=14)
        self.result_tree.heading("status", text="状态")
        self.result_tree.heading("path", text="目录")
        self.result_tree.heading("count", text="文件数")
        self.result_tree.column("status", width=self.STATUS_COL_WIDTH, anchor="center", stretch=False)
        self.result_tree.column("path", width=self.MIN_PATH_COL_WIDTH, anchor="w", stretch=False)
        self.result_tree.column("count", width=self.COUNT_COL_WIDTH, anchor="center", stretch=False)
        self.result_tree.bind("<Button-1>", self._block_tree_column_resize)
        self.result_tree.bind("<B1-Motion>", self._block_tree_column_resize)

        self.result_tree.tag_configure("done", background="#eaf7ea")
        self.result_tree.tag_configure("todo", background="#fbeaea")

        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=tree_scroll.set)

        self.result_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.root.after(0, self._fit_tree_columns)

        ttk.Label(right, text="运行日志").pack(anchor="w")
        self.log_text = ScrolledText(right, wrap=tk.WORD, height=9)
        self.log_text.pack(fill=tk.BOTH, expand=False, pady=(6, 0))
        self.log_text.configure(state=tk.DISABLED)

    def _add_labeled_entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        show: str | None = None,
    ) -> tuple[tk.StringVar, int]:
        row += 1
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(8, 0))
        value = tk.StringVar()
        ttk.Entry(parent, textvariable=value, show=show).grid(
            row=row,
            column=1,
            columnspan=2,
            sticky="ew",
            padx=6,
            pady=(8, 0),
        )
        return value, row

    def _block_tree_column_resize(self, event: tk.Event) -> str | None:
        if self.result_tree.identify_region(event.x, event.y) == "separator":
            return "break"
        return None

    def _fit_tree_columns(self) -> None:
        tree_width = self.result_tree.winfo_width()
        if tree_width <= 1:
            self.root.after(30, self._fit_tree_columns)
            return

        path_width = tree_width - self.STATUS_COL_WIDTH - self.COUNT_COL_WIDTH - 4
        path_width = max(path_width, self.MIN_PATH_COL_WIDTH)
        self.result_tree.column("path", width=path_width)

    def _choose_config_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="选择 JSON 配置文件",
            filetypes=[("JSON", "*.json"), ("All Files", "*.*")],
            initialdir=str(self.workspace),
        )
        if not file_path:
            return
        self.config_path_var.set(file_path)
        self._load_config_into_form()

    def _load_config_into_form(self) -> None:
        path = Path(self.config_path_var.get().strip() or self.config_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("读取失败", f"无法读取配置: {exc}")
            return

        ftp = data.get("ftp", {})
        self.host_var.set(str(ftp.get("host", "")))
        self.port_var.set(str(ftp.get("port", 21)))
        self.username_var.set(str(ftp.get("username", "")))
        self.password_var.set(str(ftp.get("password", "")))
        self.key_var.set(str(data.get("key", "")))
        self.timeout_var.set(str(data.get("timeout", 15)))

        paths = data.get("paths", [])
        self.paths_text.delete("1.0", tk.END)
        if isinstance(paths, list):
            self.paths_text.insert("1.0", "\n".join(str(p) for p in paths))

        self._append_result(f"已加载配置: {path}\n")

    def _build_config_from_form(self) -> dict:
        raw_paths = self.paths_text.get("1.0", tk.END).splitlines()
        paths = [line.strip() for line in raw_paths if line.strip()]

        return {
            "ftp": {
                "host": self.host_var.get().strip(),
                "port": int(self.port_var.get().strip()),
                "username": self.username_var.get().strip(),
                "password": self.password_var.get().strip(),
            },
            "key": self.key_var.get().strip(),
            "paths": paths,
            "timeout": int(self.timeout_var.get().strip()),
        }

    def _save_form_to_config(self) -> bool:
        path = Path(self.config_path_var.get().strip() or self.config_path)
        try:
            data = self._build_config_from_form()
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._append_result(f"配置已保存: {path}\n")
            return True
        except ValueError:
            messagebox.showerror("输入错误", "Port 和 Timeout 必须是整数")
            return False
        except Exception as exc:
            messagebox.showerror("保存失败", f"无法保存配置: {exc}")
            return False

    def _save_and_run(self) -> None:
        if self.is_running:
            return
        if not self._save_form_to_config():
            return

        config_path = Path(self.config_path_var.get().strip() or self.config_path)
        self._append_result("开始执行检查...\n")
        self._clear_result_list()
        self.is_running = True
        self.run_button.configure(state=tk.DISABLED)

        thread = threading.Thread(target=self._run_checker_subprocess, args=(config_path,), daemon=True)
        thread.start()

    def _run_checker_subprocess(self, config_path: Path) -> None:
        try:
            output_buf = StringIO()
            err_buf = StringIO()
            with redirect_stdout(output_buf), redirect_stderr(err_buf):
                exit_code = run_check(config_path, timeout_override=None)

            output = output_buf.getvalue() + err_buf.getvalue()
            self.result_queue.put(output + f"\n[exit_code={exit_code}]\n")
        except Exception as exc:
            self.result_queue.put(f"执行失败: {exc}\n")

    def _poll_result_queue(self) -> None:
        try:
            while True:
                payload = self.result_queue.get_nowait()
                self._render_result_list(payload)
                self._append_result(payload)
                self.is_running = False
                self.run_button.configure(state=tk.NORMAL)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_result_queue)

    def _append_result(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_result_list(self) -> None:
        self.result_tree.delete(*self.result_tree.get_children())

    def _extract_status_rows(self, output: str) -> list[tuple[str, str, int]]:
        rows: list[tuple[str, str, int]] = []
        for line in output.splitlines():
            match = self.STATUS_PATTERN.match(line.strip())
            if not match:
                continue
            folder_path = match.group(1)
            status = match.group(2)
            file_count = int(match.group(3))
            rows.append((folder_path, status, file_count))
        return rows

    def _render_result_list(self, output: str) -> None:
        rows = self._extract_status_rows(output)
        self._clear_result_list()
        if not rows:
            return

        # Put unfinished rows before completed rows, then sort by path.
        rows.sort(key=lambda row: (row[1] == "已完成", row[0]))

        for folder_path, status, file_count in rows:
            tag = "done" if status == "已完成" else "todo"
            self.result_tree.insert(
                "",
                tk.END,
                values=(status, folder_path, file_count),
                tags=(tag,),
            )


def main() -> None:
    _enable_high_dpi_mode()
    root = tk.Tk()
    CheckerGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
