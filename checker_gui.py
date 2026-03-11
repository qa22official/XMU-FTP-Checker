#!/usr/bin/env python3
"""GUI for editing checker_config.json and viewing check results."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from check_ftp import run_check


class CheckerGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Homework Checker GUI")
        self._set_window_geometry()

        # In frozen executable mode, use EXE directory as workspace.
        if getattr(sys, "frozen", False):
            self.workspace = Path(sys.executable).resolve().parent
        else:
            self.workspace = Path(__file__).resolve().parent
        self.config_path = self.workspace / "checker_config.json"

        self.result_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.is_running = False

        self._build_ui()
        self._load_config_into_form()
        self.root.after(100, self._poll_result_queue)

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

        min_w = 960
        min_h = 640
        width = min(width, screen_w - 80)
        height = min(height, screen_h - 80)
        width = max(width, min(min_w, screen_w - 80))
        height = max(height, min(min_h, screen_h - 80))

        pos_x = (screen_w - width) // 2
        pos_y = (screen_h - height) // 2
        self.root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        self.root.minsize(width, height)
        self.root.maxsize(width, height)

    def _build_ui(self) -> None:
        container = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left = ttk.Frame(container, padding=10)
        right = ttk.Frame(container, padding=10)
        container.add(left, weight=2)
        container.add(right, weight=3)

        # Left panel: config editor
        row = 0
        ttk.Label(left, text="配置文件").grid(row=row, column=0, sticky="w")
        self.config_path_var = tk.StringVar(value=str(self.config_path))
        ttk.Entry(left, textvariable=self.config_path_var, width=46).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(left, text="选择", command=self._choose_config_file).grid(row=row, column=2)

        row += 1
        ttk.Label(left, text="Host").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.host_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.host_var).grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))

        row += 1
        ttk.Label(left, text="Port").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.port_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.port_var).grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))

        row += 1
        ttk.Label(left, text="Username").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.username_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.username_var).grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))

        row += 1
        ttk.Label(left, text="Password").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.password_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.password_var, show="*").grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))

        row += 1
        ttk.Label(left, text="Base Path").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.base_path_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.base_path_var).grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))

        row += 1
        ttk.Label(left, text="Key").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.key_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.key_var).grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))

        row += 1
        ttk.Label(left, text="Timeout").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.timeout_var = tk.StringVar()
        ttk.Entry(left, textvariable=self.timeout_var).grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))

        row += 1
        ttk.Label(left, text="Paths (每行一个)").grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))

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

        # Right panel: output
        ttk.Label(right, text="检查结果").pack(anchor="w")
        self.result_text = ScrolledText(right, wrap=tk.WORD)
        self.result_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.result_text.configure(state=tk.DISABLED)

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
        self.base_path_var.set(str(ftp.get("base_path", "")))
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
                "base_path": self.base_path_var.get().strip(),
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
            self.result_queue.put(("done", output + f"\n[exit_code={exit_code}]\n"))
        except Exception as exc:
            self.result_queue.put(("done", f"执行失败: {exc}\n"))

    def _poll_result_queue(self) -> None:
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "done":
                    self._append_result(payload)
                    self.is_running = False
                    self.run_button.configure(state=tk.NORMAL)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_result_queue)

    def _append_result(self, text: str) -> None:
        self.result_text.configure(state=tk.NORMAL)
        self.result_text.insert(tk.END, text)
        self.result_text.see(tk.END)
        self.result_text.configure(state=tk.DISABLED)


def main() -> None:
    root = tk.Tk()
    CheckerGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
