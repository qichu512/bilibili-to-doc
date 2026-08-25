# -*- coding: utf-8 -*-
"""
B站视频转文档 (bilibili-to-doc) — 本地 Web 应用
================================================
将 Claude Code skill "bilibili-to-doc" 封装为独立的本地程序：
  1. 用户粘贴 B 站视频链接
  2. yt-dlp（可携带 Cookies）下载 AI 中文字幕
  3. 解析 SRT 字幕
  4. 调用用户自定义的 AI 模型 API（OpenAI 兼容格式）整理成结构化 Markdown 文档
  5. 保存到桌面（或自定义目录），并在网页中预览

仅使用 Python 标准库，无第三方运行依赖（yt-dlp 除外，通过 pip 安装）。
"""

import glob as globmod
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "bilibili-to-doc"
VERSION = "1.0.2"

# PyInstaller 打包后（frozen）以 exe 所在目录为根目录
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "config.json"
COOKIES_FILE = DATA_DIR / "cookies.txt"
COOKIES_RAW_FILE = DATA_DIR / "cookies_raw.txt"
LOG_FILE = ROOT / "app.log"

DEFAULT_PORTS = list(range(8787, 8797))

# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
    try:
        LOG_FILE.unlink()
    except OSError:
        pass

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("bili2doc")

CONFIG_LOCK = threading.Lock()
JOBS = {}
JOBS_LOCK = threading.Lock()


class JobCancelled(Exception):
    pass


# --------------------------------------------------------------------------
# 桌面路径（兼容 OneDrive 重定向桌面）
# --------------------------------------------------------------------------
def get_desktop_dir() -> Path:
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(300)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    for cand in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop",
                 Path.home() / "OneDrive" / "桌面"):
        if cand.exists():
            return cand
    return Path.home()


# --------------------------------------------------------------------------
# 配置管理
# --------------------------------------------------------------------------
def default_config():
    return {
        "cookies": {"mode": "none", "browser": "chrome", "file_name": ""},
        "ai": {"base_url": "", "api_key": "", "model": "", "max_chars": 60000},
        "output_dir": str(get_desktop_dir()),
    }


def load_config():
    with CONFIG_LOCK:
        cfg = default_config()
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(data.get("cookies"), dict):
                    cfg["cookies"].update(data["cookies"])
                if isinstance(data.get("ai"), dict):
                    cfg["ai"].update(data["ai"])
                if data.get("output_dir"):
                    cfg["output_dir"] = str(data["output_dir"])
            except Exception as e:
                log.warning("读取配置失败: %s", e)
        return cfg


def save_config(cfg):
    with CONFIG_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)


# --------------------------------------------------------------------------
# Cookies 处理（yt-dlp 需要 Netscape 格式）
# --------------------------------------------------------------------------
def is_netscape_format(text: str) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return len(line.split("\t")) >= 7
    return False


def to_netscape(raw: str) -> str:
    """把 名称=值; 名称=值 的 Cookie 文本转换为 Netscape 格式。"""
    if is_netscape_format(raw):
        return raw
    text = re.sub(r"(?im)^\s*cookie:\s*", "", raw.strip())
    lines = []
    for item in text.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"')
        if not name:
            continue
        if name.lower() in ("expires", "max-age", "path", "domain", "version",
                            "httponly", "secure", "samesite", "priority"):
            continue
        lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t2147483647\t{name}\t{value}")
    if not lines:
        raise ValueError("没有解析到有效的 Cookie 键值对，请确认粘贴的是 name=value 格式")
    header = "# Netscape HTTP Cookie File\n# generated by bilibili-to-doc\n"
    return header + "\n".join(lines) + "\n"


def cookie_args(cfg) -> list:
    mode = cfg["cookies"].get("mode", "none")
    if mode == "browser":
        return ["--cookies-from-browser", cfg["cookies"].get("browser") or "chrome"]
    if mode in ("file", "text") and COOKIES_FILE.exists():
        return ["--cookies", str(COOKIES_FILE)]
    return []


