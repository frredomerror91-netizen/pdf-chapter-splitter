from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from pdf_chapter_splitter_core import (
    ChapterRange,
    build_ranges,
    recompute_ranges,
    split_pdf,
)
from pdf_ocr_workflow import AnalysisOutcome, OcrAnalysisError, analyze_with_ocr
from umi_ocr_client import OcrCancelledError, OcrProgress, UmiOcrClient, UmiOcrError, validate_local_url
from umi_ocr_service import (
    OcrSettings,
    UmiExecutableNotFoundError,
    ensure_umi_service,
    find_umi_executable,
    load_settings,
    save_settings,
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
TABLE_HEADING_BG = "#EAF0F6"
TABLE_SELECTED_BG = "#DCE8FF"
CONTROL_DISABLED_BG = "#F1F4F8"
ACCENT_DISABLED_BG = "#AFC4ED"
PROGRESS_TRACK = "#E6EDF5"
RADIO_ACTIVE_BG = "#F4F7FF"
APP_VERSION = "1.4.0"

THEME_LIGHT = {
    "app_bg": "#F3F6FA", "card_bg": "#FFFFFF", "ink": "#172033", "muted": "#65758B",
    "border": "#D8E2EC", "accent": "#2563EB", "accent_dark": "#1D4ED8", "accent_soft": "#E8F0FF",
    "success": "#0F766E", "warning": "#B45309", "danger": "#B42318", "row_alt": "#F8FAFC",
    "table_heading": "#EAF0F6", "table_selected": "#DCE8FF", "disabled": "#F1F4F8",
    "accent_disabled": "#AFC4ED", "progress_track": "#E6EDF5", "radio_active": "#F4F7FF",
}
THEME_DARK = {
    "app_bg": "#151A21", "card_bg": "#202832", "ink": "#EEF2F7", "muted": "#AAB6C4",
    "border": "#3A4655", "accent": "#5B8DEF", "accent_dark": "#3E72D5", "accent_soft": "#293B5D",
    "success": "#55C6A3", "warning": "#F2BD63", "danger": "#FF858F", "row_alt": "#26313D",
    "table_heading": "#2B3745", "table_selected": "#34527C", "disabled": "#2B333D",
    "accent_disabled": "#526B99", "progress_track": "#303A47", "radio_active": "#2A3442",
}


def _set_theme_constants(dark: bool) -> None:
    global APP_BG, CARD_BG, INK, MUTED, BORDER, ACCENT, ACCENT_DARK, ACCENT_SOFT
    global SUCCESS, WARNING, DANGER, ROW_ALT, TABLE_HEADING_BG, TABLE_SELECTED_BG
    global CONTROL_DISABLED_BG, ACCENT_DISABLED_BG, PROGRESS_TRACK, RADIO_ACTIVE_BG
    palette = THEME_DARK if dark else THEME_LIGHT
    APP_BG, CARD_BG, INK, MUTED = palette["app_bg"], palette["card_bg"], palette["ink"], palette["muted"]
    BORDER, ACCENT, ACCENT_DARK, ACCENT_SOFT = palette["border"], palette["accent"], palette["accent_dark"], palette["accent_soft"]
    SUCCESS, WARNING, DANGER, ROW_ALT = palette["success"], palette["warning"], palette["danger"], palette["row_alt"]
    TABLE_HEADING_BG, TABLE_SELECTED_BG = palette["table_heading"], palette["table_selected"]
    CONTROL_DISABLED_BG, ACCENT_DISABLED_BG = palette["disabled"], palette["accent_disabled"]
    PROGRESS_TRACK, RADIO_ACTIVE_BG = palette["progress_track"], palette["radio_active"]

MODE_LABELS = {
    "auto": "自动（书签优先）",
    "bookmark": "仅 PDF 书签",
    "text": "仅页面标题",
}
SOURCE_LABELS = {
    "bookmark": "PDF 书签",
    "text": "页面标题",
    "ocr": "OCR 页面标题",
    "manual": "手动",
    "generated": "自动补齐",
}


class PdfChapterSplitterApp:
    def __init__(self, root: tk.Tk, *, project_dir: Path | None = None) -> None:
        self.root = root
        self.root.title("PDF 章节拆分器")
        self.root.geometry("1120x800")
        self.root.minsize(960, 740)

        self.source_path: Path | None = None
        self.total_pages = 0
        self.ranges: list[ChapterRange] = []
        self._cell_editor: ttk.Entry | None = None
        self._busy = False
        self._closing = False
        self._closed = False
        self._processing_path: Path | None = None
        self._events: queue.Queue = queue.Queue()
        self._cancel_event = threading.Event()
        self.project_dir = (project_dir or Path(__file__).parent).resolve()
        self._settings_path = self.project_dir / "umi-ocr.local.json"
        self.ocr_settings = load_settings(self._settings_path)
        self.umi_executable = find_umi_executable(self.project_dir, self.ocr_settings.executable)
        self._last_force_ocr = False
        self.dark_mode_var = tk.BooleanVar(value=self.ocr_settings.dark_mode)
        _set_theme_constants(self.dark_mode_var.get())
        self.root.configure(bg=APP_BG)

        self.source_var = tk.StringVar(value="还没有选择 PDF")
        self.output_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="auto")
        self.open_after_var = tk.BooleanVar(value=True)
        self.auto_ocr_var = tk.BooleanVar(value=self.ocr_settings.auto_ocr)
        self.status_var = tk.StringVar(value="选择一份 PDF，工具会先读取目录，再尝试识别页面标题。")
        self.status_tone = "normal"
        self.page_count_var = tk.StringVar(value="—")
        self.range_count_var = tk.StringVar(value="—")
        self.method_var = tk.StringVar(value="等待分析")
        self.notice_var = tk.StringVar(value="")

        self._configure_styles()
        self._build_widgets()
        self._refresh_stats()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = self.root.after(75, self._poll_events)

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
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", ACCENT_DISABLED_BG)])
        style.configure("Ghost.TButton", background=CARD_BG, foreground=INK, bordercolor=BORDER, padding=(11, 7))
        style.map("Ghost.TButton", background=[("active", ACCENT_SOFT), ("disabled", CONTROL_DISABLED_BG)])
        style.configure("Danger.TButton", background=CARD_BG, foreground=DANGER, bordercolor=BORDER, padding=(11, 7))
        style.map("Danger.TButton", background=[("active", ACCENT_SOFT), ("disabled", CONTROL_DISABLED_BG)])
        style.configure("Mode.TRadiobutton", background=CARD_BG, foreground=INK, padding=(7, 6), font=("Microsoft YaHei UI", 9))
        style.map("Mode.TRadiobutton", background=[("selected", ACCENT_SOFT), ("active", RADIO_ACTIVE_BG)])
        style.configure("Card.TEntry", fieldbackground=CARD_BG, foreground=INK, padding=(8, 6))
        style.configure("Table.Treeview", background=CARD_BG, fieldbackground=CARD_BG, foreground=INK, rowheight=36, borderwidth=0, font=("Microsoft YaHei UI", 9))
        style.configure("Table.Treeview.Heading", background=TABLE_HEADING_BG, foreground=MUTED, relief="flat", font=("Microsoft YaHei UI", 9, "bold"), padding=(8, 9))
        style.map("Table.Treeview", background=[("selected", TABLE_SELECTED_BG)], foreground=[("selected", INK)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PROGRESS_TRACK, borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
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
        container = ttk.Frame(parent, style="App.TFrame")
        container.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        self.sidebar_canvas = tk.Canvas(container, bg=APP_BG, width=312, height=400, highlightthickness=0)
        self.sidebar_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.sidebar_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.sidebar_canvas.configure(yscrollcommand=scrollbar.set)
        sidebar = ttk.Frame(self.sidebar_canvas, style="App.TFrame")
        window_id = self.sidebar_canvas.create_window((0, 0), window=sidebar, anchor="nw")
        sidebar.bind("<Configure>", lambda _event: self.sidebar_canvas.configure(scrollregion=self.sidebar_canvas.bbox("all")))
        self.sidebar_canvas.bind("<Configure>", lambda event: self.sidebar_canvas.itemconfigure(window_id, width=event.width))
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
        self.choose_button = ttk.Button(source_actions, text="选择 PDF", style="Accent.TButton", command=self.choose_pdf)
        self.choose_button.pack(side="left")
        ttk.Button(source_actions, text="打开", style="Ghost.TButton", command=self.open_source).pack(side="left", padx=(8, 0))

        mode_card = self._make_card(sidebar, row=1, column=0, sticky="ew", pady=(0, 14))
        mode_card.columnconfigure(0, weight=1)
        self._card_heading(mode_card, "02 识别方式", "自动模式会优先读取 PDF 目录。")
        modes = tk.Frame(mode_card, bg=CARD_BG)
        modes.grid(row=2, column=0, sticky="ew", padx=12, pady=(1, 8))
        self.mode_buttons = []
        for value, label in MODE_LABELS.items():
            button = ttk.Radiobutton(
                modes,
                text=label,
                value=value,
                variable=self.mode_var,
                style="Mode.TRadiobutton",
            )
            button.pack(fill="x", pady=2)
            self.mode_buttons.append(button)
        tk.Label(
            mode_card,
            text="扫描版可自动使用 Umi-OCR。部分页面缺少文字层时，可点击“一键 OCR 并分析”。",
            bg=CARD_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=268,
        ).grid(row=3, column=0, sticky="ew", padx=16, pady=(2, 6))
        self.auto_ocr_check = ttk.Checkbutton(
            mode_card, text="无文字层时自动 OCR", variable=self.auto_ocr_var, command=self.save_ocr_preference,
        )
        self.auto_ocr_check.grid(row=4, column=0, sticky="w", padx=12, pady=(0, 8))
        self.settings_button = ttk.Button(
            mode_card, text="OCR 设置", style="Ghost.TButton", command=self.open_ocr_settings,
        )
        self.settings_button.grid(row=5, column=0, sticky="w", padx=16, pady=(0, 12))

        output_card = self._make_card(sidebar, row=2, column=0, sticky="ew")
        output_card.columnconfigure(0, weight=1)
        self._card_heading(output_card, "03 输出设置", "每个章节会生成一个独立 PDF。")
        self.output_entry = ttk.Entry(output_card, textvariable=self.output_var, style="Card.TEntry")
        self.output_entry.grid(row=2, column=0, sticky="ew", padx=16, pady=(1, 8))
        self.output_button = ttk.Button(output_card, text="选择输出文件夹", style="Ghost.TButton", command=self.choose_output)
        self.output_button.grid(row=3, column=0, sticky="w", padx=16, pady=(0, 7))
        ttk.Checkbutton(
            output_card,
            text="导出完成后打开文件夹",
            variable=self.open_after_var,
        ).grid(row=4, column=0, sticky="w", padx=12, pady=(0, 14))
        self.theme_check = ttk.Checkbutton(
            output_card, text="暗夜模式", variable=self.dark_mode_var, command=self.toggle_theme,
        )
        self.theme_check.grid(row=5, column=0, sticky="w", padx=12, pady=(0, 14))

        self.analyze_button = ttk.Button(
            container,
            text="分析章节",
            style="Accent.TButton",
            command=self.analyze,
        )
        self.analyze_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.ocr_button = ttk.Button(
            container, text="一键 OCR 并分析", style="Ghost.TButton", command=lambda: self.analyze(force_ocr=True),
        )
        self.ocr_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        def bind_scroll(widget: tk.Misc) -> None:
            widget.bind("<MouseWheel>", lambda event: self.sidebar_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units"))
            widget.bind("<Button-4>", lambda _event: self.sidebar_canvas.yview_scroll(-1, "units"))
            widget.bind("<Button-5>", lambda _event: self.sidebar_canvas.yview_scroll(1, "units"))
            for child in widget.winfo_children():
                bind_scroll(child)

        bind_scroll(self.sidebar_canvas)

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
        self.notice_label.bind("<Configure>", lambda event: self.notice_label.configure(wraplength=max(200, event.width)))

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
        self.status_label.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(150, event.width)))
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=150)
        self.progress.grid(row=0, column=1, padx=(12, 12))
        self.export_button = ttk.Button(bottom, text="导出章节 PDF", style="Accent.TButton", command=self.export)
        self.export_button.grid(row=0, column=2, sticky="e")
        self.cancel_button = ttk.Button(bottom, text="取消处理", command=self.cancel_processing, state="disabled")
        self.cancel_button.grid(row=1, column=2, sticky="e", pady=(8, 0))

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
        if self._busy:
            return
        selected = filedialog.askopenfilename(
            title="选择需要按章节拆分的 PDF",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        self.source_path = Path(selected)
        self._processing_path = self.source_path
        self.source_var.set(str(self.source_path))
        output_base = self.source_path.with_name(f"{self.source_path.stem}_章节拆分")
        output_dir = output_base
        suffix = 2
        while output_dir.exists():
            output_dir = output_base.with_name(f"{output_base.name} ({suffix})")
            suffix += 1
        self.output_var.set(str(output_dir))
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
        if self._busy:
            return
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

    def save_ocr_preference(self) -> None:
        self.ocr_settings = replace(self.ocr_settings, auto_ocr=self.auto_ocr_var.get())
        try:
            save_settings(self._settings_path, self.ocr_settings)
        except OSError as exc:
            self._set_status(f"OCR 设置已在本次会话生效，但无法保存：{exc}", "warning")

    def toggle_theme(self) -> None:
        if self._busy:
            return
        dark = self.dark_mode_var.get()
        self.ocr_settings = replace(self.ocr_settings, dark_mode=dark)
        try:
            save_settings(self._settings_path, self.ocr_settings)
        except OSError as exc:
            self._set_status(f"主题已切换，但无法保存设置：{exc}", "warning")
        _set_theme_constants(dark)
        self.root.configure(bg=APP_BG)
        for child in self.root.winfo_children():
            child.destroy()
        self._configure_styles()
        self._build_widgets()
        self._render_ranges()
        self._refresh_stats()
        self._set_status("已切换为暗夜模式。" if dark else "已切换为明亮模式。", "success")

    def choose_umi_executable(self) -> bool:
        selected = filedialog.askopenfilename(
            title="首次使用 OCR：选择已解压的 Umi-OCR.exe（取消可继续手动添加章节）",
            parent=self.root,
            filetypes=[("Umi-OCR", "Umi-OCR.exe"), ("可执行文件", "*.exe")],
        )
        if not selected:
            return False
        path = Path(selected).resolve()
        if not path.is_file() or path.name.lower() != "umi-ocr.exe":
            messagebox.showerror("无法配置 OCR", "请选择已解压的 Umi-OCR.exe 主程序。", parent=self.root)
            return False
        self.umi_executable = path
        self.ocr_settings = replace(self.ocr_settings, executable=str(path))
        self.save_ocr_preference()
        return True

    def open_ocr_settings(self) -> None:
        if self._busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("OCR 设置")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg=APP_BG)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20, style="App.TFrame")
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Umi-OCR 主程序", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        executable_var = tk.StringVar(value=str(self.umi_executable or self.ocr_settings.executable))
        ttk.Entry(frame, textvariable=executable_var, width=57).pack(fill="x", pady=(8, 6))

        def browse() -> None:
            selected = filedialog.askopenfilename(
                title="选择 Umi-OCR.exe", parent=dialog,
                filetypes=[("Umi-OCR", "Umi-OCR.exe"), ("可执行文件", "*.exe")],
            )
            if selected:
                executable_var.set(selected)

        ttk.Button(frame, text="选择 Umi-OCR.exe", command=browse).pack(anchor="w")
        ttk.Label(frame, text="本地服务地址", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(18, 0))
        url_var = tk.StringVar(value=self.ocr_settings.base_url)
        ttk.Entry(frame, textvariable=url_var).pack(fill="x", pady=(8, 12))
        ttk.Label(
            frame,
            text="首次使用：下载并解压 Umi-OCR v2.1.4 或更高版本。\n"
            "在 Umi-OCR 全局设置中开启 HTTP 服务，主机选择“仅本地”。\n"
            "默认地址为 http://127.0.0.1:1224，配置后即可后台调用。\n"
            "也可把便携版放在 tools/Umi-OCR，无需手动指定路径。",
            style="Muted.TLabel", wraplength=440, justify="left",
        ).pack(anchor="w")

        def save() -> None:
            try:
                base_url = validate_local_url(url_var.get())
                path_value = executable_var.get().strip()
                executable = Path(path_value).resolve() if path_value else None
                if executable is not None and (not executable.is_file() or executable.name.lower() != "umi-ocr.exe"):
                    raise UmiOcrError("请选择有效的 Umi-OCR.exe 文件。")
                settings = replace(self.ocr_settings, executable=str(executable) if executable else "",
                                   base_url=base_url, auto_ocr=self.auto_ocr_var.get())
                save_settings(self._settings_path, settings)
            except (OSError, UmiOcrError) as exc:
                messagebox.showerror("无法保存 OCR 设置", str(exc), parent=dialog)
                return
            self.ocr_settings = settings
            self.umi_executable = find_umi_executable(self.project_dir, settings.executable)
            dialog.destroy()
            self._set_status("OCR 设置已保存。选择 PDF 后可直接分析或点击“一键 OCR 并分析”。", "success")

        ttk.Button(frame, text="保存设置", command=save, style="Accent.TButton").pack(anchor="e", pady=(18, 0))

    def close(self) -> None:
        if self._closed:
            return
        if self._busy:
            self._closing = True
            self.cancel_processing()
            self.root.withdraw()
            return
        self._closed = True
        self.root.after_cancel(self._poll_id)
        self.root.destroy()

    def analyze(self, *, force_ocr: bool = False) -> None:
        if self._busy:
            return
        if self.source_path is None:
            messagebox.showinfo("尚未选择文件", "请先选择一份 PDF。")
            return
        source = self.source_path if force_ocr else (self._processing_path or self.source_path)
        mode = self.mode_var.get()
        settings = self.ocr_settings
        auto_ocr = self.auto_ocr_var.get()
        executable = self.umi_executable
        destination = source.with_name(f"{source.stem}_OCR.pdf")
        self._last_force_ocr = force_ocr
        self._previous_method = self.method_var.get()
        self._busy = True
        self._cancel_event.clear()
        self._set_busy(True)
        self.cancel_button.configure(state="normal")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._set_status("正在分析 PDF……")

        def prepare_service() -> None:
            self._events.put(("status", "正在连接或启动 Umi-OCR……"))
            ensure_umi_service(client, executable, cancel_event=self._cancel_event)
            self._events.put(("status", "正在上传 PDF 到本地 OCR 服务……"))

        def worker() -> None:
            try:
                result = analyze_with_ocr(
                    source, destination, client, mode=mode, force=force_ocr, auto_ocr=auto_ocr,
                    prepare_service=prepare_service,
                    on_progress=lambda progress: self._events.put(("ocr_progress", progress)),
                    cancel_event=self._cancel_event,
                    workspace_dir=self.project_dir, executable=executable,
                )
                self._events.put(("analysis_finished", result))
            except Exception as exc:
                self._events.put(("analysis_failed", exc))

        client = UmiOcrClient(settings.base_url)
        threading.Thread(target=worker).start()

    def _analysis_finished(self, outcome: AnalysisOutcome) -> None:
        self._finish_work()
        if self._closing:
            self.close()
            return
        result = outcome.detection
        if outcome.pdf_path != self.source_path and result.method == "text":
            result = replace(
                result,
                method="ocr",
                markers=[replace(marker, source="ocr") for marker in result.markers],
            )
        self._processing_path = outcome.pdf_path
        self.total_pages = result.total_pages
        self.ranges = build_ranges(result.markers, result.total_pages) if result.markers else []
        if result.markers:
            prefix = "OCR 已完成。" if outcome.used_ocr else ""
            self.notice_var.set(prefix + "起始页可以双击修正；结束页会根据下一章自动计算。")
            self._set_status(f"已识别 {len(self.ranges)} 个输出段。请在导出前逐行校对。", "success")
        else:
            self.notice_var.set(
                "OCR 已完成，未找到章节标题。可手动添加章节后导出带文字层的 PDF。"
                if outcome.used_ocr else result.warning or "没有找到章节标题。",
            )
            self._set_status("没有找到章节起始页，请在右侧添加章节。", "warning")
        self._render_ranges()
        method = "ocr" if outcome.used_ocr else result.method
        self._refresh_stats(method)

    def _analysis_failed(self, error: Exception) -> None:
        self._finish_work()
        if self._closing:
            self.close()
            return
        if isinstance(error, OcrCancelledError):
            self._set_status("处理已取消。", "warning")
            self.method_var.set(self._previous_method)
            return
        if isinstance(error, OcrAnalysisError):
            self.total_pages = error.detection.total_pages
            if error.ocr_path is not None and error.ocr_path.exists():
                self._processing_path = error.ocr_path
                self.notice_var.set(
                    f"OCR PDF 已保存到：{error.ocr_path}\n"
                    f"但章节分析失败：{error} 可打开该 PDF，或手动添加章节继续导出。"
                )
            else:
                self.notice_var.set(f"OCR 未完成：{error} 可以重试，或手动添加章节继续导出。")
        else:
            self.notice_var.set(str(error))
        self._refresh_stats("none")
        self._set_status("分析失败，可以重试或手动校对。", "error")
        if isinstance(error.__cause__, UmiExecutableNotFoundError):
            if self.choose_umi_executable():
                self.analyze(force_ocr=self._last_force_ocr)
            return
        messagebox.showerror("无法分析 PDF", str(error), parent=self.root)

    def _poll_events(self) -> None:
        try:
            while not self._closed:
                kind, value = self._events.get_nowait()
                if kind == "status":
                    self._set_status(value)
                elif kind == "ocr_progress":
                    self._show_ocr_progress(value)
                elif kind == "analysis_finished":
                    self._analysis_finished(value)
                elif kind == "analysis_failed":
                    self._analysis_failed(value)
                elif kind == "export_finished":
                    self._export_finished(*value)
                elif kind == "export_failed":
                    self._export_failed(value)
        except queue.Empty:
            pass
        if not self._closed:
            self._poll_id = self.root.after(75, self._poll_events)

    def _show_ocr_progress(self, progress: OcrProgress) -> None:
        if progress.total_pages:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=progress.total_pages, value=progress.processed_pages)
        if progress.status in {"preparing", "splitting", "merging"}:
            self.method_var.set("准备分块 OCR" if progress.status != "merging" else "合并 OCR 结果")
            self._set_status(progress.message or "正在处理 OCR 分块……")
            return
        if progress.total_chunks > 1:
            self.method_var.set("分块 OCR 处理中")
            self._set_status(
                f"分块 OCR：已完成 {progress.completed_chunks}/{progress.total_chunks} 份，"
                f"进度 {progress.processed_pages}/{progress.total_pages} 页。"
            )
            return
        self.method_var.set("OCR 处理中")
        if progress.status == "waiting":
            self._set_status("OCR 正在排队，请稍候……")
        elif progress.is_done and progress.status == "success":
            self._set_status("OCR 已完成，正在下载并分析文字层……")
        else:
            suffix = f"（{progress.processed_pages}/{progress.total_pages} 页）" if progress.total_pages else ""
            self._set_status(f"正在 OCR{suffix}……")

    def cancel_processing(self) -> None:
        if self._busy:
            self._cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self._set_status("正在取消，请稍候……")

    def _finish_work(self) -> None:
        self._busy = False
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self._set_busy(False)
        self.cancel_button.configure(state="disabled")

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
        elif method == "ocr":
            self.method_var.set("OCR 页面标题")
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
        if self._busy:
            return
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
        if self._busy:
            return
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
        if self._busy:
            return
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
        if self._busy:
            return
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
        source_path = self._processing_path or self.source_path

        def worker() -> None:
            try:
                outputs = split_pdf(source_path, output_dir, ranges)
            except Exception as exc:
                self._events.put(("export_failed", str(exc)))
            else:
                self._events.put(("export_finished", (outputs, open_after)))

        threading.Thread(target=worker).start()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (
            self.analyze_button, self.ocr_button, self.add_button, self.delete_button, self.export_button,
            self.choose_button, self.output_button, self.output_entry, self.settings_button, self.auto_ocr_check,
            self.theme_check,
            *self.mode_buttons,
        ):
            widget.configure(state=state)
        if busy and self._cell_editor is not None:
            self._cancel_editor(self._cell_editor)

    def _export_failed(self, message: str) -> None:
        self._finish_work()
        if self._closing:
            self.close()
            return
        self._set_status("导出失败。", "error")
        messagebox.showerror("导出失败", message)

    def _export_finished(self, outputs: list[Path], open_after: bool) -> None:
        self._finish_work()
        if self._closing:
            self.close()
            return
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
