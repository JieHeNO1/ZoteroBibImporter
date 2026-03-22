# ZoteroBibImporter

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)

一键从 BibTeX 文件自动添加文献到 Zotero，并智能下载全文 PDF。支持重复检测、DOI 补全、多源 PDF 下载（Unpaywall、Semantic Scholar、Sci‑Hub、LibGen 等）。

## ✨ 功能特点

- 📄 **批量导入**：自动解析 BibTeX 文件，将文献添加到 Zotero 个人库
- 🔍 **DOI 补全**：对于缺少 DOI 的条目，通过标题在 CrossRef 查询并自动补全
- 🔄 **重复检测**：自动识别并去除重复文献（基于 DOI 和标题），生成去重后的 BibTeX 文件
- 📥 **智能 PDF 下载**：依次尝试从 Unpaywall、Semantic Scholar、DOI 直链、PMC、arXiv、Sci‑Hub、LibGen 等来源获取全文 PDF
- ✅ **PDF 校验**：下载后自动验证文件头、完整性，避免损坏文件
- 📁 **规范命名**：PDF 文件按 `序号#引用标签#年份#标题.pdf` 格式重命名，方便管理
- 📋 **失败记录**：自动将添加失败、PDF 下载失败的条目导出为独立 BibTeX 文件，便于重试
- 🚀 **多文件处理**：支持处理目录下所有 `.bib` 文件

## 🖥️ 系统要求

- Python 3.7 或更高版本
- Zotero 桌面版（用于同步 PDF）
- 有效的 Zotero API Key（需包含 **写入** 和 **文件上传** 权限）

## 📦 安装

1. 克隆仓库
```bash
git clone https://github.com/JieHeNO1/ZoteroBibImporter.git
cd ZoteroBibImporter
安装依赖

bash
pip install -r requirements.txt
配置环境变量

复制 .env.example 为 .env

填写你的 Zotero API Key 和邮箱

⚙️ 配置
1. 获取 Zotero API Key
登录 Zotero 官网，进入 Settings → Feeds/API → Create new private key

勾选 Allow library access、Write access 和 File upload（至少需要这些权限）

复制生成的 Key

2. 设置环境变量
编辑 .env 文件，填写以下内容：

bash
ZOTERO_API_KEY=你的API Key
ZOTERO_LIBRARY_ID=你的用户ID
ZOTERO_LIBRARY_TYPE=user
UNPAYWALL_EMAIL=你的邮箱
3. 调整目录和参数（可选）
脚本开头的 INPUT_DIR、OUTPUT_DIR 等可根据需要修改。

🚀 使用方法
将需要处理的 BibTeX 文件放入 ./bibs/ 目录（可修改 INPUT_DIR 配置）。

运行脚本：

bash
python zotero_bib_importer.py
脚本会自动：

检测并去重文献，生成 _unique.bib 文件

提取无 DOI 的条目，保存到 no_doi_entries.bib

通过标题为无 DOI 条目补全 DOI，生成 _alldoi.bib

将文献添加到 Zotero，并尝试下载 PDF

在 ./output/ 目录下保存 PDF 文件（按规范命名）

生成失败日志文件（failed_entries.bib、failedPDFs.bib）

📂 输出文件说明
output/：成功下载的 PDF 文件

bibs/genbibs/：

*_unique.bib：去重后的 BibTeX 文件

*_alldoi.bib：包含补全 DOI 后的所有条目

no_doi_entries.bib：最终仍无 DOI 的条目

failed_entries.bib：添加失败条目

failedPDFs.bib：PDF 下载失败条目

*+doi.bib：通过标题查询成功补全 DOI 的条目

🛠️ 常见问题
Q: 为什么有些 PDF 下载失败？
A: 脚本会依次尝试多个来源，若所有来源都失败，可能由于网络限制或文献尚未开放获取。可以稍后手动下载，或检查 PDF 下载源是否可用。

Q: 如何只添加文献不下载 PDF？
A: 在脚本中将 ENABLE_PDF_DOWNLOAD 设为 False。

Q: 如何提高下载成功率？
A: 确保网络能访问 Sci‑Hub、LibGen 等镜像站，必要时可更新 SCIHUB_MIRRORS 列表。

Q: Zotero API 限速怎么办？
A: 脚本已内置自动重试和延迟，遇到 429 状态码会等待后重试。若仍频繁受限，可适当增加 DELAY_SECONDS。

🤝 贡献
欢迎提交 Issue 和 Pull Request！如果你有好的 PDF 下载源或改进想法，请随时参与。

📄 许可
MIT License © 2025 [JieHeNO1]