# --------------------------------------------------------------------------
# Cookies 有效性检测（调用 B 站登录接口，定时 + 手动触发）
# --------------------------------------------------------------------------
COOKIE_CHECK_INTERVAL = 180  # 秒
COOKIE_STATUS = {"state": "unknown", "detail": "", "uname": "", "checked_at": 0}
COOKIE_STATUS_LOCK = threading.Lock()

_BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def cookies_to_header() -> tuple:
    """把 data/cookies.txt 解析成 Cookie 请求头。返回 (header, 错误说明)。"""
    if not COOKIES_FILE.exists():
        return None, "未找到 Cookies 文件"
    try:
        lines = COOKIES_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return None, f"读取 Cookies 文件失败：{e}"
    pairs = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5]:
            pairs.append(f"{parts[5]}={parts[6]}")
    if not pairs:
        return None, "Cookies 文件为空"
    return "; ".join(pairs), None


def check_cookies(force: bool = False) -> dict:
    """检测当前配置的 Cookies 是否有效。结果缓存 COOKIE_CHECK_INTERVAL 秒。"""
    global COOKIE_STATUS
    now = time.time()
    with COOKIE_STATUS_LOCK:
        cached = dict(COOKIE_STATUS)
    if not force and cached.get("state") not in ("unknown",) \
            and now - cached.get("checked_at", 0) < COOKIE_CHECK_INTERVAL:
        return cached

    cfg = load_config()
    mode = cfg["cookies"].get("mode", "none")
    status = {"state": "unknown", "detail": "", "uname": "", "checked_at": now}
    try:
        if mode == "none":
            status.update(state="none", detail="未配置 Cookies（游客模式）")
        elif mode == "browser":
            status.update(state="browser",
                          detail=f"使用浏览器 Cookies（{cfg['cookies'].get('browser', 'chrome')}），无法直接检测有效性")
        elif mode in ("file", "text"):
            header, err = cookies_to_header()
            if header is None:
                status.update(state="invalid", detail=err)
            else:
                req = urllib.request.Request(
                    "https://api.bilibili.com/x/web-interface/nav",
                    headers={"User-Agent": _BILI_UA, "Referer": "https://www.bilibili.com/",
                             "Accept": "application/json", "Cookie": header})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                code = data.get("code")
                d = data.get("data") or {}
                if code == 0 and d.get("isLogin"):
                    uname = str(d.get("uname") or "")
                    status.update(state="valid", uname=uname,
                                  detail=f"已登录：{uname}" if uname else "已登录")
                elif code == -101:
                    status.update(state="invalid", detail="未登录或 Cookies 已过期（SESSDATA 失效）")
                else:
                    status.update(state="invalid", detail=f"登录状态无效（code={code}）")
    except Exception as e:
        status.update(state="error", detail=f"检测失败（网络/接口问题）：{e}")
    with COOKIE_STATUS_LOCK:
        COOKIE_STATUS = status
    return dict(status)


def cookie_status_loop():
    """后台线程：定时检测 Cookies 有效性。"""
    while True:
        try:
            check_cookies(force=True)
        except Exception as e:
            log.warning("Cookie 定时检测失败: %s", e)
        time.sleep(COOKIE_CHECK_INTERVAL)


# --------------------------------------------------------------------------
# yt-dlp
# --------------------------------------------------------------------------
def yt_dlp_cmd() -> list:
    # 打包后使用安装目录下自带的 yt-dlp.exe；开发模式使用本机 Python 的 yt_dlp 模块
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve().parent / "yt-dlp.exe")]
    return [sys.executable, "-m", "yt_dlp"]


YTDLP_VERSION = None


def detect_yt_dlp():
    global YTDLP_VERSION
    try:
        proc = subprocess.run(yt_dlp_cmd() + ["--version"], capture_output=True,
                              text=True, timeout=60)
        if proc.returncode == 0:
            YTDLP_VERSION = proc.stdout.strip().splitlines()[0].strip()
    except Exception as e:
        log.warning("yt-dlp 检测失败: %s", e)


def pick_subtitle(files):
    files = [f for f in files if f.suffix.lower() == ".srt"]
    for pref in ("ai-zh", "zh-hans", "zh-hant", "zh-cn", "zh"):
        for f in files:
            if pref in f.name.lower():
                return f
    return files[0] if files else None


