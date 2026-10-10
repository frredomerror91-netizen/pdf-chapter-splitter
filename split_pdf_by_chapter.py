from __future__ import annotations

import os
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from pdf_chapter_splitter_core import (
    ChapterRange,
    analyze_pdf,
    build_ranges,
    recompute_ranges,
    split_pdf,
)


APP_BG = "#F3F6FA"
CARD_BG = "#FFFFFF"
INK = "#172033"
MUTED = "#65758B"
BORDER = "#D8E2EC"
ACCENT = "#2563EB"
ACCENT_DARK = "#1D4ED8"
ACCENT_SOFT = "#E8F0FF"
SUCCESS = "#0F766E"
WARNING = "#B45309"
DANGER = "#B42318"
ROW_ALT = "#F8FAFC"
APP_VERSION = "1.1.0"

MODE_LABELS = {
    "auto": "自动（书签优先）",
    "bookmark": "仅 PDF 书签",
    "text": "仅页面标题",
}
SOURCE_LABELS = {
    "bookmark": "PDF 书签",
    "text": "页面标题",
    "manual": "手动",
    "generated": "自动补齐",
}


class PdfChapterSplitterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PDF 章节拆分器")
        self.root.geometry("1120x720")
        self.root.minsize(960, 620)
        self.root.configure(bg=APP_BG)

        self.source_path: Path | None = None
        self.total_pages = 0
        self.ranges: list[ChapterRange] = []
        self._cell_editor: ttk.Entry | None = None
        self._busy = False

        self.source_var = tk.StringVar(value="还没有选择 PDF")
        self.output_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="auto")
        self.open_after_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="选择一份 PDF，工具会先读取目录，再尝试识别页面标题。")
        self.status_tone = "normal"
        self.page_count_var = tk.StringVar(value="—")
        self.range_count_var = tk.StringVar(value="—")
        self.method_var = tk.StringVar(value="等待分析")
        self.notice_var = tk.StringVar(value="")

        self._configure_styles()
        self._build_widgets()
        self._refresh_stats()

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background=APP_BG)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("TLabel", background=APP_BG, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=APP_BG, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("CardTitle.TLabel", background=CARD_BG, foreground=INK, font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("CardMuted.TLabel", background=CARD_BG, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("StatValue.TLabel", background=CARD_BG, foreground=INK, font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("StatLabel.TLabel", background=CARD_BG, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(12, 7))
        style.configure("Accent.TButton", background=ACCENT, foreground="#FFFFFF", borderwidth=0, padding=(14, 9))
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#AFC4ED")])
        style.configure("Ghost.TButton", background=CARD_BG, foreground=INK, bordercolor=BORDER, padding=(11, 7))
        style.map("Ghost.TButton", background=[("active", ACCENT_SOFT), ("disabled", "#F1F4F8")])
        style.configure("Danger.TButton", background=CARD_BG, foreground=DANGER, bordercolor=BORDER, padding=(11, 7))
        style.map("Danger.TButton", background=[("active", "#FDECEC"), ("disabled", "#F1F4F8")])
        style.configure("Mode.TRadiobutton", background=CARD_BG, foreground=INK, padding=(7, 6), font=("Microsoft YaHei UI", 9))
        style.map("Mode.TRadiobutton", background=[("selected", ACCENT_SOFT), ("active", "#F4F7FF")])
        style.configure("Card.TEntry", fieldbackground=CARD_BG, foreground=INK, padding=(8, 6))
        style.configure("Table.Treeview", background=CARD_BG, fieldbackground=CARD_BG, foreground=INK, rowheight=36, borderwidth=0, font=("Microsoft YaHei UI", 9))
        style.configure("Table.Treeview.Heading", background="#EAF0F6", foreground=MUTED, relief="flat", font=("Microsoft YaHei UI", 9, "bold"), padding=(8, 9))
        style.map("Table.Treeview", background=[("selected", "#DCE8FF")], foreground=[("selected", INK)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#E6EDF5", borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("TCheckbutton", background=CARD_BG, foreground=INK, font=("Microsoft YaHei UI", 9))
        style.map("TCheckbutton", background=[("active", CARD_BG)])

    def _make_card(self, parent: tk.Misc, **grid_options: object) -> tk.Frame:
        card = tk.Frame(
            parent,
            bg=CARD_BG,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            highlightthickness=1,
            bd=0,
        )
        card.grid(**grid_options)
        return card

    def _build_widgets(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = tk.Frame(self.root, bg=APP_BG)
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 18))
        header.columnconfigure(1, weight=1)

        brand = tk.Label(
            header,
            text="PDF",
            bg=ACCENT,
            fg="#FFFFFF",
            font=("Microsoft YaHei UI", 11, "bold"),
            width=5,
            height=2,
        )
        brand.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        tk.Label(
            header,
            text="PDF 章节拆分器",
            bg=APP_BG,
            fg=INK,
            font=("Microsoft YaHei UI", 23, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        tk.Label(
            header,
            text="从目录到独立章节文件，把长资料整理成可复用的阅读单元。",
            bg=APP_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 10),
            anchor="w",
        ).grid(row=1, column=1, sticky="w", pady=(3, 0))
        tk.Label(
            header,
            text=f"v{APP_VERSION} · 本地处理 · 原文件不覆盖",
            bg=APP_BG,
            fg=SUCCESS,
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="e",
        ).grid(row=0, column=2, rowspan=2, sticky="e", padx=(16, 0))

        body = ttk.Frame(self.root, style="App.TFrame")
        body.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 24))
        body.columnconfigure(0, weight=0, minsize=310)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_sidebar(body)
        self._build_workspace(body)

    def _build_sidebar(self, parent: ttk.Frame) -> None:
        sidebar = ttk.Frame(parent, style="App.TFrame")
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        sidebar.columnconfigure(0, weight=1)

        source_card = self._make_card(sidebar, row=0, column=0, sticky="ew", pady=(0, 14))
        source_card.columnconfigure(0, weight=1)
        self._card_heading(source_card, "01 选择资料", "选择需要拆分的长 PDF。")
        self.source_label = tk.Label(
            source_card,
            textvariable=self.source_var,
            bg=CARD_BG,
            fg=INK,
            font=("Microsoft YaHei UI", 9),
            justify="left",
            anchor="w",
            wraplength=268,
        )
        self.source_label.grid(row=2, column=0, sticky="ew", padx=16, pady=(2, 10))
        source_actions = tk.Frame(source_card, bg=CARD_BG)
        source_actions.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        ttk.Button(source_actions, text="选择 PDF", style="Accent.TButton", command=self.choose_pdf).pack(side="left")
        ttk.Button(source_actions, text="打开", style="Ghost.TButton", command=self.open_source).pack(side="left", padx=(8, 0))

        mode_card = self._make_card(sidebar, row=1, column=0, sticky="ew", pady=(0, 14))
        mode_card.columnconfigure(0, weight=1)
        self._card_heading(mode_card, "02 识别方式", "自动模式会优先读取 PDF 目录。")
        modes = tk.Frame(mode_card, bg=CARD_BG)
        modes.grid(row=2, column=0, sticky="ew", padx=12, pady=(1, 8))
        for label, value in MODE_LABELS.items():
            ttk.Radiobutton(
                modes,
                text=label,
                value=label,
                variable=self.mode_var,
                style="Mode.TRadiobutton",
            ).pack(fill="x", pady=2)
        tk.Label(
            mode_card,
            text="扫描版如果没有文字层，请使用 PDFgear OCR，或在右侧手动添加章节起始页。",
            bg=CARD_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=268,
        ).grid(row=3, column=0, sticky="ew", padx=16, pady=(2, 16))

        output_card = self._make_card(sidebar, row=2, column=0, sticky="ew")
        output_card.columnconfigure(0, weight=1)
        self._card_heading(output_card, "03 输出设置", "每个章节会生成一个独立 PDF。")
        ttk.Entry(output_card, textvariable=self.output_var, style="Card.TEntry").grid(
            row=2, column=0, sticky="ew", padx=16, pady=(1, 8)
        )
        ttk.Button(output_card, text="选择输出文件夹", style="Ghost.TButton", command=self.choose_output).grid(
            row=3, column=0, sticky="w", padx=16, pady=(0, 7)
        )
        ttk.Checkbutton(
            output_card,
            text="导出完成后打开文件夹",
            variable=self.open_after_var,
        ).grid(row=4, column=0, sticky="w", padx=12, pady=(0, 14))

        self.analyze_button = ttk.Button(
            sidebar,
            text="分析章节",
            style="Accent.TButton",
            command=self.analyze,
        )
        self.analyze_button.grid(row=3, column=0, sticky="ew", pady=(16, 0))

    def _card_heading(self, card: tk.Frame, title: str, subtitle: str) -> None:
        tk.Label(
            card,
            text=title,
            bg=CARD_BG,
            fg=INK,
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(15, 2))
        tk.Label(
            card,
            text=subtitle,
            bg=CARD_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

    def _build_workspace(self, parent: ttk.Frame) -> None:
        workspace = ttk.Frame(parent, style="App.TFrame")
        workspace.grid(row=0, column=1, sticky="nsew")
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(1, weight=1)

        stats = tk.Frame(workspace, bg=APP_BG)
        stats.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        for column in range(3):
            stats.columnconfigure(column, weight=1)
        self._stat_card(stats, 0, "PDF 页数", self.page_count_var)
        self._stat_card(stats, 1, "输出段数", self.range_count_var)
        self._stat_card(stats, 2, "检测方式", self.method_var)

        review_card = self._make_card(workspace, row=1, column=0, sticky="nsew")
        review_card.columnconfigure(0, weight=1)
        review_card.rowconfigure(2, weight=1)

        top = tk.Frame(review_card, bg=CARD_BG)
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(15, 0))
        top.columnconfigure(0, weight=1)
        tk.Label(
            top,
            text="章节边界",
            bg=CARD_BG,
            fg=INK,
            font=("Microsoft YaHei UI", 13, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        actions = tk.Frame(top, bg=CARD_BG)
        actions.grid(row=0, column=1, sticky="e")
        self.add_button = ttk.Button(actions, text="添加章节", style="Ghost.TButton", command=self.add_chapter)
        self.add_button.pack(side="left")
        self.delete_button = ttk.Button(actions, text="删除所选", style="Danger.TButton", command=self.delete_selected)
        self.delete_button.pack(side="left", padx=(8, 0))

        self.notice_label = tk.Label(
            review_card,
            textvariable=self.notice_var,
            bg=CARD_BG,
            fg=WARNING,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            justify="left",
        )
        self.notice_label.grid(row=1, column=0, sticky="ew", padx=16, pady=(5, 8))

        table_frame = tk.Frame(review_card, bg=CARD_BG)
        table_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 12))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = ("index", "title", "start", "end", "source")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", style="Table.Treeview")
        headings = {
            "index": "序号",
            "title": "章节标题 · 双击编辑",
            "start": "起始页 · 双击编辑",
            "end": "结束页",
            "source": "来源",
        }
        for column, heading in headings.items():
            self.tree.heading(column, text=heading)
        self.tree.column("index", width=54, anchor="center", stretch=False)
        self.tree.column("title", width=430, anchor="w")
        self.tree.column("start", width=110, anchor="center", stretch=False)
        self.tree.column("end", width=110, anchor="center", stretch=False)
        self.tree.column("source", width=110, anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("stripe", background=ROW_ALT)
        self.tree.tag_configure("generated", foreground=MUTED)
        self.tree.bind("<Double-1>", self._begin_cell_edit)

        bottom = tk.Frame(review_card, bg=CARD_BG)
        bottom.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 15))
        bottom.columnconfigure(0, weight=1)
        self.status_label = tk.Label(
            bottom,
            textvariable=self.status_var,
            bg=CARD_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            justify="left",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=150)
        self.progress.grid(row=0, column=1, padx=(12, 12))
        self.export_button = ttk.Button(bottom, text="导出章节 PDF", style="Accent.TButton", command=self.export)
        self.export_button.grid(row=0, column=2, sticky="e")

    def _stat_card(self, parent: tk.Frame, column: int, label: str, value: tk.StringVar) -> None:
        parent.columnconfigure(column, weight=1)
        card = tk.Frame(parent, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1, bd=0)
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 6 if column < 2 else 0))
        tk.Label(card, textvariable=value, bg=CARD_BG, fg=INK, font=("Microsoft YaHei UI", 20, "bold"), anchor="w").pack(
            anchor="w", padx=14, pady=(10, 0)
        )
        tk.Label(card, text=label, bg=CARD_BG, fg=MUTED, font=("Microsoft YaHei UI", 9), anchor="w").pack(
            anchor="w", padx=14, pady=(0, 10)
        )

    def choose_pdf(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择需要按章节拆分的 PDF",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        self.source_path = Path(selected)
        self.source_var.set(str(self.source_path))
        self.output_var.set(str(self.source_path.with_name(f"{self.source_path.stem}_章节拆分")))
        self.total_pages = 0
        self.ranges = []
        self.notice_var.set("")
        self._render_ranges()
        self._refresh_stats()
        self._set_status("文件已选择，选择识别方式后点击“分析章节”。")

    def open_source(self) -> None:
        if self.source_path is None:
            messagebox.showinfo("尚未选择文件", "请先选择一份 PDF。")
            return
        self._open_path(self.source_path)

    def choose_output(self) -> None:
        initial = str(self.source_path.parent) if self.source_path else None
        selected = filedialog.askdirectory(title="选择输出文件夹", initialdir=initial)
        if selected:
            self.output_var.set(selected)

    def open_output(self) -> None:
        output = Path(self.output_var.get().strip())
        if not output.exists():
            messagebox.showinfo("文件夹不存在", "导出后才能打开输出文件夹。")
            return
        self._open_path(output)

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("无法打开路径", str(exc))

    def analyze(self) -> None:
        if self.source_path is None:
            messagebox.showinfo("尚未选择文件", "请先选择一份 PDF。")
            return
        try:
            result = analyze_pdf(self.source_path, mode=self.mode_var.get())
            self.total_pages = result.total_pages
            if result.markers:
                self.ranges = build_ranges(result.markers, result.total_pages)
                self.notice_var.set(
                    "起始页可以双击修正；结束页会根据下一章起始页自动计算。"
                    if result.method != "none"
                    else ""
                )
                self._set_status(
                    f"已识别 {len(self.ranges)} 个输出段。你可以在导出前逐行校对。",
                    "success",
                )
            else:
                self.ranges = []
                self.notice_var.set(result.warning or "没有自动识别到章节，请手动添加起始页。")
                self._set_status("没有找到章节起始页，请在右侧添加章节。", "warning")
            self._render_ranges()
            self._refresh_stats(result.method)
        except Exception as exc:
            self.ranges = []
            self.total_pages = 0
            self._render_ranges()
            self._refresh_stats()
            self._set_status("分析失败，请检查 PDF 是否加密或损坏。", "error")
            messagebox.showerror("无法分析 PDF", str(exc))

    def _render_ranges(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for position, item in enumerate(self.ranges):
            tags = []
            if position % 2 == 1:
                tags.append("stripe")
            if item.source == "generated":
                tags.append("generated")
            self.tree.insert(
                "",
                "end",
                iid=str(position),
                values=(
                    item.index,
                    item.title,
                    item.start_page,
                    item.end_page,
                    SOURCE_LABELS.get(item.source, item.source),
                ),
                tags=tuple(tags),
            )

    def _refresh_stats(self, method: str | None = None) -> None:
        self.page_count_var.set(str(self.total_pages) if self.total_pages else "—")
        self.range_count_var.set(str(len(self.ranges)) if self.ranges else "—")
        if method == "bookmark":
            self.method_var.set("PDF 书签")
        elif method == "text":
            self.method_var.set("页面标题")
        elif method == "none":
            self.method_var.set("未识别")
        elif not self.total_pages:
            self.method_var.set("等待分析")

    def _set_status(self, message: str, tone: str = "normal") -> None:
        self.status_var.set(message)
        self.status_tone = tone
        color = {"normal": MUTED, "success": SUCCESS, "warning": WARNING, "error": DANGER}.get(tone, MUTED)
        self.status_label.configure(fg=color)

    def _selected_position(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            position = int(selection[0])
        except ValueError:
            return None
        return position if 0 <= position < len(self.ranges) else None

    def _begin_cell_edit(self, event: tk.Event) -> None:
        if self._cell_editor is not None:
            self._cell_editor.destroy()
            self._cell_editor = None

        item_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not item_id or column not in ("#2", "#3"):
            return
        position = int(item_id)
        if position >= len(self.ranges):
            return
        if column == "#3" and self.ranges[position].source == "generated":
            self._set_status("前置内容固定从第 1 页开始，请修改后续章节的起始页。", "warning")
            return

        box = self.tree.bbox(item_id, column)
        if not box:
            return
        x, y, width, height = box
        value_index = 1 if column == "#2" else 2
        current_value = str(self.tree.item(item_id, "values")[value_index])
        editor = ttk.Entry(self.tree, style="Card.TEntry")
        editor.insert(0, current_value)
        editor.select_range(0, tk.END)
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()
        self._cell_editor = editor

        def commit(_event: tk.Event | None = None) -> None:
            if self._cell_editor is None:
                return
            value = editor.get().strip()
            editor.destroy()
            self._cell_editor = None
            if not value:
                self._set_status("修改已取消：内容不能为空。", "warning")
                return
            current = self.ranges[position]
            try:
                updated = replace(current, title=value) if column == "#2" else replace(current, start_page=int(value))
                candidate = list(self.ranges)
                candidate[position] = updated
                self.ranges = recompute_ranges(candidate, self.total_pages)
                self._render_ranges()
                self._refresh_stats()
                self._set_status("章节边界已更新。", "success")
            except (ValueError, TypeError) as exc:
                self._set_status(f"修改失败：{exc}", "error")

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)
        editor.bind("<Escape>", lambda _event: self._cancel_editor(editor))

    def _cancel_editor(self, editor: ttk.Entry) -> None:
        if self._cell_editor is editor:
            editor.destroy()
            self._cell_editor = None

    def add_chapter(self) -> None:
        if self.source_path is None or self.total_pages < 1:
            messagebox.showinfo("请先分析文件", "先选择 PDF 并点击“分析章节”，再添加手动章节。")
            return
        title = simpledialog.askstring("添加章节", "章节标题：", parent=self.root)
        if not title or not title.strip():
            return
        page = simpledialog.askinteger(
            "添加章节",
            f"起始页（1 至 {self.total_pages}）：",
            parent=self.root,
            minvalue=1,
            maxvalue=self.total_pages,
        )
        if page is None:
            return
        try:
            self.ranges = recompute_ranges(
                [*self.ranges, ChapterRange(0, title.strip(), page, page, "manual")], self.total_pages
            )
            self._render_ranges()
            self._refresh_stats()
            self._set_status("已添加手动章节。", "success")
        except ValueError as exc:
            messagebox.showerror("无法添加章节", str(exc))

    def delete_selected(self) -> None:
        position = self._selected_position()
        if position is None:
            messagebox.showinfo("未选择章节", "请先在表格中选择要删除的行。")
            return
        candidate = self.ranges[:position] + self.ranges[position + 1 :]
        if not candidate:
            self.ranges = []
        else:
            try:
                self.ranges = recompute_ranges(candidate, self.total_pages)
            except ValueError as exc:
                messagebox.showerror("无法删除章节", str(exc))
                return
        self._render_ranges()
        self._refresh_stats()
        self._set_status("已删除所选章节，后续页码已重新计算。", "success")

    def export(self) -> None:
        if self.source_path is None:
            messagebox.showinfo("尚未选择文件", "请先选择 PDF。")
            return
        if not self.ranges:
            messagebox.showinfo("没有章节范围", "请先分析章节，或手动添加至少一个章节。")
            return
        try:
            ranges = recompute_ranges(self.ranges, self.total_pages)
            output_value = self.output_var.get().strip()
            if not output_value:
                raise ValueError("请选择输出文件夹")
            output_dir = Path(output_value)
        except ValueError as exc:
            messagebox.showerror("无法导出", str(exc))
            return

        if self._busy:
            return
        self._busy = True
        self._set_busy(True)
        self.progress.start(12)
        self._set_status("正在导出，请稍候……")
        open_after = self.open_after_var.get()
        source_path = self.source_path

        def worker() -> None:
            try:
                outputs = split_pdf(source_path, output_dir, ranges)
            except Exception as exc:
                self.root.after(0, lambda: self._export_failed(str(exc)))
            else:
                self.root.after(0, lambda: self._export_finished(outputs, open_after))

        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in (self.analyze_button, self.add_button, self.delete_button, self.export_button):
            button.configure(state=state)

    def _export_failed(self, message: str) -> None:
        self._busy = False
        self.progress.stop()
        self._set_busy(False)
        self._set_status("导出失败。", "error")
        messagebox.showerror("导出失败", message)

    def _export_finished(self, outputs: list[Path], open_after: bool) -> None:
        self._busy = False
        self.progress.stop()
        self._set_busy(False)
        self._set_status(f"已导出 {len(outputs)} 个 PDF。", "success")
        if open_after and outputs:
            self._open_path(outputs[0].parent)
        messagebox.showinfo("导出完成", f"已生成 {len(outputs)} 个章节 PDF：\n{outputs[0].parent}")


def main() -> None:
    root = tk.Tk()
    PdfChapterSplitterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
