#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import shutil
import logging
import bibtexparser
import requests
from pyzotero import zotero
from urllib.parse import quote, urlparse
from dotenv import load_dotenv   # 新增，用于加载 .env 文件

# 加载 .env 文件
load_dotenv()

# -------------------- 配置部分（从环境变量读取）--------------------
INPUT_DIR = "./bibs"
OUTPUT_DIR = "./output"
FAILED_BIB_FILE = "failed_entries.bib"
NO_DOI_BIB_FILE = "no_doi_entries.bib"
FAILED_PDF_BIB_FILE = "failedPDFs.bib"
GEN_BIBS_DIR = "./bibs/genbibs"
ZOTERO_STORAGE = "./output"

# 从环境变量读取敏感信息
ZOTERO_API_KEY = os.getenv("ZOTERO_API_KEY", "")
ZOTERO_LIBRARY_ID = os.getenv("ZOTERO_LIBRARY_ID", "")
ZOTERO_LIBRARY_TYPE = os.getenv("ZOTERO_LIBRARY_TYPE", "user")

UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "")

# 如果缺少必要信息，给出提示并退出
if not ZOTERO_API_KEY:
    print("错误：请设置环境变量 ZOTERO_API_KEY（可在 .env 文件中定义）")
    sys.exit(1)
if not UNPAYWALL_EMAIL:
    print("警告：未设置 UNPAYWALL_EMAIL，Unpaywall 功能可能受限")

# 其余配置保持原样
DELAY_SECONDS = 3
PDF_DOWNLOAD_TIMEOUT = 60
ENABLE_PDF_DOWNLOAD = True
PDF_MAX_RETRIES = 3
PDF_MIN_SIZE = 10240
TRY_FIND_DOI_BY_TITLE = True

SCIHUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.ren",
    "https://sci-hub.wf",
    "https://sci-hub.do",
    "https://sci-hub.hkvisa.net",
]

LIBGEN_MIRRORS = [
    "https://libgen.is",
    "https://libgen.st",
    "https://libgen.rs",
]

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 全局模板缓存
TEMPLATE_CACHE = {}

# ==================== 新增：可逆编码与解码函数 ====================
# 定义需要编码的非法字符（Windows 文件名非法字符集）
ILLEGAL_CHARS = r'\\/*?:"<>|'   # 注意反斜杠需转义，故写为 \\
# 生成映射字典 {字符: 编码形式}，编码为 _XX_ 格式，XX 为两位十六进制 ASCII 码（大写）
CHAR_TO_HEX = {ch: f"_{ord(ch):02X}_" for ch in ILLEGAL_CHARS}
# 解码正则：匹配 _XX_ 形式（XX 为两位十六进制大写）
DECODE_PATTERN = re.compile(r'_([0-9A-F]{2})_')

def encode_for_filename(text):
    """
    将字符串转换为安全的文件名（可逆），用于引用标签。
    将非法字符替换为 _XX_ 形式，空格替换为下划线，去除首尾特殊字符。
    """
    if not text:
        return "Untitled"
    encoded = text
    for ch, code in CHAR_TO_HEX.items():
        encoded = encoded.replace(ch, code)
    # 将空格（包括连续空格）替换为单个下划线
    encoded = re.sub(r'\s+', '_', encoded)
    # 去除首尾可能残留的下划线或点（防止文件名非法）
    encoded = encoded.strip('._')
    return encoded[:100] if encoded else "Untitled"

def decode_filename(encoded):
    """
    从编码后的文件名还原原始字符串。
    """
    if not encoded:
        return ""
    def repl(match):
        hex_code = match.group(1)
        try:
            return chr(int(hex_code, 16))
        except ValueError:
            return match.group(0)
    return DECODE_PATTERN.sub(repl, encoded)

def parse_pdf_filename(filename):
    """
    从 PDF 文件名解析出各组成部分，并还原引用标签。
    输入格式: {序号}#{编码后引用标签}#{年份}#{标题}.pdf
    返回: (序号, 原始引用标签, 年份, 标题) 或 None
    """
    if not filename.endswith('.pdf'):
        return None
    basename = filename[:-4]
    parts = basename.split('#')
    if len(parts) != 4:
        # 标题中可能包含 #，将多余部分合并回标题
        if len(parts) > 4:
            parts = [parts[0], parts[1], parts[2], '#'.join(parts[3:])]
        else:
            return None
    idx_str, encoded_key, year_str, title_encoded = parts
    if not idx_str.isdigit() or not year_str.isdigit():
        return None
    original_key = decode_filename(encoded_key)
    return int(idx_str), original_key, int(year_str), title_encoded

# ==================== 原有函数（保留 safe_filename 用于标题）====================
def safe_filename(text):
    """简单的安全化函数，用于标题（无需可逆）"""
    if not text:
        return "Untitled"
    text = re.sub(r'[\\/*?:"<>|]', '_', text)
    text = re.sub(r'\s+', '_', text)
    text = text.strip('._')
    return text[:100] if text else "Untitled"

def generate_pdf_filename(entry_index, citation_key, year, title):
    """
    生成PDF文件名，使用 # 分隔符，并对引用标签进行可逆编码。
    格式: {序号}#{编码后引用标签}#{年份}#{标题}.pdf
    例如: 001#WOS_3A_000322454500030#2023#Deep_Learning_Survey.pdf
    """
    safe_idx = f"{entry_index:03d}"
    safe_key = encode_for_filename(citation_key) if citation_key else "Unknown"
    safe_year = str(year) if year else "Unknown"
    safe_title = safe_filename(title)   # 标题仍然使用简单安全化
    return f"{safe_idx}#{safe_key}#{safe_year}#{safe_title}.pdf"


# ==================== 解析准确度校验函数 ====================