def list_available_subs(url: str, cfg) -> list:
    """用 --list-subs 查询视频实际可用的字幕语言（用于报错提示）。"""
    try:
        proc = subprocess.run(
            yt_dlp_cmd() + [url, "--list-subs", "--no-warnings", "--encoding", "utf-8"] + cookie_args(cfg),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(ROOT))
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        langs, in_section = [], False
        for line in out.splitlines():
            if "Available subtitles" in line:
                in_section = True
                continue
            if in_section:
                if not line.strip():
                    break
                parts = line.split()
                if not parts:
                    continue
                if parts[0].lower() in ("language", "available", "has"):
                    continue
                langs.append(parts[0])
        return langs
    except Exception:
        return []


def parse_srt(text: str) -> str:
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if re.fullmatch(r"\d{1,8}", s):
            continue
        if "-->" in s:
            continue
        s = re.sub(r"<[^>]*>", "", s)
        s = re.sub(r"\{\\[^}]*\}", "", s)
        if s:
            out.append(s)
    return "\n".join(out)


# --------------------------------------------------------------------------
# AI 调用（OpenAI 兼容 /chat/completions）
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """你是专业的视频内容整理助手。用户提供一段 B 站视频的 AI 字幕（口语化、可能碎片化、偶有识别错误），请整理成结构清晰、可直接阅读的中文 Markdown 文档。

【文档结构模板】
# {视频标题}
> 来源：B站视频 {视频ID} | 主讲：{UP主}
---
## 一、概述
（1-3 段话概述视频主题与内容）
### 1.1 背景
### 1.2 核心主题
## 二、{按字幕内容划分的第一大主题}
### 2.1 {子主题}
（讲解内容；涉及代码/配置时放入代码块）
## 三、{第二大主题}
...
## 四、实战演示（若视频包含实操演示）
### 4.1 环境准备
### 4.2 操作步骤
### 4.3 效果验证
## N、总结与建议
- 要点1
- 要点2
- 要点3
---
*本文档由 AI 根据 B 站视频 AI 字幕自动提取整理，可能存在少量错漏，建议结合原视频学习。*
*原视频链接：{URL}*

【整理要求】
1. 不要照搬字幕顺序，按逻辑主题重新划分章节；章节编号用中文（一、二、三…）。
2. 保留并忠实呈现技术细节：配置、代码、SQL、JSON、YAML、Shell 命令等放入代码块，并标注语言。
3. 参数列表、对比、优缺点等内容用 Markdown 表格呈现。
4. 关键要点用层级列表。
5. 字幕中因缺少画面而含糊之处可补充必要说明，但不要编造视频中没有的内容。
6. 删除口头禅（嗯、啊、这个）、重复语句、求三连/关注等推广内容。
7. 语言专业、通顺，修正明显的字幕识别错误（如技术名词）。
8. 直接输出 Markdown 文档本身，不要输出任何解释、前言或代码块围栏之外的文字。"""


