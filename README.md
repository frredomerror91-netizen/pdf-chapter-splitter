# PDF 章节拆分器

一个面向 Windows 的本地 PDF 整理工具。选择一份长 PDF 后，它会读取内置书签或扫描页面标题，生成可校对的章节边界，并批量导出每章一个 PDF。

工具围绕三个动作设计：**选择资料 → 校对章节 → 导出文件**。原始 PDF 始终保持不变，所有结果写入旁边的独立文件夹。

## 功能

- 自动读取 PDF 内置目录/书签，识别形如“第 1 章”或“Chapter 1”的章节条目。
- 没有可用书签时，扫描页面文字层前部的章节标题。
- 提供三种检测模式：
  - **自动（书签优先）**：先读书签，找不到时再读页面标题；
  - **仅 PDF 书签**：只使用文档目录，适合目录结构可靠的教材；
  - **仅页面标题**：忽略书签，直接扫描页面文字。
- 双击编辑章节标题或起始页，结束页自动根据下一章重新计算。
- 手动添加、删除章节，适合目录缺失或扫描版资料。
- 自动保留第一个章节前的封面、目录和前言，命名为“前置内容”。
- Windows 文件名安全化、重复标题自动编号、输出进度提示。
- 导出完成后可自动打开输出文件夹。
- 全部处理在本机完成，不上传 PDF 内容。

## 快速开始

### 直接启动

在 Windows 中双击：

```text
启动章节拆分工具.cmd
```

启动入口会优先寻找本机已配置的 Python；找不到时再使用系统 `python` 命令。

### 安装依赖

首次使用或系统 Python 尚未安装依赖时，在项目目录打开 PowerShell：

```powershell
python -m pip install -r requirements.txt
```

也可以执行 PowerShell 入口：

```powershell
powershell -ExecutionPolicy Bypass -File .\启动章节拆分工具.ps1
```

运行环境建议使用 Python 3.10 或更高版本。

## 使用流程

1. 点击左侧“选择 PDF”。工具会默认把输出目录设置为：

   ```text
   原文件名_章节拆分
   ```

2. 选择识别方式。一般资料使用“自动（书签优先）”。
3. 点击“分析章节”。右侧表格会显示序号、标题、起始页、结束页和识别来源。
4. 检查章节边界：
   - 双击标题可以修改文件名中的章节标题；
   - 双击起始页可以移动章节边界；
   - 点击“添加章节”可以输入标题和起始页；
   - 选中一行后点击“删除所选”可以移除误识别的章节。
5. 选择输出文件夹，点击“导出章节 PDF”。
6. 导出的文件形如：

   ```text
   01_第1章 基础.pdf
   02_第2章 进阶.pdf
   ```

## 识别规则与限制

### 有文字层的 PDF

工具会识别页面前部、以这些形式开头的标题：

```text
第 1 章 文件基础
第2章：进阶内容
Chapter 3: Parsing
```

正文中间出现的“第 X 章”不会作为章节标题，除非它位于一行开头。

### 扫描版 PDF

如果 PDF 只有图片、没有文字层，页面标题扫描不会找到章节。这时可以：

1. 先用 PDFgear 等工具进行 OCR 并另存为带文字层的 PDF；或
2. 在本工具中分析后，使用“添加章节”手动输入每章起始页。

### 加密 PDF

加密 PDF 需要先解密或另存为未加密 PDF。工具不会尝试破解密码，也不会覆盖原文件。

### 书签层级

工具优先选择标题形如“第 X 章”或“Chapter X”的书签。若书签没有这些文字模式，则使用最顶层书签作为候选，导出前应在表格中复核。

## 目录结构

```text
pdf-chapter-splitter/
├─ pdf_chapter_splitter_core.py     # 识别、页码计算和 PDF 导出
├─ split_pdf_by_chapter.py          # Tkinter 桌面界面
├─ tests/                           # unittest 核心测试
├─ 启动章节拆分工具.cmd              # Windows 双击入口
├─ 启动章节拆分工具.ps1              # PowerShell 入口
├─ requirements.txt                 # 运行依赖
└─ requirements-dev.txt             # 测试依赖
```

## 开发与验证

安装开发依赖：

```powershell
python -m pip install -r requirements-dev.txt
```

运行测试：

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m py_compile pdf_chapter_splitter_core.py split_pdf_by_chapter.py
```

## 许可证

本项目使用 [MIT License](LICENSE)。