def count_entries_by_pattern(bib_path):
    """
    通过搜索 'abstract =' 或 'abstract=' 关键字来计算BIB文件中的条目数量
    这是一种更可靠的条目计数方法，因为每个学术文献通常都有abstract字段
    """
    try:
        with open(bib_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # 匹配 abstract = 或 abstract= (不区分大小写)
        abstract_pattern = re.compile(r'\babstract\s*=', re.IGNORECASE)
        abstract_matches = abstract_pattern.findall(content)
        count_by_abstract = len(abstract_matches)
        
        # 同时通过 @type{ 来计算条目数量（更准确的方法）
        entry_pattern = re.compile(r'@\w+\s*\{', re.IGNORECASE)
        entry_matches = entry_pattern.findall(content)
        count_by_entry = len(entry_matches)
        
        logger.info(f"  [校验] 通过 'abstract =' 匹配到 {count_by_abstract} 条")
        logger.info(f"  [校验] 通过 '@type{{' 匹配到 {count_by_entry} 条")
        
        return count_by_abstract, count_by_entry
    except Exception as e:
        logger.warning(f"  [校验] 计算条目数量失败: {e}")
        return 0, 0


def count_entries_by_braces(bib_path):
    """
    通过分析大括号结构来计算条目数量（更精确的方法）
    BibTeX条目格式: @type{key, ... }
    """
    try:
        with open(bib_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # 匹配所有条目类型
        entry_pattern = re.compile(r'@(\w+)\s*\{\s*([^,]+)\s*,', re.IGNORECASE)
        matches = entry_pattern.findall(content)
        
        # 过滤掉注释和字符串定义
        valid_entries = [(entry_type, key.strip()) for entry_type, key in matches 
                         if entry_type.lower() not in ['comment', 'string', 'preamble']]
        
        return len(valid_entries), valid_entries
    except Exception as e:
        logger.warning(f"  [校验] 大括号分析失败: {e}")
        return 0, []


def count_doi_entries(bib_path):
    """
    统计有DOI字段的条目数量
    """
    try:
        with open(bib_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # 匹配 DOI = {xxx} 或 doi = {xxx}
        doi_pattern = re.compile(r'\bdoi\s*=\s*\{', re.IGNORECASE)
        doi_matches = doi_pattern.findall(content)
        
        return len(doi_matches)
    except Exception as e:
        logger.warning(f"  [校验] DOI统计失败: {e}")
        return 0


def verify_parsing_accuracy(bib_path, parsed_count, entries_with_doi):
    """
    验证解析准确度，比较不同方法的计数结果
    返回: (是否准确, 原始条目数量, 差异说明)
    """
    count_by_abstract, count_by_entry = count_entries_by_pattern(bib_path)
    count_by_braces, entries_info = count_entries_by_braces(bib_path)
    count_by_doi = count_doi_entries(bib_path)
    
    logger.info(f"  [校验] 解析得到 {parsed_count} 条文献（有DOI）")
    logger.info(f"  [校验] 大括号分析得到 {count_by_braces} 条文献（总计）")
    logger.info(f"  [校验] 文件中有DOI字段的条目: {count_by_doi} 条")
    
    # 使用大括号分析作为最可靠的参考
    reference_count = count_by_braces if count_by_braces > 0 else count_by_entry
    
    # 计算无DOI的条目数量
    no_doi_count = reference_count - count_by_doi
    
    if no_doi_count > 0:
        logger.warning(f"  [校验] 发现 {no_doi_count} 条文献没有DOI字段")
    
    # 检查解析是否遗漏了有DOI的条目
    if entries_with_doi == count_by_doi:
        logger.info(f"  [校验] ✓ DOI条目解析完全匹配！")
        if no_doi_count > 0:
            logger.info(f"  [校验] ℹ 有 {no_doi_count} 条文献无DOI，已单独记录")
        return True, reference_count, f"解析准确，{no_doi_count}条无DOI"
    else:
        diff = count_by_doi - entries_with_doi
        if diff > 0:
            msg = f"可能遗漏了 {diff} 条有DOI的文献"
        else:
            msg = f"解析数量多于预期 {abs(diff)} 条"
        logger.warning(f"  [校验] ✗ 解析数量不匹配！{msg}")
        return False, reference_count, msg


# ==================== 通过标题查询DOI ====================

def find_doi_by_title(title, max_retries=2):
    """
    通过标题在CrossRef查询DOI
    """
    if not title or len(title.strip()) < 10:
        return None
    
    for attempt in range(max_retries):
        try:
            # 使用CrossRef的查询API
            query_url = f"https://api.crossref.org/works?query.title={quote(title)}&rows=1"
            headers = {
                "Accept": "application/json",
                "User-Agent": f"PyZotero/1.0 (mailto:{UNPAYWALL_EMAIL})"
            }
            
            response = requests.get(query_url, headers=headers, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                items = data.get("message", {}).get("items", [])
                
                if items:
                    item = items[0]
                    found_title = item.get("title", [""])[0].lower()
                    original_title = title.lower()
                    
                    # 清理标题进行比较
                    clean_found = re.sub(r'[^\w\s]', '', found_title)
                    clean_original = re.sub(r'[^\w\s]', '', original_title)
                    
                    # 计算标题相似度
                    from difflib import SequenceMatcher
                    similarity = SequenceMatcher(None, clean_found, clean_original).ratio()
                    
                    if similarity > 0.8:  # 相似度阈值
                        doi = item.get("DOI")
                        if doi:
                            logger.info(f"    [CrossRef查询] 找到DOI: {doi} (相似度: {similarity:.2f})")
                            return doi
                    else:
                        logger.info(f"    [CrossRef查询] 标题不匹配 (相似度: {similarity:.2f})")
            
            elif response.status_code == 429:
                wait_time = (attempt + 1) * 3
                logger.warning(f"    [CrossRef查询] 被限速，等待 {wait_time} 秒...")
                time.sleep(wait_time)
                continue
                
        except requests.exceptions.Timeout:
            logger.warning(f"    [CrossRef查询] 请求超时")
        except Exception as e:
            logger.warning(f"    [CrossRef查询] 错误: {e}")
        
        if attempt < max_retries - 1:
            time.sleep(1)
    
    return None


# ==================== 重复文献检测与去重 ====================

def detect_and_remove_duplicates(bib_path):
    """
    检测BIB文件中的重复文献，并创建去重后的_unique版本
    返回: (重复数量, 去重后的条目列表, 去重后文件路径)
    """
    try:
        with open(bib_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # 使用bibtexparser解析
        bib_db = bibtexparser.loads(content)
        original_entries = bib_db.entries
        original_count = len(original_entries)
        
        # 用于检测重复的字典
        seen_dois = {}
        seen_titles = {}
        unique_entries = []
        duplicates = []
        
        for entry in original_entries:
            doi = entry.get('doi', entry.get('DOI', '')).strip().lower()
            title = entry.get('title', entry.get('Title', '')).strip().lower()
            # 移除标题中的LaTeX格式和多余空格
            title = re.sub(r'[{}\\]', '', title)
            title = re.sub(r'\s+', ' ', title).strip()
            
            entry_key = entry.get('ID', '')
            
            is_duplicate = False
            duplicate_reason = ""
            
            # 检查DOI重复
            if doi and doi in seen_dois:
                is_duplicate = True
                duplicate_reason = f"DOI重复: {doi} (与 {seen_dois[doi]} 重复)"
            elif doi:
                seen_dois[doi] = entry_key
            
            # 检查标题重复（如果DOI不存在或DOI检查未发现重复）
            if not is_duplicate and title:
                # 标题相似度检查（完全匹配或高度相似）
                normalized_title = re.sub(r'[^a-z0-9]', '', title)
                for seen_title, seen_key in seen_titles.items():
                    normalized_seen = re.sub(r'[^a-z0-9]', '', seen_title)
                    if normalized_title == normalized_seen:
                        is_duplicate = True
                        duplicate_reason = f"标题重复: {title[:50]}... (与 {seen_key} 重复)"
                        break
                
                if not is_duplicate:
                    seen_titles[title] = entry_key
            
            if is_duplicate:
                duplicates.append({
                    'key': entry_key,
                    'reason': duplicate_reason,
                    'entry': entry
                })
            else:
                unique_entries.append(entry)
        
        duplicate_count = len(duplicates)
        
        if duplicate_count > 0:
            logger.info(f"  [去重] 发现 {duplicate_count} 条重复文献:")
            for dup in duplicates[:10]:  # 只显示前10条
                logger.info(f"    - {dup['key']}: {dup['reason']}")
            if duplicate_count > 10:
                logger.info(f"    ... 还有 {duplicate_count - 10} 条重复")
            
            # 创建去重后的文件
            base_name = os.path.splitext(os.path.basename(bib_path))[0]
            # 确保生成目录存在
            os.makedirs(GEN_BIBS_DIR, exist_ok=True)
            unique_bib_path = os.path.join(
                GEN_BIBS_DIR, 
                f"{base_name}_unique.bib"
            )
            
            # 写入去重后的内容
            unique_db = bibtexparser.bibdatabase.BibDatabase()
            unique_db.entries = unique_entries
            
            # 保留原文件的注释和字符串定义
            if hasattr(bib_db, 'comments'):
                unique_db.comments = bib_db.comments
            if hasattr(bib_db, 'strings'):
                unique_db.strings = bib_db.strings
            if hasattr(bib_db, 'preambles'):
                unique_db.preambles = bib_db.preambles
            
            with open(unique_bib_path, 'w', encoding='utf-8') as f:
                bibtexparser.dump(unique_db, f)
            
            logger.info(f"  [去重] 已创建去重文件: {unique_bib_path}")
            logger.info(f"  [去重] 原始条目: {original_count}, 去重后: {len(unique_entries)}")
        else:
            logger.info(f"  [去重] 未发现重复文献")
            unique_bib_path = None
        
        return duplicate_count, unique_entries, unique_bib_path
        
    except Exception as e:
        logger.error(f"  [去重] 处理失败: {e}")
        return 0, [], None


def extract_entries_from_bib(bib_path):
    """
    从BIB文件中提取条目，区分有DOI和无DOI的条目
    返回: (有DOI的条目列表, 无DOI的条目列表)
    """
    with open(bib_path, encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    bib_db = bibtexparser.loads(content)
    entries_with_doi = []
    entries_without_doi = []
    
    for entry in bib_db.entries:
        doi = entry.get('doi', entry.get('DOI', '')).strip()
        title = entry.get('title', entry.get('Title', '')).strip()
        year = entry.get('year', entry.get('Year', '')).strip()
        key = entry.get('ID', '')
        
        if doi:
            doi = doi.lower().strip()
            entries_with_doi.append({
                'key': key,
                'title': title,
                'year': year,
                'doi': doi,
                'raw_entry': entry
            })
        else:
            entries_without_doi.append({
                'key': key,
                'title': title,
                'year': year,
                'raw_entry': entry
            })
    
    logger.info(f"从 {bib_path} 中提取到 {len(entries_with_doi)} 条有 DOI 的文献。")
    logger.info(f"从 {bib_path} 中提取到 {len(entries_without_doi)} 条无 DOI 的文献。")
    
    # 验证解析准确度
    is_accurate, ref_count, msg = verify_parsing_accuracy(
        bib_path, 
        len(entries_with_doi) + len(entries_without_doi),
        len(entries_with_doi)
    )
    
    if not is_accurate:
        logger.warning(f"  [警告] 解析可能不完整，请检查文件格式！")
    
    return entries_with_doi, entries_without_doi


def get_zotero_attachment_path(zot, item_key, storage_root):
    try:
        child_items = zot.children(item_key)
        for child in child_items:
            if child['data'].get('itemType') == 'attachment' and child['data'].get('contentType') == 'application/pdf':
                attachment_key = child['key']
                folder = os.path.join(storage_root, attachment_key)
                if os.path.isdir(folder):
                    for fname in os.listdir(folder):
                        if fname.lower().endswith('.pdf'):
                            return os.path.join(folder, fname)
    except Exception as e:
        logger.warning(f"    获取附件路径失败: {e}")
    return None


def copy_and_rename_pdf(src_path, dest_dir, filename):
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{base}_{counter}{ext}")
            counter += 1
    shutil.copy2(src_path, dest_path)
    return dest_path


# ==================== 模板管理 ====================

def get_item_template(zot, item_type):
    """
    获取 Zotero 条目模板（带缓存）
    """
    global TEMPLATE_CACHE
    
    # 检查缓存
    if item_type in TEMPLATE_CACHE:
        return TEMPLATE_CACHE[item_type].copy()
    
    # 尝试获取模板
    max_retries = 3
    for attempt in range(max_retries):
        try:
            template = zot.item_template(item_type)
            if template:
                TEMPLATE_CACHE[item_type] = template.copy()
                return template
        except Exception as e:
            error_msg = str(e)
            if "403" in error_msg or "rate" in error_msg.lower():
                wait_time = (attempt + 1) * 5
                logger.warning(f"    模板获取被限速，等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                logger.warning(f"    获取模板失败 ({item_type}): {e}")
                break
    
    # 如果获取失败，使用预定义的默认模板
    return get_default_template(item_type)


def get_default_template(item_type):
    """
    返回预定义的默认模板
    """
    default_templates = {
        "journalArticle": {
            "itemType": "journalArticle",
            "title": "",
            "creators": [],
            "abstractNote": "",
            "publicationTitle": "",
            "volume": "",
            "issue": "",
            "pages": "",
            "date": "",
            "series": "",
            "seriesTitle": "",
            "seriesText": "",
            "journalAbbreviation": "",
            "language": "",
            "DOI": "",
            "ISSN": "",
            "shortTitle": "",
            "url": "",
            "accessDate": "",
            "archive": "",
            "archiveLocation": "",
            "libraryCatalog": "",
            "callNumber": "",
            "rights": "",
            "extra": "",
            "tags": [],
            "collections": [],
            "relations": {},
        },
        "book": {
            "itemType": "book",
            "title": "",
            "creators": [],
            "abstractNote": "",
            "series": "",
            "seriesNumber": "",
            "volume": "",
            "numberOfVolumes": "",
            "edition": "",
            "place": "",
            "publisher": "",
            "date": "",
            "numPages": "",
            "language": "",
            "ISBN": "",
            "shortTitle": "",
            "url": "",
            "accessDate": "",
            "archive": "",
            "archiveLocation": "",
            "libraryCatalog": "",
            "callNumber": "",
            "rights": "",
            "extra": "",
            "tags": [],
            "collections": [],
            "relations": {},
        },
        "conferencePaper": {
            "itemType": "conferencePaper",
            "title": "",
            "creators": [],
            "abstractNote": "",
            "date": "",
            "proceedingsTitle": "",
            "conferenceName": "",
            "place": "",
            "publisher": "",
            "volume": "",
            "pages": "",
            "series": "",
            "language": "",
            "DOI": "",
            "ISBN": "",
            "shortTitle": "",
            "url": "",
            "accessDate": "",
            "archive": "",
            "archiveLocation": "",
            "libraryCatalog": "",
            "callNumber": "",
            "rights": "",
            "extra": "",
            "tags": [],
            "collections": [],
            "relations": {},
        },
        "thesis": {
            "itemType": "thesis",
            "title": "",
            "creators": [],
            "abstractNote": "",
            "thesisType": "",
            "university": "",
            "place": "",
            "date": "",
            "numPages": "",
            "language": "",
            "shortTitle": "",
            "url": "",
            "accessDate": "",
            "archive": "",
            "archiveLocation": "",
            "libraryCatalog": "",
            "callNumber": "",
            "rights": "",
            "extra": "",
            "tags": [],
            "collections": [],
            "relations": {},
        },
        "bookSection": {
            "itemType": "bookSection",
            "title": "",
            "creators": [],
            "abstractNote": "",
            "bookTitle": "",
            "series": "",
            "seriesNumber": "",
            "volume": "",
            "numberOfVolumes": "",
            "edition": "",
            "place": "",
            "publisher": "",
            "date": "",
            "pages": "",
            "language": "",
            "ISBN": "",
            "shortTitle": "",
            "url": "",
            "accessDate": "",
            "archive": "",
            "archiveLocation": "",
            "libraryCatalog": "",
            "callNumber": "",
            "rights": "",
            "extra": "",
            "tags": [],
            "collections": [],
            "relations": {},
        },
        "report": {
            "itemType": "report",
            "title": "",
            "creators": [],
            "abstractNote": "",
            "reportNumber": "",
            "reportType": "",
            "seriesTitle": "",
            "place": "",
            "institution": "",
            "date": "",
            "pages": "",
            "language": "",
            "shortTitle": "",
            "url": "",
            "accessDate": "",
            "archive": "",
            "archiveLocation": "",
            "libraryCatalog": "",
            "callNumber": "",
            "rights": "",
            "extra": "",
            "tags": [],
            "collections": [],
            "relations": {},
        },
        "preprint": {
            "itemType": "preprint",
            "title": "",
            "creators": [],
            "abstractNote": "",
            "date": "",
            "language": "",
            "shortTitle": "",
            "url": "",
            "accessDate": "",
            "archive": "",
            "archiveLocation": "",
            "libraryCatalog": "",
            "callNumber": "",
            "rights": "",
            "extra": "",
            "tags": [],
            "collections": [],
            "relations": {},
            "DOI": "",
            "repository": "",
        },
    }
    
    if item_type in default_templates:
        logger.info(f"    使用预定义模板: {item_type}")
        return default_templates[item_type].copy()
    
    # 最终回退到 journalArticle
    logger.warning(f"    未知类型 {item_type}，使用 journalArticle 模板")
    return default_templates["journalArticle"].copy()


# ==================== PDF 下载源函数 ====================

def get_unpaywall_pdf_url(doi, email):
    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data.get("is_oa"):
                best_oa = data.get("best_oa_location", {}) or {}
                pdf_url = best_oa.get("url_for_pdf") or best_oa.get("url")
                if pdf_url:
                    logger.info(f"    [Unpaywall] 找到开放获取 PDF")
                    return pdf_url
            logger.info(f"    [Unpaywall] 无开放获取版本")
        elif response.status_code == 404:
            logger.info(f"    [Unpaywall] DOI 不存在")
        else:
            logger.warning(f"    [Unpaywall] 状态码: {response.status_code}")
    except requests.exceptions.Timeout:
        logger.warning(f"    [Unpaywall] 请求超时")
    except Exception as e:
        logger.warning(f"    [Unpaywall] 错误: {e}")
    return None


def get_semantic_scholar_pdf_url(doi):
    try:
        api_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf,isOpenAccess"
        response = requests.get(api_url, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("isOpenAccess") and data.get("openAccessPdf"):
                pdf_url = data["openAccessPdf"].get("url")
                if pdf_url:
                    logger.info(f"    [Semantic Scholar] 找到开放获取 PDF")
                    return pdf_url
            logger.info(f"    [Semantic Scholar] 无开放获取版本")
        elif response.status_code == 404:
            logger.info(f"    [Semantic Scholar] DOI 不存在")
        else:
            logger.warning(f"    [Semantic Scholar] 状态码: {response.status_code}")
    except requests.exceptions.Timeout:
        logger.warning(f"    [Semantic Scholar] 请求超时")
    except Exception as e:
        logger.warning(f"    [Semantic Scholar] 错误: {e}")
    return None


def get_doi_direct_pdf_url(doi):
    try:
        doi_url = f"https://doi.org/{doi}"
        headers = DEFAULT_HEADERS.copy()
        headers["Accept"] = "application/pdf,text/html,*/*"
        
        response = requests.get(doi_url, headers=headers, timeout=15, allow_redirects=True)
        
        final_url = response.url
        if final_url.lower().endswith('.pdf') or 'pdf' in final_url.lower():
            logger.info(f"    [DOI Direct] 重定向到 PDF")
            return final_url
        
        content_type = response.headers.get('Content-Type', '')
        if 'pdf' in content_type.lower():
            logger.info(f"    [DOI Direct] 返回 PDF 内容")
            return response.url
        
        if 'text/html' in content_type:
            pdf_patterns = [
                r'href=["\']([^"\']*\.pdf[^"\']*)["\']',
                r'content=["\']([^"\']*\.pdf[^"\']*)["\']',
            ]
            for pattern in pdf_patterns:
                matches = re.findall(pattern, response.text, re.IGNORECASE)
                for match in matches:
                    if match.startswith('http'):
                        logger.info(f"    [DOI Direct] 从页面提取 PDF 链接")
                        return match
                    elif match.startswith('/'):
                        parsed = urlparse(response.url)
                        return f"{parsed.scheme}://{parsed.netloc}{match}"
        
        logger.info(f"    [DOI Direct] 未找到 PDF 链接")
    except requests.exceptions.Timeout:
        logger.warning(f"    [DOI Direct] 请求超时")
    except Exception as e:
        logger.warning(f"    [DOI Direct] 错误: {e}")
    return None


def get_scihub_pdf_url(doi):
    for mirror in SCIHUB_MIRRORS:
        try:
            scihub_url = f"{mirror}/{doi}"
            logger.info(f"    [Sci-Hub] 尝试镜像: {mirror}")
            
            response = requests.get(scihub_url, headers=DEFAULT_HEADERS, timeout=20, allow_redirects=True)
            
            if response.status_code == 200:
                content = response.text
                
                patterns = [
                    r'<embed[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']',
                    r'<iframe[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']',
                    r'(https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*)',
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, content, re.IGNORECASE)
                    if match:
                        pdf_url = match.group(1)
                        if pdf_url.startswith('//'):
                            pdf_url = 'https:' + pdf_url
                        elif pdf_url.startswith('/'):
                            pdf_url = mirror + pdf_url
                        elif not pdf_url.startswith('http'):
                            pdf_url = mirror + '/' + pdf_url
                        logger.info(f"    [Sci-Hub] 找到 PDF 链接")
                        return pdf_url, mirror
                        
            elif response.status_code == 429:
                logger.warning(f"    [Sci-Hub] 请求过于频繁")
                time.sleep(5)
            else:
                logger.warning(f"    [Sci-Hub] 状态码: {response.status_code}")
                
        except requests.exceptions.Timeout:
            logger.warning(f"    [Sci-Hub] 连接超时")
        except requests.exceptions.ConnectionError:
            logger.warning(f"    [Sci-Hub] 连接失败")
        except Exception as e:
            logger.warning(f"    [Sci-Hub] 错误: {e}")
        
        time.sleep(1)
    
    logger.warning(f"    [Sci-Hub] 所有镜像均失败")
    return None, None


def get_libgen_pdf_url(doi):
    for mirror in LIBGEN_MIRRORS:
        try:
            logger.info(f"    [LibGen] 尝试镜像: {mirror}")
            search_url = f"{mirror}/search.php?req={quote(doi)}&view=simple&column=doi"
            response = requests.get(search_url, headers=DEFAULT_HEADERS, timeout=15, allow_redirects=True)
            
            if response.status_code == 200:
                download_patterns = [
                    r'href=["\']([^"\']*download[^"\']*)["\']',
                    r'href=["\']([^"\']*\.pdf[^"\']*)["\']',
                ]
                
                for pattern in download_patterns:
                    match = re.search(pattern, response.text, re.IGNORECASE)
                    if match:
                        pdf_url = match.group(1)
                        if not pdf_url.startswith('http'):
                            pdf_url = mirror + '/' + pdf_url
                        logger.info(f"    [LibGen] 找到 PDF 链接")
                        return pdf_url, mirror
                        
        except requests.exceptions.Timeout:
            logger.warning(f"    [LibGen] 连接超时")
        except Exception as e:
            logger.warning(f"    [LibGen] 错误: {e}")
        
        time.sleep(1)
    
    logger.warning(f"    [LibGen] 所有镜像均失败")
    return None, None


def get_pmc_pdf_url(doi):
    try:
        api_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={doi}&format=json"
        response = requests.get(api_url, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            records = data.get("records", [])
            if records and records[0].get("pmcid"):
                pmcid = records[0]["pmcid"]
                pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
                logger.info(f"    [PMC] 找到 PDF: {pmcid}")
                return pdf_url
            logger.info(f"    [PMC] 不在 PMC 中")
        else:
            logger.warning(f"    [PMC] 状态码: {response.status_code}")
    except Exception as e:
        logger.warning(f"    [PMC] 错误: {e}")
    return None


def get_arxiv_pdf_url(doi):
    try:
        if 'arxiv' not in doi.lower():
            return None
        
        arxiv_id = None
        patterns = [
            r'arxiv\.org/abs/(\d+\.\d+)',
            r'arxiv:(\d+\.\d+)',
            r'10\.48550/arxiv\.(\d+\.\d+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, doi, re.IGNORECASE)
            if match:
                arxiv_id = match.group(1)
                break
        
        if arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            response = requests.head(pdf_url, timeout=10, allow_redirects=True)
            if response.status_code == 200:
                logger.info(f"    [arXiv] 找到 PDF: {arxiv_id}")
                return pdf_url
        
        logger.info(f"    [arXiv] 不是 arXiv 文献")
    except Exception as e:
        logger.warning(f"    [arXiv] 错误: {e}")
    return None


# ==================== PDF 验证函数 ====================

def verify_pdf_file(pdf_path):
    """
    验证PDF文件是否完整和可读
    返回: (是否有效, 错误信息)
    """
    if not pdf_path or not os.path.exists(pdf_path):
        return False, "文件不存在"
    
    # 检查文件大小
    file_size = os.path.getsize(pdf_path)
    if file_size < PDF_MIN_SIZE:
        return False, f"文件太小 ({file_size} bytes < {PDF_MIN_SIZE} bytes)"
    
    try:
        with open(pdf_path, 'rb') as f:
            # 读取文件头
            header = f.read(5)
            if header != b'%PDF-':
                return False, "文件头不是有效的PDF格式"
            
            # 读取文件尾检查 %%EOF
            f.seek(0, 2)  # 移动到文件末尾
            file_size = f.tell()
            
            # 检查最后1024字节中是否有 %%EOF
            seek_pos = max(0, file_size - 1024)
            f.seek(seek_pos)
            tail_content = f.read()
            
            if b'%%EOF' not in tail_content:
                return False, "文件尾缺少%%EOF标记，PDF可能不完整"
            
            # 尝试使用PyPDF2验证可读性（如果可用）
            try:
                from PyPDF2 import PdfReader
                f.seek(0)
                reader = PdfReader(f)
                num_pages = len(reader.pages)
                if num_pages == 0:
                    return False, "PDF没有页面"
                # 尝试读取第一页
                _ = reader.pages[0]
                logger.info(f"    [PDF验证] 有效，共 {num_pages} 页")
                return True, f"有效PDF，共{num_pages}页"
            except ImportError:
                # PyPDF2不可用，仅通过文件头尾验证
                logger.info(f"    [PDF验证] 有效（仅文件头尾验证）")
                return True, "有效PDF（仅文件头尾验证）"
            except Exception as e:
                return False, f"PDF解析失败: {str(e)}"
                
    except Exception as e:
        return False, f"文件读取失败: {str(e)}"


def download_pdf_with_verify(pdf_url, output_path, timeout=PDF_DOWNLOAD_TIMEOUT, referer=None, max_retries=PDF_MAX_RETRIES):
    """
    下载PDF并验证，如果验证失败则重试
    返回: (下载路径, 是否有效, 错误信息)
    """
    headers = DEFAULT_HEADERS.copy()
    headers["Accept"] = "application/pdf,application/x-pdf,*/*"
    if referer:
        headers["Referer"] = referer
    
    last_error = ""
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                logger.info(f"    [重试 {attempt}/{max_retries}] 重新下载PDF...")
                time.sleep(2)  # 重试前等待
            
            response = requests.get(pdf_url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
            
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '')
                content_disp = response.headers.get('Content-Disposition', '')
                
                is_pdf = (
                    'pdf' in content_type.lower() or 
                    pdf_url.lower().endswith('.pdf') or
                    '.pdf' in content_disp.lower()
                )
                
                if is_pdf:
                    content_length = response.headers.get('Content-Length')
                    if content_length and int(content_length) < PDF_MIN_SIZE:
                        last_error = f"文件太小 ({content_length} bytes)"
                        continue
                    
                    # 确保输出目录存在
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                    
                    # 下载文件
                    downloaded = 0
                    with open(output_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                    
                    if downloaded < PDF_MIN_SIZE:
                        last_error = f"下载文件太小 ({downloaded} bytes)"
                        try:
                            os.unlink(output_path)
                        except:
                            pass
                        continue
                    
                    # 验证PDF
                    is_valid, msg = verify_pdf_file(output_path)
                    
                    if is_valid:
                        logger.info(f"    PDF下载并验证成功 ({downloaded/1024:.1f} KB)")
                        return output_path, True, msg
                    else:
                        last_error = msg
                        logger.warning(f"    PDF验证失败: {msg}")
                        # 删除无效文件，准备重试
                        try:
                            os.unlink(output_path)
                        except:
                            pass
                        continue
                else:
                    last_error = f"响应不是PDF: {content_type}"
            else:
                last_error = f"下载失败，状态码: {response.status_code}"
                
        except requests.exceptions.Timeout:
            last_error = "下载超时"
        except requests.exceptions.ConnectionError:
            last_error = "连接失败"
        except Exception as e:
            last_error = f"下载错误: {str(e)}"
    
    return None, False, last_error


def download_pdf(pdf_url, timeout=PDF_DOWNLOAD_TIMEOUT, referer=None, output_path=None):
    headers = DEFAULT_HEADERS.copy()
    headers["Accept"] = "application/pdf,application/x-pdf,*/*"
    if referer:
        headers["Referer"] = referer
    
    try:
        response = requests.get(pdf_url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
        
        if response.status_code == 200:
            content_type = response.headers.get('Content-Type', '')
            content_disp = response.headers.get('Content-Disposition', '')
            
            is_pdf = (
                'pdf' in content_type.lower() or 
                pdf_url.lower().endswith('.pdf') or
                '.pdf' in content_disp.lower()
            )
            
            if is_pdf:
                content_length = response.headers.get('Content-Length')
                if content_length and int(content_length) < 10240:
                    logger.warning(f"    文件太小，可能不是有效 PDF")
                    return None
                
                if output_path is None:
                    os.makedirs(OUTPUT_DIR, exist_ok=True)
                    timestamp = int(time.time() * 1000)
                    output_path = os.path.abspath(os.path.join(OUTPUT_DIR, f"temp_{timestamp}.pdf"))
                else:
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                
                downloaded = 0
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                
                if downloaded < 10240:
                    logger.warning(f"    文件太小 ({downloaded} bytes)")
                    try:
                        os.unlink(output_path)
                    except:
                        pass
                    return None
                
                logger.info(f"    PDF 下载成功 ({downloaded/1024:.1f} KB)")
                return output_path
            else:
                logger.warning(f"    响应不是 PDF: {content_type}")
        else:
            logger.warning(f"    下载失败，状态码: {response.status_code}")
    except requests.exceptions.Timeout:
        logger.warning(f"    下载超时")
    except requests.exceptions.ConnectionError:
        logger.warning(f"    连接失败")
    except Exception as e:
        logger.warning(f"    下载错误: {e}")
    return None


def attach_pdf_to_zotero(zot, item_key, pdf_path, filename):
    try:
        pdf_path = os.path.abspath(pdf_path)
        if not os.path.isfile(pdf_path):
            logger.error(f"    PDF 文件不存在: {pdf_path}")
            return False
        
        result = zot.attachment_both(pdf_path, filename, item_key)
        if result:
            logger.info(f"    PDF 已附加到 Zotero")
            return True
        else:
            logger.error(f"    PDF 附加返回空结果")
            return False
    except Exception as e:
        logger.error(f"    附加 PDF 失败: {e}")
        return False


def try_all_pdf_sources(zot, doi, target_path, target_filename, item_key):
    """
    尝试从多个源下载PDF，并进行验证
    返回: (是否成功, 失败原因列表)
    """
    sources = [
        ("Unpaywall", lambda: get_unpaywall_pdf_url(doi, UNPAYWALL_EMAIL)),
        ("Semantic Scholar", lambda: get_semantic_scholar_pdf_url(doi)),
        ("DOI Direct", lambda: get_doi_direct_pdf_url(doi)),
        ("PMC", lambda: get_pmc_pdf_url(doi)),
        ("arXiv", lambda: get_arxiv_pdf_url(doi)),
        ("Sci-Hub", lambda: get_scihub_pdf_url(doi)[0] if get_scihub_pdf_url(doi)[0] else None),
        ("LibGen", lambda: get_libgen_pdf_url(doi)[0] if get_libgen_pdf_url(doi)[0] else None),
    ]
    
    failed_reasons = []
    
    for source_name, get_url_func in sources:
        try:
            logger.info(f"    尝试 [{source_name}]...")
            pdf_url = get_url_func()
            
            if pdf_url:
                logger.info(f"    正在从 [{source_name}] 下载...")
                # 使用带验证的下载函数
                pdf_path, is_valid, msg = download_pdf_with_verify(pdf_url, target_path)
                
                if pdf_path and is_valid:
                    if attach_pdf_to_zotero(zot, item_key, pdf_path, target_filename):
                        logger.info(f"    PDF 已保存并验证: {pdf_path}")
                        return True, []
                    else:
                        # PDF下载成功但附加失败，仍然算成功
                        logger.info(f"    PDF 已下载但附加失败: {pdf_path}")
                        return True, []
                else:
                    failed_reasons.append(f"[{source_name}]: {msg}")
                    logger.warning(f"    [{source_name}] 下载或验证失败: {msg}")
        except Exception as e:
            failed_reasons.append(f"[{source_name}]: {str(e)}")
            logger.warning(f"    [{source_name}] 错误: {e}")
        
        time.sleep(0.5)
    
    return False, failed_reasons


# ==================== 主处理函数 ====================

def map_crossref_to_zotero_type(crossref_type):
    type_map = {
        "journal-article": "journalArticle",
        "book": "book",
        "book-chapter": "bookSection",
        "proceedings-article": "conferencePaper",
        "dissertation": "thesis",
        "report": "report",
        "posted-content": "preprint",
        "peer-review": "journalArticle",
        "standard": "report",
        "dataset": "report",
        "monograph": "book",
        "reference-entry": "bookSection",
        "book-series": "book",
        "book-set": "book",
        "book-track": "bookSection",
        "component": "report",
        "journal": "journalArticle",
        "journal-issue": "journalArticle",
        "journal-volume": "journalArticle",
        "proceedings": "conferencePaper",
        "proceedings-series": "conferencePaper",
        "reference-book": "book",
        "edited-book": "book",
        "other": "report",
        "preprint": "preprint",
        "grant": "report",
        "software": "report",
    }
    
    zotero_type = type_map.get(crossref_type)
    if zotero_type:
        return zotero_type
    
    logger.warning(f"    未知类型: {crossref_type}，使用 journalArticle")
    return "journalArticle"


def add_item_by_doi(zot, doi, max_retries=3):
    """通过 DOI 添加条目到 Zotero（带重试机制）"""
    
    for attempt in range(max_retries):
        try:
            # 1. 从 CrossRef 获取元数据
            crossref_url = f"https://api.crossref.org/works/{doi}"
            headers = {
                "Accept": "application/json",
                "User-Agent": f"PyZotero/1.0 (mailto:{UNPAYWALL_EMAIL})"
            }
            response = requests.get(crossref_url, headers=headers, timeout=30)
            
            if response.status_code != 200:
                logger.warning(f"    CrossRef 状态码: {response.status_code}")
                if response.status_code == 429:
                    wait_time = (attempt + 1) * 10
                    logger.warning(f"    被限速，等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                    continue
                return None
            
            data = response.json().get("message", {})
            
            # 2. 确定条目类型
            crossref_type = data.get("type", "journal-article")
            zotero_type = map_crossref_to_zotero_type(crossref_type)
            
            # 3. 获取模板（使用缓存）
            template = get_item_template(zot, zotero_type)
            
            # 4. 填充字段
            template["DOI"] = doi
            
            if data.get("title"):
                template["title"] = data["title"][0]
            
            date_parts = (data.get("published-print") or 
                          data.get("published-online") or 
                          data.get("published") or
                          data.get("created"))
            if date_parts and date_parts.get("date-parts"):
                parts = date_parts["date-parts"][0]
                if parts:
                    template["date"] = str(parts[0])
            
            if data.get("author"):
                template["creators"] = []
                for author in data["author"]:
                    creator = {
                        "creatorType": "author",
                        "firstName": author.get("given", ""),
                        "lastName": author.get("family", "")
                    }
                    template["creators"].append(creator)
            
            if data.get("container-title"):
                template["publicationTitle"] = data["container-title"][0]
            
            if data.get("volume"):
                template["volume"] = str(data["volume"])
            if data.get("issue"):
                template["issue"] = str(data["issue"])
            if data.get("page"):
                template["pages"] = str(data["page"])
            
            if data.get("URL"):
                template["url"] = data["URL"]
            
            if data.get("abstract"):
                abstract = re.sub(r'<[^>]+>', '', data["abstract"])
                template["abstractNote"] = abstract[:65535]
            
            if data.get("ISSN"):
                template["ISSN"] = data["ISSN"][0]
            if data.get("ISBN"):
                template["ISBN"] = data["ISBN"][0]
            
            if data.get("language"):
                template["language"] = data["language"]
            
            # 5. 创建条目
            result = zot.create_items([template])
            
            # 6. 提取 key
            if result and "successful" in result:
                successful = result["successful"]
                if successful:
                    first_item = list(successful.values())[0]
                    return first_item.get("key")
            elif result and isinstance(result, list) and len(result) > 0:
                return result[0].get("key")
                
        except requests.exceptions.RequestException as e:
            logger.error(f"    网络请求失败: {e}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5
                logger.warning(f"    等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
        except Exception as e:
            error_msg = str(e)
            if "403" in error_msg or "rate" in error_msg.lower() or "limit" in error_msg.lower():
                wait_time = (attempt + 1) * 10
                logger.warning(f"    API 限速，等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                logger.error(f"    添加失败: {e}")
                return None
    
    return None


def process_pdf_for_item(zot, item_key, doi, entry_index, citation_key, year, title):
    """
    处理PDF文件，使用新的命名格式
    返回: (是否成功, 失败原因列表)
    """
    target_filename = generate_pdf_filename(entry_index, citation_key, year, title)
    target_path = os.path.join(OUTPUT_DIR, target_filename)
    
    src_pdf = get_zotero_attachment_path(zot, item_key, ZOTERO_STORAGE)
    if src_pdf and os.path.isfile(src_pdf):
        # 验证本地PDF
        is_valid, msg = verify_pdf_file(src_pdf)
        if is_valid:
            dest_path = copy_and_rename_pdf(src_pdf, OUTPUT_DIR, target_filename)
            logger.info(f"    本地 PDF 已复制并验证: {dest_path}")
            return True, []
        else:
            logger.warning(f"    本地 PDF 验证失败: {msg}")
            # 继续尝试下载
    
    if not ENABLE_PDF_DOWNLOAD:
        logger.info(f"    PDF 下载已禁用")
        return False, ["PDF下载已禁用"]
    
    return try_all_pdf_sources(zot, doi, target_path, target_filename, item_key)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    try:
        zot_client = zotero.Zotero(ZOTERO_LIBRARY_ID, ZOTERO_LIBRARY_TYPE, ZOTERO_API_KEY)
        logger.info("Zotero 客户端初始化成功。")
    except Exception as e:
        logger.error(f"Zotero 客户端初始化失败: {e}")
        return

    try:
        key_info = zot_client.key_info()
        access = key_info.get('access', {}).get('user', {})
        if not access.get('write', False):
            logger.error("API Key 没有写入权限！")
            return
        if not access.get('files', False):
            logger.error("API Key 没有文件上传权限！")
            return
        logger.info(f"API Key 权限验证通过")
    except Exception as e:
        logger.warning(f"无法验证 API Key 权限: {e}")

    # 预加载模板到缓存
    logger.info("预加载模板...")
    for item_type in ["journalArticle", "book", "conferencePaper", "thesis", "bookSection", "report", "preprint"]:
        try:
            get_item_template(zot_client, item_type)
            time.sleep(0.5)
        except:
            pass
    logger.info("模板预加载完成")

    if not os.path.exists(INPUT_DIR):
        logger.error(f"输入目录不存在: {INPUT_DIR}")
        return
        
    bib_files = sorted([f for f in os.listdir(INPUT_DIR) if f.lower().endswith('.bib')])
    if not bib_files:
        logger.error(f"未找到 .bib 文件")
        return

    logger.info(f"共找到 {len(bib_files)} 个 BibTeX 文件")
    logger.info(f"PDF 下载功能: {'已启用' if ENABLE_PDF_DOWNLOAD else '已禁用'}")
    logger.info(f"PDF 文件名格式: 序号#引用标签#年份#标题.pdf")
    logger.info(f"尝试通过标题查询DOI: {'已启用' if TRY_FIND_DOI_BY_TITLE else '已禁用'}")
    
    # ==================== 步骤1：重复文献检测与去重 ====================
    logger.info("=" * 60)
    logger.info("步骤1：检测重复文献并生成去重文件...")
    logger.info("=" * 60)
    
    total_duplicates = 0
    unique_bib_paths = {}  # 存储去重后的文件路径 {原文件名: 去重后路径}
    
    for bib_file in bib_files:
        bib_path = os.path.join(INPUT_DIR, bib_file)
        logger.info(f"检查文件: {bib_file}")
        
        dup_count, unique_entries, unique_path = detect_and_remove_duplicates(bib_path)
        total_duplicates += dup_count
        
        if unique_path:
            unique_bib_paths[bib_file] = unique_path
        else:
            # 如果没有重复，原文件就是去重后的文件
            unique_bib_paths[bib_file] = bib_path
    
    logger.info(f"重复文献检测完成，共发现 {total_duplicates} 条重复")
    logger.info("=" * 60)
    
    # ==================== 步骤2：提取无DOI条目并保存 ====================
    logger.info("=" * 60)
    logger.info("步骤2：提取无DOI条目并保存到 no_doi_entries2.bib...")
    logger.info("=" * 60)
    
    all_no_doi_entries = []
    all_doi_found_entries = {}  # {源文件名: [找到DOI的条目列表]}
    alldoi_bib_paths = {}  # {原文件名: _alldoi文件路径}
    
    for bib_file, unique_path in unique_bib_paths.items():
        logger.info(f"处理去重文件: {unique_path}")
        
        # 从去重文件中提取条目
        with open(unique_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        bib_db = bibtexparser.loads(content)
        
        entries_with_doi = []
        entries_without_doi = []
        
        for entry in bib_db.entries:
            doi = entry.get('doi', entry.get('DOI', '')).strip()
            if doi:
                entries_with_doi.append(entry)
            else:
                entries_without_doi.append(entry)
        
        logger.info(f"  有DOI条目: {len(entries_with_doi)} 条")
        logger.info(f"  无DOI条目: {len(entries_without_doi)} 条")
        
        # 收集所有无DOI条目
        all_no_doi_entries.extend(entries_without_doi)
        
        # 复制 _unique.bib 为 _unique_alldoi.bib
        base_name = os.path.splitext(os.path.basename(unique_path))[0]
        alldoi_bib_name = f"{base_name}_alldoi.bib"
        # 确保生成目录存在
        os.makedirs(GEN_BIBS_DIR, exist_ok=True)
        alldoi_bib_path = os.path.join(GEN_BIBS_DIR, alldoi_bib_name)
        shutil.copy2(unique_path, alldoi_bib_path)
        alldoi_bib_paths[bib_file] = alldoi_bib_path
        logger.info(f"  已创建: {alldoi_bib_path}")
        
        # 初始化找到DOI的条目列表
        all_doi_found_entries[bib_file] = []
    
    # 导出所有无DOI条目到 no_doi_entries.bib
    if all_no_doi_entries:
        # 确保生成目录存在
        os.makedirs(GEN_BIBS_DIR, exist_ok=True)
        no_doi_bib_path = os.path.join(GEN_BIBS_DIR, NO_DOI_BIB_FILE)
        no_doi_db = bibtexparser.bibdatabase.BibDatabase()
        no_doi_db.entries = all_no_doi_entries
        with open(no_doi_bib_path, 'w', encoding='utf-8') as f:
            bibtexparser.dump(no_doi_db, f)
        logger.info(f"共 {len(all_no_doi_entries)} 条无DOI文献，已导出至 {no_doi_bib_path}")
    logger.info("=" * 60)
    
    # ==================== 步骤3：为无DOI条目查询并补全DOI ====================
    logger.info("=" * 60)
    logger.info("步骤3：为无DOI条目查询并补全DOI...")
    logger.info("=" * 60)
    
    found_doi_count = 0
    
    for bib_file, unique_path in unique_bib_paths.items():
        alldoi_bib_path = alldoi_bib_paths[bib_file]
        
        # 重新读取 _alldoi.bib 文件
        with open(alldoi_bib_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        bib_db = bibtexparser.loads(content)
        
        updated_entries = []
        file_found_doi_count = 0
        
        for entry in bib_db.entries:
            doi = entry.get('doi', entry.get('DOI', '')).strip()
            
            if not doi:
                # 没有DOI，尝试通过标题查询
                title = entry.get('title', entry.get('Title', '')).strip()
                citation_key = entry.get('ID', '')
                
                logger.info(f"  查询DOI: {citation_key} - {title[:50]}...")
                
                found_doi = find_doi_by_title(title)
                
                if found_doi:
                    file_found_doi_count += 1
                    found_doi_count += 1
                    logger.info(f"    找到DOI: {found_doi}")
                    
                    # 更新条目，添加DOI字段
                    entry['DOI'] = found_doi
                    all_doi_found_entries[bib_file].append(entry.copy())
            
            updated_entries.append(entry)
        
        # 将更新后的条目写回 _alldoi.bib 文件
        bib_db.entries = updated_entries
        with open(alldoi_bib_path, 'w', encoding='utf-8') as f:
            bibtexparser.dump(bib_db, f)
        
        if file_found_doi_count > 0:
            logger.info(f"  文件 {bib_file}: 新找到 {file_found_doi_count} 条DOI")
    
    logger.info(f"DOI补全完成，共找到 {found_doi_count} 条新DOI")
    logger.info("=" * 60)
    
    # ==================== 步骤4：处理文献（添加到Zotero并下载PDF） ====================
    logger.info("=" * 60)
    logger.info("步骤4：处理文献（添加到Zotero并下载PDF）...")
    logger.info("=" * 60)
    
    all_failed_entries = []
    all_failed_pdf_entries = []  # 新增：记录PDF下载失败的条目
    success_count = 0
    pdf_count = 0
    consecutive_failures = 0
    global_entry_index = 0
    
    # 使用 _alldoi.bib 文件进行处理
    for file_idx, (bib_file, alldoi_path) in enumerate(alldoi_bib_paths.items(), 1):
        logger.info(f"处理文件 [{file_idx}/{len(alldoi_bib_paths)}]: {alldoi_path}")
        
        # 从 _alldoi.bib 提取条目
        with open(alldoi_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        bib_db = bibtexparser.loads(content)
        
        for entry_idx, entry in enumerate(bib_db.entries, 1):
            global_entry_index += 1
            
            doi = entry.get('doi', entry.get('DOI', '')).strip()
            title = entry.get('title', entry.get('Title', '')).strip()
            year = entry.get('year', entry.get('Year', '')).strip()
            citation_key = entry.get('ID', '')
            
            if not doi:
                logger.info(f"  跳过无DOI条目 [{entry_idx}] {citation_key}")
                continue
            
            logger.info(f"  处理条目 [{entry_idx}/{len(bib_db.entries)}] 全局序号 [{global_entry_index}] DOI: {doi}")
            logger.info(f"    引用标签: {citation_key}")
            
            try:
                item_key = add_item_by_doi(zot_client, doi)
                if not item_key:
                    logger.warning(f"    添加失败")
                    all_failed_entries.append(entry)
                    consecutive_failures += 1
                    
                    if consecutive_failures >= 5:
                        logger.warning(f"    连续失败 {consecutive_failures} 次，等待 30 秒...")
                        time.sleep(30)
                        consecutive_failures = 0
                    continue
                
                consecutive_failures = 0
                logger.info(f"    成功添加，key: {item_key}")
                success_count += 1
                time.sleep(1)
                
                # 处理PDF并获取结果
                pdf_success, pdf_failed_reasons = process_pdf_for_item(
                    zot_client, item_key, doi, global_entry_index, citation_key, year, title
                )
                
                if pdf_success:
                    pdf_count += 1
                else:
                    # 记录PDF下载失败的条目
                    failed_entry = entry.copy()
                    failed_entry['pdf_failed_reasons'] = '; '.join(pdf_failed_reasons) if pdf_failed_reasons else '未知原因'
                    all_failed_pdf_entries.append(failed_entry)
                    logger.info(f"    未获取到有效 PDF: {failed_entry['pdf_failed_reasons']}")
            
            except Exception as e:
                logger.error(f"    处理失败: {e}")
                all_failed_entries.append(entry)
                consecutive_failures += 1
            
            time.sleep(DELAY_SECONDS)
        
        logger.info(f"文件 {bib_file} 处理完成\n")

    # 导出失败条目
    if all_failed_entries:
        # 确保生成目录存在
        os.makedirs(GEN_BIBS_DIR, exist_ok=True)
        failed_bib_path = os.path.join(GEN_BIBS_DIR, FAILED_BIB_FILE)
        failed_db = bibtexparser.bibdatabase.BibDatabase()
        failed_db.entries = all_failed_entries
        with open(failed_bib_path, 'w', encoding='utf-8') as f:
            bibtexparser.dump(failed_db, f)
        logger.info(f"共 {len(all_failed_entries)} 条失败文献，已导出至 {failed_bib_path}")
    
    # 导出PDF下载失败的条目
    if all_failed_pdf_entries:
        # 确保生成目录存在
        os.makedirs(GEN_BIBS_DIR, exist_ok=True)
        failed_pdf_bib_path = os.path.join(GEN_BIBS_DIR, FAILED_PDF_BIB_FILE)
        failed_pdf_db = bibtexparser.bibdatabase.BibDatabase()
        failed_pdf_db.entries = all_failed_pdf_entries
        with open(failed_pdf_bib_path, 'w', encoding='utf-8') as f:
            bibtexparser.dump(failed_pdf_db, f)
        logger.info(f"共 {len(all_failed_pdf_entries)} 条PDF下载失败的文献，已导出至 {failed_pdf_bib_path}")
    
    # 导出找到DOI的条目到新文件（文件名添加"+doi"）
    total_found_doi_entries = 0
    for bib_file, found_entries in all_doi_found_entries.items():
        if found_entries:
            total_found_doi_entries += len(found_entries)
            # 生成新文件名：原文件名+doi.bib
            base_name = os.path.splitext(bib_file)[0]
            new_bib_name = f"{base_name}+doi.bib"
            # 确保生成目录存在
            os.makedirs(GEN_BIBS_DIR, exist_ok=True)
            new_bib_path = os.path.join(GEN_BIBS_DIR, new_bib_name)
            
            # 创建新的BibDatabase并写入
            found_doi_db = bibtexparser.bibdatabase.BibDatabase()
            found_doi_db.entries = found_entries
            with open(new_bib_path, 'w', encoding='utf-8') as f:
                bibtexparser.dump(found_doi_db, f)
            logger.info(f"共 {len(found_entries)} 条新找到DOI的文献，已导出至 {new_bib_path}")
    
    if total_found_doi_entries > 0:
        logger.info(f"总计通过标题找到 {total_found_doi_entries} 条DOI，已分别导出到对应的+doi文件")
    
    logger.info("=" * 60)
    logger.info(f"处理完成！统计信息:")
    logger.info(f"  - 总条目数: {global_entry_index}")
    logger.info(f"  - 成功添加条目: {success_count}")
    logger.info(f"  - 通过标题找到DOI: {found_doi_count}")
    logger.info(f"  - 成功获取 PDF: {pdf_count}")
    logger.info(f"  - PDF下载失败: {len(all_failed_pdf_entries)}")
    logger.info(f"  - 条目添加失败: {len(all_failed_entries)}")
    logger.info(f"  - 无DOI条目: {len(all_no_doi_entries)}")
    logger.info(f"  - 发现重复文献: {total_duplicates}")
    if success_count > 0:
        logger.info(f"  - PDF 获取率: {pdf_count/success_count*100:.1f}%")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