def build_messages(title, uploader, video_id, url, transcript):
    user = (
        f"视频标题：{title}\n"
        f"UP主：{uploader}\n"
        f"视频ID：{video_id}\n"
        f"视频链接：{url}\n\n"
        f"字幕全文如下：\n\n{transcript}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def call_ai(base_url, api_key, model, messages, job=None, timeout=1500, retries=3):
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("尚未配置 AI 接口地址，请点击输入框右上角 ⚙ 设置")
    if not model:
        raise RuntimeError("尚未配置 AI 模型名称，请点击输入框右上角 ⚙ 设置")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    last_err = None
    raw = b""
    for attempt in range(1, retries + 1):
        if attempt > 1:
            time.sleep(3 * attempt)
        if job is not None and job.get("cancel"):
            raise JobCancelled()

        # --- 建立连接 ---
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            err_body = e.read(6000).decode("utf-8", "replace")
            msg = err_body
            try:
                j = json.loads(err_body)
                err = j.get("error")
                if isinstance(err, dict):
                    msg = err.get("message") or err_body
                elif err:
                    msg = str(err)
                else:
                    msg = j.get("message") or err_body
            except Exception:
                pass
            # 限流/服务端临时错误：可重试；其余（401/403 等）直接报错
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                last_err = f"HTTP {e.code}：{msg[:200]}"
                continue
            raise RuntimeError(f"AI 接口返回错误（HTTP {e.code}）：{msg[:800]}")
        except urllib.error.URLError as e:
            last_err = str(e.reason)
            if attempt < retries:
                continue
            raise RuntimeError(f"无法连接 AI 接口：{e.reason}")

        # --- 读取响应（网络可能中途断开） ---
        try:
            chunks = []
            while True:
                if job is not None and job.get("cancel"):
                    raise JobCancelled()
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            break
        except JobCancelled:
            raise
        except (http.client.IncompleteRead, http.client.RemoteDisconnected,
                ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                TimeoutError) as e:
            last_err = str(e)
            if attempt < retries:
                continue
            raise RuntimeError(
                f"AI 接口响应中断（网络连接在传输中被断开，已重试 {retries} 次仍失败：{e}）。"
                "请检查网络后重试；若反复出现，可在设置中换一个 API 地址。")
        finally:
            try:
                resp.close()
            except Exception:
                pass

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"AI 接口返回内容无法解析：{e}")
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError("AI 接口响应格式异常：" + json.dumps(data, ensure_ascii=False)[:500])
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


def strip_fences(md: str) -> str:
    md = md.strip()
    md = re.sub(r"^\s*```[a-zA-Z0-9_-]*\s*\n?", "", md, count=1)
    if md.endswith("```"):
        md = md[:-3].rstrip()
    return md.strip() + "\n"


# --------------------------------------------------------------------------
# 转换任务
# --------------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return (name[:120] or "bilibili_video")


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 1000):
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
    return p.with_name(f"{p.stem}_{int(time.time())}{p.suffix}")


def ytdlp_error_message(err: str) -> str:
    tail = err.strip()[-1500:] if err.strip() else "（无输出）"
    msg = "yt-dlp 处理失败：\n" + tail
    low = tail.lower()
    if "412" in tail or "precondition failed" in low:
        msg += ("\n\n提示：HTTP 412 是 B 站的风控拦截，与 Cookies 是否有效无关，"
                "常见于短时间多次访问。请：\n1. 直接重试一次（程序会自动重试）；\n"
                "2. 稍等几分钟再试；\n3. 若每次都 412，可尝试更新 Cookies 或切换网络。")
    return msg


