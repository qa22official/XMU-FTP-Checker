#!/usr/bin/env python3
"""GUI for editing checker_config.json and viewing check results."""

from __future__ import annotations

import json
import ftplib
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

from check_ftp import load_app_config, run_check


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
    IGNORED_STATUS = "已忽略"
    STATUS_COL_WIDTH = 90
    COUNT_COL_WIDTH = 80
    MIN_PATH_COL_WIDTH = 180
    TREE_ROW_HEIGHT = 28
    WINDOW_MARGIN = 80
    MIN_WINDOW_W = 960
    MIN_WINDOW_H = 640
    UI_STATE_KEY = "ui_state"
    UI_RESULT_ROWS_KEY = "result_rows"

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
        self.ignored_original_status_by_path: dict[str, str] = {}

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
        self.run_button = ttk.Button(actions, text="更新结果", command=self._save_and_run)
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
        self.result_tree.bind("<Button-3>", self._show_result_item_menu)

        self.result_tree.tag_configure("done", background="#eaf7ea")
        self.result_tree.tag_configure("todo", background="#fbeaea")
        self.result_tree.tag_configure("ignored", background="#efefef", foreground="#666666")

        self.result_item_menu = tk.Menu(self.root, tearoff=0)
        self.result_item_menu.add_command(label="忽略", command=self._toggle_ignore_menu_target_item)
        self.result_item_menu.add_command(label="选择文件并提交", command=self._choose_file_and_submit_for_item)
        self._menu_target_iid: str | None = None

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

    def _show_result_item_menu(self, event: tk.Event) -> str | None:
        iid = self.result_tree.identify_row(event.y)
        if not iid:
            return None

        values = self.result_tree.item(iid, "values")
        current_status = str(values[0]) if values else ""
        ignore_label = "取消忽略" if current_status == self.IGNORED_STATUS else "忽略"
        self.result_item_menu.entryconfigure(0, label=ignore_label)

        self.result_tree.selection_set(iid)
        self.result_tree.focus(iid)
        self._menu_target_iid = iid
        try:
            self.result_item_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.result_item_menu.grab_release()
        return "break"

    def _status_sort_rank(self, status: str) -> int:
        if status == self.IGNORED_STATUS:
            return 2
        if status == "已完成":
            return 1
        return 0

    def _status_to_tag(self, status: str) -> str:
        if status == self.IGNORED_STATUS:
            return "ignored"
        if status == "已完成":
            return "done"
        return "todo"

    def _resort_tree_items(self) -> None:
        rows: list[tuple[str, str, str, int]] = []
        for iid in self.result_tree.get_children():
            status, folder_path, file_count = self.result_tree.item(iid, "values")
            rows.append((iid, str(status), str(folder_path), int(file_count)))

        rows.sort(key=lambda row: (self._status_sort_rank(row[1]), row[2]))
        for index, (iid, _status, _folder_path, _file_count) in enumerate(rows):
            self.result_tree.move(iid, "", index)

    def _toggle_ignore_menu_target_item(self) -> None:
        if not self._menu_target_iid:
            return

        values = list(self.result_tree.item(self._menu_target_iid, "values"))
        if len(values) != 3:
            return

        current_status = str(values[0])
        folder_path = str(values[1]).strip()

        if current_status == self.IGNORED_STATUS:
            restore_status = self.ignored_original_status_by_path.get(folder_path, "未完成")
            values[0] = restore_status
        else:
            self.ignored_original_status_by_path[folder_path] = current_status
            values[0] = self.IGNORED_STATUS

        self.result_tree.item(
            self._menu_target_iid,
            values=tuple(values),
            tags=(self._status_to_tag(str(values[0])),),
        )
        self._resort_tree_items()
        self._save_result_state_to_config()

    def _ask_submit_confirmation(self, file_name: str) -> bool:
        result = {"confirmed": False}
        dialog = tk.Toplevel(self.root)
        dialog.title("确认提交")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        ttk.Label(dialog, text=f"已选择文件：{file_name}").pack(padx=16, pady=(14, 6), anchor="w")
        ttk.Label(dialog, text="是否提交该文件？").pack(padx=16, pady=(0, 12), anchor="w")

        buttons = ttk.Frame(dialog)
        buttons.pack(fill=tk.X, padx=16, pady=(0, 14))

        def on_cancel() -> None:
            result["confirmed"] = False
            dialog.destroy()

        def on_confirm() -> None:
            result["confirmed"] = True
            dialog.destroy()

        ttk.Button(buttons, text="取消", command=on_cancel).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="确认", command=on_confirm).pack(side=tk.RIGHT, padx=(0, 8))

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        dialog.grab_set()
        dialog.wait_window()
        return bool(result["confirmed"])

    def _choose_file_and_submit_for_item(self) -> None:
        if not self._menu_target_iid:
            return

        row_values = self.result_tree.item(self._menu_target_iid, "values")
        if len(row_values) != 3:
            messagebox.showerror("提交失败", "无法识别当前条目的远程目录")
            return
        remote_path = str(row_values[1]).strip()
        if not remote_path:
            messagebox.showerror("提交失败", "当前条目缺少远程目录路径")
            return

        file_path = filedialog.askopenfilename(
            title="选择要提交的文件",
            initialdir=str(self.workspace),
            filetypes=[("All Files", "*.*")],
        )
        if not file_path:
            return

        local_file = Path(file_path)
        file_name = local_file.name
        if not self._ask_submit_confirmation(file_name):
            return

        try:
            self._upload_file_to_remote_path(local_file, remote_path)
            messagebox.showinfo("提交结果", f"提交成功：{file_name}\n目标目录：{remote_path}")
            self._append_result(f"提交成功: {file_name} -> {remote_path}\n")
        except Exception as exc:
            messagebox.showerror("提交失败", f"上传失败: {exc}")
            self._append_result(f"提交失败: {file_name} -> {remote_path} ({exc})\n")

    def _upload_file_to_remote_path(self, local_file: Path, remote_path: str) -> None:
        config_path = self._get_active_config_path()
        app_cfg = load_app_config(config_path)
        cfg = app_cfg.ftp

        ftp = ftplib.FTP(encoding="gbk")
        try:
            ftp.connect(cfg.host, cfg.port, timeout=app_cfg.timeout)
            ftp.login(cfg.username, cfg.password)
            ftp.cwd(remote_path)

            with local_file.open("rb") as fp:
                ftp.storbinary(f"STOR {local_file.name}", fp)
        finally:
            try:
                ftp.quit()
            except Exception:
                pass

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

    def _get_active_config_path(self) -> Path:
        return Path(self.config_path_var.get().strip() or self.config_path)

    def _read_config_data(self, path: Path) -> dict:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是对象")
        return data

    def _write_config_data(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _collect_tree_rows(self) -> list[dict[str, str | int]]:
        rows: list[dict[str, str | int]] = []
        for iid in self.result_tree.get_children():
            status, folder_path, file_count = self.result_tree.item(iid, "values")
            rows.append(
                {
                    "path": str(folder_path),
                    "status": str(status),
                    "count": int(file_count),
                }
            )
        return rows

    def _apply_result_rows_to_tree(self, rows: list[tuple[str, str, int]]) -> None:
        self._clear_result_list()
        if not rows:
            return

        rows.sort(key=lambda row: (self._status_sort_rank(row[1]), row[0]))
        for folder_path, status, file_count in rows:
            self.result_tree.insert(
                "",
                tk.END,
                values=(status, folder_path, file_count),
                tags=(self._status_to_tag(status),),
            )

    def _get_ignored_paths_from_config_data(self, data: dict) -> set[str]:
        state = data.get(self.UI_STATE_KEY)
        if not isinstance(state, dict):
            return set()

        persisted_rows = state.get(self.UI_RESULT_ROWS_KEY)
        if not isinstance(persisted_rows, list):
            return set()

        ignored_paths: set[str] = set()
        for row in persisted_rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("status", "")) != self.IGNORED_STATUS:
                continue
            row_path = str(row.get("path", "")).strip()
            if row_path:
                ignored_paths.add(row_path)
        return ignored_paths

    def _save_result_state_to_config(self) -> None:
        path = self._get_active_config_path()
        try:
            data = self._read_config_data(path)
        except Exception:
            return

        state = data.get(self.UI_STATE_KEY)
        if not isinstance(state, dict):
            state = {}
            data[self.UI_STATE_KEY] = state
        state[self.UI_RESULT_ROWS_KEY] = self._collect_tree_rows()

        try:
            self._write_config_data(path, data)
        except Exception:
            return

    def _restore_result_state_from_config(self, data: dict) -> None:
        state = data.get(self.UI_STATE_KEY)
        if not isinstance(state, dict):
            self._clear_result_list()
            return

        persisted_rows = state.get(self.UI_RESULT_ROWS_KEY)
        if not isinstance(persisted_rows, list):
            self._clear_result_list()
            return

        rows: list[tuple[str, str, int]] = []
        for row in persisted_rows:
            if not isinstance(row, dict):
                continue

            row_path = str(row.get("path", "")).strip()
            status = str(row.get("status", "")).strip()
            raw_count = row.get("count", 0)

            if not row_path:
                continue
            if status not in ("已完成", "未完成", self.IGNORED_STATUS):
                continue

            try:
                file_count = int(raw_count)
            except (TypeError, ValueError):
                file_count = 0

            rows.append((row_path, status, file_count))

        self._apply_result_rows_to_tree(rows)

    def _load_config_into_form(self) -> None:
        path = self._get_active_config_path()
        try:
            data = self._read_config_data(path)
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

        self._restore_result_state_from_config(data)

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
        path = self._get_active_config_path()
        try:
            data = self._build_config_from_form()
            data[self.UI_STATE_KEY] = {self.UI_RESULT_ROWS_KEY: self._collect_tree_rows()}
            self._write_config_data(path, data)
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
        self.ignored_original_status_by_path = {folder_path: status for folder_path, status, _ in rows}
        try:
            config_data = self._read_config_data(self._get_active_config_path())
            ignored_paths = self._get_ignored_paths_from_config_data(config_data)
        except Exception:
            ignored_paths = set()

        if ignored_paths:
            rows = [
                (
                    folder_path,
                    self.IGNORED_STATUS if folder_path in ignored_paths else status,
                    file_count,
                )
                for folder_path, status, file_count in rows
            ]

        self._apply_result_rows_to_tree(rows)
        if not rows:
            self._save_result_state_to_config()
            return

        self._save_result_state_to_config()


def main() -> None:
    _enable_high_dpi_mode()
    root = tk.Tk()
    CheckerGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
