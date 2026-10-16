# PDF 章节拆分器

把一本 PDF 按章节拆成多个文件，方便阅读、打印和整理。软件会先读取 PDF 书签或页面标题，你可以核对、修改章节名称和起始页，再一次导出。扫描版也可以先做 OCR，让拆分后的文件保留可搜索、可复制的文字。

适用于 Windows，所有处理都在本机完成，原始 PDF 保持不变。

[下载 v1.5.0](https://github.com/frredomerror91-netizen/pdf-chapter-splitter/releases/latest) · [使用说明](docs/使用说明.md) · [版本更新](https://github.com/frredomerror91-netizen/pdf-chapter-splitter/releases)

## 能做什么

- **自动找章节**：优先使用 PDF 书签，也可以从页面标题中识别章节。
- **扫描版 OCR**：配合 Umi-OCR 识别扫描页，另存整本可搜索的 PDF，再继续拆分。
- **手动校对**：修改章节名称和起始页，添加漏掉的章节，删除误识别的条目。
- **批量导出**：每章生成一个 PDF，封面、目录等前置内容也可以单独保留。
- **处理大文件**：自动分块识别，显示处理进度；中途失败或取消后，重试时可接着使用已完成的结果。
- **明暗主题**：可以切换暗夜模式，并记住主题和 OCR 设置。

## 下载和启动

目前提供源码包，需要安装 **Python 3.10 或更高版本**，并包含 Tkinter。安装 Python 时勾选“Add python.exe to PATH”。

1. 从 [Releases](https://github.com/frredomerror91-netizen/pdf-chapter-splitter/releases) 下载源码 ZIP，完整解压。
2. 在解压后的文件夹打开 PowerShell，安装依赖：

```powershell
python -m pip install -r requirements.txt
```

3. 双击 `启动章节拆分工具.cmd` 启动。也可以在同一目录运行：

```powershell
python .\split_pdf_by_chapter.py
```

如果电脑上有多个 Python 环境，请用同一个环境安装依赖和启动软件。

## 怎么用

1. 选择 PDF，通常保留“自动（书签优先）”即可。
2. 点击“分析章节”。扫描版请先配置 Umi-OCR，再点击“一键 OCR 并分析”。
3. 核对表格中的章节标题和起始页，双击即可修改，也可以添加或删除章节。
4. 选择输出文件夹，点击“开始导出”。

**起始页按 PDF 文件中的页序填写，从 1 开始**，可能与书上印刷的页码不同。

需要 OCR 时，另行下载并完整解压 [Umi-OCR 便携版](https://github.com/hiroi-sora/Umi-OCR/releases)，在“OCR 设置”中选择 `Umi-OCR.exe`。首次配置和常见问题见 [使用说明](docs/使用说明.md)。

## v1.5.0 更新了什么

这一版主要改善大文件 OCR：默认最多同时处理 4 个分块，也能按电脑性能调整；首块准备好就开始识别，处理进度更清楚。任务中断后重试时，可以复用已准备好的分块和识别结果。部分 PDF 准备过慢的问题也做了优化。

详细变化见 [v1.5.0 版本说明](docs/发布说明-v1.5.0.md)。

## 许可证

本项目使用 [MIT License](LICENSE)。OCR 功能依赖单独安装的 Umi-OCR，其使用遵循上游许可证。