def run_job(job):
    cfg = load_config()
    url = job["url"]
    tmp = DATA_DIR / "tmp" / job["id"]
    try:
        job["status"] = "running"
        job["stage"] = "检查运行环境"
        if not YTDLP_VERSION:
            raise RuntimeError("未检测到 yt-dlp，请先在命令行运行：pip install yt-dlp")
        if job.get("cancel"):
            raise JobCancelled()

        job["stage"] = "获取视频信息"
        tmp.mkdir(parents=True, exist_ok=True)

        # 注意：--print 与 --write-subs 组合在新版 yt-dlp 中存在 bug，会导致字幕
        # 不写出，因此元信息与字幕必须分两次调用。
        title, uploader, video_id = "未知标题", "", ""
        try:
            meta_cmd = (yt_dlp_cmd() + [
                url, "--skip-download", "--no-warnings", "--no-progress",
                "--encoding", "utf-8",
                "--print", "%(title)s\t%(uploader)s\t%(id)s",
            ] + cookie_args(cfg))
            mproc = subprocess.run(meta_cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=120, cwd=str(ROOT))
            for line in mproc.stdout.splitlines():
                line = line.strip()
                if line.count("\t") >= 2 and "WARNING" not in line:
                    parts = line.rsplit("\t", 2)
                    if len(parts) == 3:
                        title, uploader, video_id = parts
                    break
        except Exception:
            log.warning("获取视频元信息失败，将使用默认标题", exc_info=True)

        job["stage"] = "下载 AI 字幕"
        cmd = (yt_dlp_cmd() + [
            url,
            "--skip-download", "--write-subs",
            "--sub-langs", "ai-zh,zh-Hans,zh-Hant,zh-CN,zh",
            "--no-progress", "--no-warnings",
            "--encoding", "utf-8",
            "--retries", "3",
            "--socket-timeout", "30",
            "-o", str(tmp / "video"),
        ] + cookie_args(cfg))
        log.info("job %s: %s", job["id"], " ".join(cmd[:2] + ["<url>"] + cmd[3:8]))

        # B 站字幕接口可能被风控间歇性拦截（412），yt-dlp 会静默跳过字幕，
        # 因此当没有拿到字幕文件时自动重试几次。
        proc = None
        for attempt in range(1, 4):
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=600, cwd=str(ROOT))
            if job.get("cancel"):
                raise JobCancelled()
            srt_file = pick_subtitle(sorted(tmp.glob("video*.srt")))
            if proc.returncode == 0 and srt_file is not None:
                break
            if attempt < 3:
                wait = 10 * attempt
                job["stage"] = f"字幕下载未成功（可能是风控拦截），{wait} 秒后自动重试（{attempt + 1}/3）"
                time.sleep(wait)

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(ytdlp_error_message(err))

        srt_file = pick_subtitle(sorted(tmp.glob("video*.srt")))
        if srt_file is None:
            langs = list_available_subs(url, cfg)
            if langs is None:
                hint = ("B 站 AI 字幕通常需要登录后才能获取：请点击输入框右上角 ⚙ 设置，"
                        "确认已导入有效的 B 站 Cookies；若视频本身没有字幕，则无法提取。")
            elif not langs:
                hint = "经 yt-dlp 确认，该视频没有任何字幕，无法提取。"
            else:
                hint = ("该视频可用的字幕语言：" + "、".join(langs)
                        + "。当前仅支持提取中文字幕（ai-zh / zh-Hans 等）。")
            raise RuntimeError("未能获取到字幕。\n" + hint)
        transcript = parse_srt(srt_file.read_text(encoding="utf-8", errors="replace"))
        if not transcript.strip():
            raise RuntimeError("字幕内容为空，无法生成文档。")
        job["title"] = title

        if job.get("cancel"):
            raise JobCancelled()
        max_chars = int(cfg["ai"].get("max_chars") or 60000)
        truncated = len(transcript) > max_chars
        if truncated:
            transcript = transcript[:max_chars] + "\n（……因长度限制，后续字幕已截断）"

        job["stage"] = "AI 正在整理生成文档（较长视频可能需要几分钟）"
        messages = build_messages(title, uploader, video_id, url, transcript)
        md = call_ai(cfg["ai"]["base_url"], cfg["ai"]["api_key"], cfg["ai"]["model"],
                     messages, job=job)
        if job.get("cancel"):
            raise JobCancelled()
        md = strip_fences(md)
        if not md.strip():
            raise RuntimeError("AI 返回内容为空，请检查模型配置")

        job["stage"] = "保存文档到本地"
        out_dir = Path(cfg["output_dir"] or get_desktop_dir())
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = unique_path(out_dir / (sanitize_filename(title) + ".md"))
        file_path.write_text(md, encoding="utf-8")

        job["result"] = {
            "title": title,
            "uploader": uploader,
            "video_id": video_id,
            "url": url,
            "file_path": str(file_path),
            "file_name": file_path.name,
            "markdown": md,
            "truncated": truncated,
        }
        job["stage"] = "完成"
        job["status"] = "done"
        log.info("job %s done -> %s", job["id"], file_path)
    except JobCancelled:
        job["status"] = "cancelled"
        job["stage"] = "已取消"
    except Exception as e:
        job["status"] = "error"
        job["stage"] = "失败"
        job["error"] = str(e)
        log.error("job %s 失败: %s\n%s", job["id"], e, traceback.format_exc())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def is_bili_url(url: str) -> bool:
    u = url.strip()
    return bool(re.search(r"bilibili\.com", u, re.I)) or bool(re.search(r"b23\.tv", u, re.I)) \
        or bool(re.search(r"BV[0-9A-Za-z]{8,}", u))


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{VERSION}"

    # ---- helpers ----
    def log_message(self, fmt, *args):  # 降低控制台噪音
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 10 * 1024 * 1024:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/health":
                self.send_json({"ok": True, "app": APP_NAME, "version": VERSION,
                                "yt_dlp": YTDLP_VERSION})
                return
            if path == "/api/config":
                self.send_json(public_config())
                return
            if path == "/api/cookie-status":
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                force = qs.get("force", ["0"])[0] in ("1", "true")
                if force:
                    # 手动检测：后台执行，立即返回当前（旧）结果，前端稍后轮询即可
                    threading.Thread(target=check_cookies, kwargs={"force": True},
                                     daemon=True).start()
                self.send_json(check_cookies(force=False))
                return
            if path.startswith("/api/jobs/"):
                rest = path[len("/api/jobs/"):]
                if rest.endswith("/download"):
                    self.handle_download(rest[:-len("/download")])
                else:
                    self.handle_job_status(rest)
                return
            self.serve_static(path)
        except Exception as e:
            log.error("GET %s 错误: %s", self.path, traceback.format_exc())
            try:
                self.send_json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/config":
                self.handle_save_config()
                return
            if path == "/api/test-ai":
                self.handle_test_ai()
                return
            if path == "/api/convert":
                self.handle_convert()
                return
            if path == "/api/quit":
                self.handle_quit()
                return
            if path.startswith("/api/jobs/"):
                rest = path[len("/api/jobs/"):]
                if rest.endswith("/cancel"):
                    self.handle_cancel(rest[:-len("/cancel")])
                    return
                if rest.endswith("/open-folder"):
                    self.handle_open_folder(rest[:-len("/open-folder")])
                    return
            self.send_json({"ok": False, "error": "未知接口"}, 404)
        except Exception as e:
            log.error("POST %s 错误: %s", self.path, traceback.format_exc())
            try:
                self.send_json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

    # ---- API 实现 ----
    def handle_job_status(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            self.send_json({"ok": False, "error": "任务不存在"}, 404)
            return
        self.send_json({
            "id": job["id"],
            "status": job["status"],
            "stage": job["stage"],
            "error": job.get("error"),
            "result": job.get("result"),
        })

    def handle_download(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        result = job.get("result") if job else None
        fp = Path(result["file_path"]) if result else None
        if fp is None or not fp.is_file():
            self.send_json({"ok": False, "error": "文件不存在"}, 404)
            return
        data = fp.read_bytes()
        quoted = urllib.parse.quote(fp.name)
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quoted}; "
                         f'filename="{fp.name.replace(chr(34), "")}"')
        self.end_headers()
        self.wfile.write(data)

    def handle_cancel(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            self.send_json({"ok": False, "error": "任务不存在"}, 404)
            return
        job["cancel"] = True
        self.send_json({"ok": True})

    def handle_open_folder(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        result = job.get("result") if job else None
        fp = Path(result["file_path"]) if result else None
        if fp is None or not fp.is_file():
            self.send_json({"ok": False, "error": "文件不存在"}, 404)
            return
        try:
            subprocess.Popen(["explorer", "/select,", str(fp)])
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, 500)
            return
        self.send_json({"ok": True})

    def handle_quit(self):
        """网页上的「停止程序」按钮：停止服务并退出整个进程。"""
        self.send_json({"ok": True, "message": "程序即将退出"})

        def _stop():
            time.sleep(0.6)
            log.info("收到网页停止请求，正在停止服务…")
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            time.sleep(0.5)
            os._exit(0)

        threading.Thread(target=_stop, daemon=True).start()

    def handle_convert(self):
        body = self.read_json()
        url = (body.get("url") or "").strip()
        if not url:
            self.send_json({"ok": False, "error": "请输入 B 站视频链接"}, 400)
            return
        if not is_bili_url(url):
            self.send_json({"ok": False,
                            "error": "请输入有效的 B 站视频链接（如 https://www.bilibili.com/video/BV... 或 b23.tv 短链接）"},
                           400)
            return
        job = {
            "id": uuid.uuid4().hex[:12],
            "url": url,
            "status": "queued",
            "stage": "排队中",
            "created": time.time(),
            "cancel": False,
            "result": None,
            "error": None,
            "title": "",
        }
        with JOBS_LOCK:
            JOBS[job["id"]] = job
        threading.Thread(target=run_job, args=(job,), daemon=True).start()
        self.send_json({"ok": True, "job_id": job["id"]})

    def handle_test_ai(self):
        body = self.read_json()
        cfg = load_config()
        base = (body.get("base_url") or "").strip() or cfg["ai"]["base_url"]
        model = (body.get("model") or "").strip() or cfg["ai"]["model"]
        key = body.get("api_key")
        if key in (None, ""):
            key = cfg["ai"]["api_key"]
        try:
            reply = call_ai(base, key, model,
                            [{"role": "user", "content": "你好，请只回复两个字：OK"}],
                            timeout=60)
            self.send_json({"ok": True, "reply": reply.strip()[:120]})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def handle_save_config(self):
        body = self.read_json()
        cfg = load_config()

        c = body.get("cookies") or {}
        mode = c.get("mode") or cfg["cookies"].get("mode") or "none"
        cfg["cookies"]["mode"] = mode
        cfg["cookies"]["browser"] = c.get("browser") or cfg["cookies"].get("browser") or "chrome"
        if c.get("file_name"):
            cfg["cookies"]["file_name"] = str(c["file_name"])
        if mode in ("file", "text"):
            raw = c.get("cookie_raw")
            if raw is not None and str(raw).strip():
                try:
                    netscape = to_netscape(str(raw))
                except ValueError as e:
                    self.send_json({"ok": False, "error": str(e)}, 400)
                    return
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                COOKIES_FILE.write_text(netscape, encoding="utf-8")
                COOKIES_RAW_FILE.write_text(str(raw).strip(), encoding="utf-8")
            elif not COOKIES_FILE.exists():
                self.send_json({"ok": False,
                                "error": "该模式下需要先导入 Cookies（选择 cookies.txt 文件或粘贴 Cookie 文本）"},
                               400)
                return

        a = body.get("ai") or {}
        if "base_url" in a:
            cfg["ai"]["base_url"] = str(a["base_url"] or "").strip().rstrip("/")
        if "model" in a:
            cfg["ai"]["model"] = str(a["model"] or "").strip()
        if "max_chars" in a:
            try:
                mc = int(a["max_chars"])
                cfg["ai"]["max_chars"] = max(1000, min(500000, mc))
            except (TypeError, ValueError):
                pass
        if "api_key" in a:
            k = a["api_key"]
            if k == "__clear__":
                cfg["ai"]["api_key"] = ""
            elif k not in (None, ""):
                cfg["ai"]["api_key"] = str(k).strip()

        if body.get("output_dir"):
            cfg["output_dir"] = str(body["output_dir"]).strip()

        save_config(cfg)
        # 配置变更后立即重新检测 Cookies 有效性
        threading.Thread(target=check_cookies, kwargs={"force": True}, daemon=True).start()
        self.send_json({"ok": True})

    # ---- 静态文件 ----
    def serve_static(self, path):
        if path in ("/", ""):
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        fp = (WEB_DIR / rel).resolve()
        try:
            fp.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.send_json({"ok": False, "error": "禁止访问"}, 403)
            return
        if not fp.is_file():
            self.send_json({"ok": False, "error": "页面不存在"}, 404)
            return
        data = fp.read_bytes()
        ctype = "text/html; charset=utf-8"
        if fp.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif fp.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif fp.suffix == ".svg":
            ctype = "image/svg+xml"
        elif fp.suffix == ".png":
            ctype = "image/png"
        elif fp.suffix == ".ico":
            ctype = "image/x-icon"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def public_config():
    cfg = load_config()
    key = cfg["ai"].get("api_key") or ""
    raw = ""
    if COOKIES_RAW_FILE.exists():
        try:
            raw = COOKIES_RAW_FILE.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
    return {
        "cookies": {
            "mode": cfg["cookies"].get("mode", "none"),
            "browser": cfg["cookies"].get("browser", "chrome"),
            "file_name": cfg["cookies"].get("file_name", ""),
            "cookie_raw": raw,
        },
        "ai": {
            "base_url": cfg["ai"].get("base_url", ""),
            "model": cfg["ai"].get("model", ""),
            "max_chars": cfg["ai"].get("max_chars", 60000),
            "has_key": bool(key),
            "key_hint": ("••••" + key[-4:]) if len(key) >= 4 else ("已保存" if key else ""),
        },
        "output_dir": cfg.get("output_dir", ""),
        "desktop_dir": str(get_desktop_dir()),
        "yt_dlp_version": YTDLP_VERSION,
        "version": VERSION,
    }


# --------------------------------------------------------------------------
# 启动：端口探测、多实例检测、自动打开浏览器
# --------------------------------------------------------------------------
def probe_running_instance(ports):
    for port in ports:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
            with urllib.request.urlopen(req, timeout=0.6) as r:
                data = json.loads(r.read(2048).decode("utf-8", "replace"))
                if data.get("app") == APP_NAME:
                    return port
        except Exception:
            continue
    return None


CONSOLE_TITLE = "B站视频转文档 · 本地服务（关闭此窗口即停止程序）"


def set_console_title(text: str):
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def install_console_handler(server):
    """关闭命令行窗口（或按 Ctrl+C）时干净地停止服务并退出进程。"""
    try:
        import ctypes

        def on_event(ctrl_type):
            # 0 = CTRL_C_EVENT, 2 = CTRL_CLOSE_EVENT
            if ctrl_type in (0, 2):
                log.info("收到关闭信号(type=%s)，正在停止服务…", ctrl_type)
                try:
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    time.sleep(0.8)
                except Exception:
                    pass
                os._exit(0)
            return 0

        handler = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong)(on_event)
        if ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
            log.info("已安装控制台关闭监听：关闭本窗口将自动停止服务")
        else:
            log.warning("控制台关闭监听安装失败（关闭窗口仍会强制结束进程）")
        return handler  # 保持引用防止被回收
    except Exception:
        return None


