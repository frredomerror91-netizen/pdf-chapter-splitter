# PDF 章节拆分器

当前版本：**v1.5.0**。面向 Windows 的本地 PDF 整理工具，支持识别章节、校对起始页和批量导出每章一个 PDF。扫描版可以在工具内一键调用 Umi-OCR，识别完成后保留整本可搜索 PDF。

**选择 PDF → 分析或一键 OCR → 校对章节 → 导出。** 原始 PDF 保持不变。

完整操作步骤见 [使用说明](docs/使用说明.md)，版本变化见 [更新日志](CHANGELOG.md)。

## v1.5.0 的主要功能

- 一键 OCR 并分析，无需手动切换到 OCR 软件或重新选择识别结果。
- 100 页及以上自动均匀分块，每块最多 50 页；默认最多使用 4 个独立 Umi-OCR 实例，并按分块数、CPU 和可用内存进一步限制；手动选择自动模式时，资源足够可每块一个实例，最多 12 个。
- 边准备边 OCR，首块准备好即可识别；逐页显示输入准备、OCR、完成分块和活动实例进度，准备期间可取消。
- 大型任务保留已完成识别结果和已验证输入；失败或取消后，重新执行可复用符合校验的检查点。
- 优化缺失 PDF 对象的查找，避免每次遇到缺失引用都重新扫描整份文件。
- 在“OCR 设置”选择并发上限：默认 4，0 为自动，1–12 为手动上限；同时受资源预算约束。
- 成功 OCR 后在原文件旁保存 `<原文件名>_OCR.pdf`，即使后续章节分析失败也能继续使用。
- 优先识别 PDF 书签，再尝试页面章节标题；过滤纯页码书签、目录条目和重复章页眉。
- 支持双击编辑章节标题和起始页、手动添加或删除章节，保留封面和目录等前置内容。
- 支持明亮和暗夜主题，保存主题偏好与 OCR 设置。

大型 PDF 的中间分块用于 OCR；最终导出按校对后的章节范围生成文件。

## 下载与安装

发行包是源码版，需要 **Windows、Python 3.10 或更高版本，以及 Tkinter**。Windows 官方 Python 安装程序通常包含 Tkinter，安装时勾选“Add python.exe to PATH”。

1. 在本仓库 Releases 下载对应版本的源码 ZIP，或使用“Code → Download ZIP”。
2. 完整解压到可写目录，例如 `D:\Tools\pdf-chapter-splitter`。
3. 在解压后的项目目录打开 PowerShell，安装运行依赖：

```powershell
python -m pip install -r requirements.txt
```

4. 双击 `启动章节拆分工具.cmd`。也可直接启动：

```powershell
python .\split_pdf_by_chapter.py
```

PowerShell 启动入口：

```powershell
powershell -ExecutionPolicy Bypass -File .\启动章节拆分工具.ps1
```

启动脚本优先使用当前用户已安装的 Codex Python 运行时，找不到时使用系统 `python`。安装依赖与启动程序应使用同一个 Python 环境。

## 配置一键 OCR