def notify_running_and_exit(port: int):
    url = f"http://127.0.0.1:{port}/"
    print("程序已在运行，已为你打开网页。")
    print(f"服务地址：{url}")
    print()
    print("注意：本窗口只是「提示」，关闭它不会影响正在运行的程序。")
    print("要停止程序，请关闭那个标题为「B站视频转文档 · 本地服务」的命令行窗口，")
    print("或点击网页底部的「停止程序」按钮。")
    print()
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        input("按回车键关闭本提示窗口…")
    except Exception:
        time.sleep(5)


def main():
    set_console_title(CONSOLE_TITLE)
    no_open = "--no-open" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--no-open"]
    ports = [int(args[0])] if args and args[0].isdigit() else list(DEFAULT_PORTS)

    # 清理历史任务残留的临时目录
    shutil.rmtree(DATA_DIR / "tmp", ignore_errors=True)

    running = probe_running_instance(ports)
    if running is not None:
        notify_running_and_exit(running)
        return

    server = None
    for port in ports:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            server.daemon_threads = True
            break
        except OSError:
            continue
    if server is None:
        # 端口全被占用：可能是另一实例刚好在启动，再探测一次
        running = probe_running_instance(ports)
        if running is not None:
            notify_running_and_exit(running)
            return
        log.error("没有可用端口: %s", ports)
        print("启动失败：端口被占用且无法连接现有服务，请稍后重试。")
        time.sleep(5)
        return

    install_console_handler(server)
    url = f"http://127.0.0.1:{port}/"
    log.info("服务已启动: %s (PID %s)", url, os.getpid())
    print(f"服务已启动：{url}")
    print("正在打开浏览器…（关闭本窗口或按 Ctrl+C 即可停止程序）")
    # 启动 Cookies 有效性定时检测
    threading.Thread(target=cookie_status_loop, daemon=True).start()
    threading.Thread(target=check_cookies, kwargs={"force": True}, daemon=True).start()
    if not no_open:
        try:
            webbrowser.open(url)
        except Exception as e:
            log.warning("打开浏览器失败: %s", e)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("服务已停止")


if __name__ == "__main__":
    detect_yt_dlp()
    main()