有可靠书签或可提取文字的 PDF 可以直接分析。扫描版需要另外安装 [Umi-OCR 便携版](https://github.com/hiroi-sora/Umi-OCR/releases)。本版本实际验证使用 **Umi-OCR Rapid v2.1.5**；文档接口说明见 [Umi-OCR PDF HTTP 接口](https://github.com/hiroi-sora/Umi-OCR/blob/main/docs/http/api_doc.md)。

1. 下载 Umi-OCR 并完整解压，保留主程序旁的 `UmiOCR-data` 目录。
2. 打开本工具的“OCR 设置”，选择解压目录中的 `Umi-OCR.exe`。
3. 保持 HTTP 服务为“仅本地”，地址与本工具配置一致；默认是 `http://127.0.0.1:1224`。
4. 保存设置后，选中 PDF，点击“一键 OCR 并分析”。后续无需重复配置。

也可以把完整便携版放到以下位置，工具会尝试自动找到主程序：

```text
tools/Umi-OCR/Umi-OCR.exe
tools/Umi-OCR_Rapid_v2.1.5/Umi-OCR.exe
Umi-OCR/Umi-OCR.exe
```

程序会连接已有的本地服务，或隐藏启动配置的 Umi-OCR。大型 PDF 按资源计划启动临时隔离实例，使用各自的端口、设置和临时目录，任务结束后回收。OCR 只与本机 HTTP 服务交换文件。

Umi-OCR、模型、个人设置和教材 PDF 不包含在源码发行包中。

## 并发数量与进度

在“OCR 设置”中，**OCR 并发上限默认是 4**，用于控制 CPU 占用；主动设为 **0** 时启用自动模式。100 页及以上按每块最多 50 页均匀分块，实际实例数取分块数、CPU 预算、内存预算、手动上限及 12 的最小值。CPU 按每实例至少 2 个逻辑核预算；内存保留 1 GiB，为每实例估算 768 MiB。资源信息不可用时最多使用 2 个实例。预算用于控制启动规模，实际内存仍随页面尺寸和模型变化。

例如，306 页分为 7 块：默认上限 4 时使用最多 4 个实例，其余分块排队；主动设为 0、20 个逻辑 CPU 且可用内存足够时，可以启动 7 个独立实例，每块各用一个。资源不足或手动设为 3 时，启动最多 3 个实例，其余分块排队。每个实例最多使用 2 个计算线程，并按 CPU 数量进一步限制；默认 4 个实例的 OCR 计算线程预算最多为 8 个。

状态区同时显示输入准备页数、OCR 页数、完成块数和活动实例。大型任务的进度条合计输入准备与 OCR 两段页数；输入全部准备好后仍需完成 OCR 和最终合并。

## 日常使用

1. 点击“选择 PDF”，确认文件和输出目录。
2. 通常使用“自动（书签优先）”，点击“分析章节”。纯扫描版在勾选“无文字层时自动 OCR”后会自动进入 OCR。
3. 对部分扫描页或文字层不可靠的资料，点击“一键 OCR 并分析”。
4. 在章节表格中校对标题和起始页。页码按 PDF 文件从 1 开始计算；双击可编辑，结束页根据下一章起始页计算。
5. 检查章节覆盖全部页面，再点击“开始导出”。
6. 在左侧“输出设置”中勾选“暗夜模式”可以切换主题。

输出示例：

```text
教材.pdf
教材_OCR.pdf
教材_章节拆分/
├─ 01_前置内容.pdf
├─ 02_第1章 绪论.pdf
└─ 03_第2章 基础知识.pdf
```

已有默认输出目录时会选择带新编号的目录。成功重新 OCR 同一份原文件时，会更新同名 OCR PDF；失败或取消的大型分块任务不会用未完成结果覆盖已有完整 OCR PDF。

## 大型 PDF 的处理与恢复

100 页及以上自动分块。例如 657 页会分为 14 块，其中 13 块为 47 页、1 块为 46 页；界面显示累计识别页数、完成块数和合并阶段。

所有分块完成后，程序核对原文件、各块页数和总页数，再按原始顺序合并，保留原始书签和元数据。中途失败或取消时，已完成块保存在项目内的 `.ocr-cache/jobs/`；保留缓存并再次执行同一任务，可以减少重复识别。

持续有新增页面进度时没有固定一小时截止；连续 15 分钟没有新增页面进度会提示停滞。并行效率受 CPU、磁盘、页面尺寸和文字密度影响。

## 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 双击后提示找不到 Python | 安装 Python 3.10+ 并加入 PATH，重新打开终端后安装依赖。 |
| 提示缺少 pypdf | 用实际启动程序的 Python 执行 `-m pip install -r requirements.txt`。 |
| 找不到 Umi-OCR 或 HTTP 连接失败 | 在“OCR 设置”选择完整便携版主程序，并检查 HTTP 服务、地址和端口。 |
| 大型 PDF 提示需要完整便携版 | 选择本机 `Umi-OCR.exe`；仅填写 HTTP 地址不足以创建独立引擎。 |
| OCR 已完成，章节分析失败 | 按提示打开已保存的 `*_OCR.pdf`；可重新分析或手动添加章节。 |
| 章节标题或起始页不准确 | 对照原 PDF 手动校正。OCR 和标题规则可能误识别，导出前需检查。 |
| PDF 已加密 | 先用知道密码的 PDF 软件解密或另存为未加密文件。 |
| 大型任务失败或取消 | 保留缓存，对同一份未改动资料重新执行 OCR。 |

更多说明见 [完整使用说明](docs/使用说明.md)。

## 开发与验证

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -p "test_*.py"
python -m py_compile pdf_chapter_splitter_core.py pdf_ocr_workflow.py pdf_reader_recovery.py ocr_chunks.py umi_ocr_pool.py umi_ocr_client.py umi_ocr_service.py split_pdf_by_chapter.py
```

回归覆盖章节识别、原文件保护、OCR 文件保存、失败与取消、并行分块、缓存恢复和界面交互。HTTP 测试使用本地模拟服务；真实 OCR 需要另外安装 Umi-OCR。无图形显示环境时，界面测试会跳过。

核心入口：`split_pdf_by_chapter.py`；OCR 工作流：`pdf_ocr_workflow.py`；分块与引擎隔离：`ocr_chunks.py`、`umi_ocr_pool.py`。

## 许可证

本项目使用 [MIT License](LICENSE)。Umi-OCR 是独立的第三方软件，使用和分发遵循其上游许可证；本仓库没有将 Umi-OCR 二进制或模型打包进源码发行包。
