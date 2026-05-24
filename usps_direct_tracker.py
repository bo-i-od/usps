"""
USPS 直接追踪器 - 使用 Selenium 直接访问 USPS 网站
支持两种浏览器模式:
  - local: 本地 Chrome 无头模式（默认），自动下载 ChromeDriver
  - bit:   通过 Bit 浏览器 API 启动窗口，连接 Selenium 操控
"""
import argparse
import glob
import io
import json
import os
import re
import shutil
import socket
import subprocess
import time
import zipfile
from typing import List, Optional

import requests
from selectolax.lexbor import LexborHTMLParser


BATCH_SIZE = 35
MAX_RETRY = 3
BATCH_TIMEOUT = 30

BROWSER_MODE_LOCAL = "local"
BROWSER_MODE_BIT = "bit"

# ─── Bit 浏览器 API ─────────────────────────────────────────────

BIT_API_URL = "http://127.0.0.1:54345"
BIT_HEADERS = {"Content-Type": "application/json"}


def _bit_open_browser(browser_id: str, headless: bool = True) -> dict:
    """打开 Bit 浏览器窗口，返回含 driver path 和 debugger address 的 response"""
    payload = {"id": browser_id}
    if headless:
        payload["args"] = ["--headless=new"]
        payload["loadExtensions"] = False
    res = requests.post(
        f"{BIT_API_URL}/browser/open",
        data=json.dumps(payload),
        headers=BIT_HEADERS,
        timeout=30,
    ).json()
    if res.get("success") is False:
        raise RuntimeError(f"Bit browser open failed: {res.get('msg', res)}")
    return res


def _bit_close_browser(browser_id: str):
    """关闭 Bit 浏览器窗口"""
    try:
        requests.post(
            f"{BIT_API_URL}/browser/close",
            data=json.dumps({"id": browser_id}),
            headers=BIT_HEADERS,
            timeout=10,
        )
    except Exception as e:
        print(f"    -> Warning: failed to close Bit browser: {e}")


def cleanup_and_close_bit(driver, browser_id: str):
    """关闭所有页签后再关闭 Bit 浏览器窗口，确保无残留"""
    try:
        handles = driver.window_handles
        if handles:
            for h in handles[1:]:
                driver.switch_to.window(h)
                driver.close()
            driver.switch_to.window(handles[0])
            driver.get("about:blank")
    except Exception as e:
        print(f"    -> Tab cleanup before close warning: {e}")
    _bit_close_browser(browser_id)


BULK_TRACKING_URL = (
    "https://tools.usps.com/go/TrackConfirmAction"
    "?tRef=fullpage&tLc={count}&text28777=&tLabels={labels}&tABt=false"
)


# ─── 代理检测 ──────────────────────────────────────────────────

PROXY_PORTS = [
    ("http", 7890),   # Clash
    ("http", 7897),   # Clash Verge
    ("http", 10809),  # V2RayN HTTP
    ("socks5", 7891), # Clash SOCKS5
    ("socks5", 10808),# V2RayN SOCKS5
    ("http", 1080),
    ("http", 8118),   # Privoxy
    ("socks5", 1081),
    ("http", 2080),
    ("http", 9050),   # Tor
]


def _detect_proxy():
    """自动检测本地代理"""
    for env_var in ["HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"]:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val
    for proto, port in PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return f"{proto}://127.0.0.1:{port}"
        except (socket.timeout, ConnectionRefusedError, OSError):
            continue
    return None


def _make_proxies(proxy_url):
    if not proxy_url:
        return None
    if proxy_url.startswith("socks"):
        try:
            import socks as _  # noqa
        except ImportError:
            return None
    return {"http": proxy_url, "https": proxy_url}


# ─── Chrome 版本检测 ───────────────────────────────────────────

_chrome_version_cache = None


def _get_chrome_version():
    """返回 (major, full) 如 (148, "148.0.7778.168")

    不使用 chrome.exe --version, 因为在 Windows 上它会弹出浏览器窗口。
    改用注册表 + 文件属性来获取版本号。
    """
    global _chrome_version_cache
    if _chrome_version_cache is not None:
        return _chrome_version_cache

    for reg_key in [
        r"HKLM\SOFTWARE\Google\Chrome\BLBeacon",
        r"HKLM\SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon",
        r"HKCU\SOFTWARE\Google\Chrome\BLBeacon",
    ]:
        try:
            out = subprocess.check_output(
                ["reg", "query", reg_key, "/v", "version"],
                text=True, timeout=5,
                stderr=subprocess.DEVNULL,
            )
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
            if m:
                full = m.group(1)
                _chrome_version_cache = (int(full.split(".")[0]), full)
                return _chrome_version_cache
        except Exception:
            pass

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                import ctypes
                size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
                if size:
                    buf = ctypes.create_string_buffer(size)
                    ctypes.windll.version.GetFileVersionInfoW(path, None, size, buf)
                    buf_ptr = ctypes.c_void_p()
                    res_len = ctypes.c_uint()
                    if ctypes.windll.version.VerQueryValueW(
                        buf, r"\VarFileInfo\Translation",
                        ctypes.byref(buf_ptr), ctypes.byref(res_len)
                    ):
                        lang_codepage = ctypes.cast(buf_ptr, ctypes.POINTER(ctypes.c_uint32))[0]
                        lang_id = lang_codepage & 0xFFFF
                        codepage = (lang_codepage >> 16) & 0xFFFF
                        sub_key = f"\\StringFileInfo\\{lang_id:04X}{codepage:04X}\\ProductVersion"
                        if ctypes.windll.version.VerQueryValueW(
                            buf, sub_key,
                            ctypes.byref(buf_ptr), ctypes.byref(res_len)
                        ):
                            ver_str = ctypes.cast(buf_ptr, ctypes.c_wchar_p).value
                            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", ver_str or "")
                            if m:
                                full = m.group(1)
                                _chrome_version_cache = (int(full.split(".")[0]), full)
                                return _chrome_version_cache
            except Exception:
                pass

    _chrome_version_cache = (None, None)
    return _chrome_version_cache


# ─── ChromeDriver 缓存操作 ─────────────────────────────────────

def _find_cached_chromedriver(major_version):
    """在缓存中递归查找版本匹配的 chromedriver.exe"""
    cache_roots = [
        os.path.join(os.path.expanduser("~"), ".cache", "selenium", "chromedriver"),
        os.path.join(os.path.expanduser("~"), ".wdm", "drivers", "chromedriver"),
    ]
    for base in cache_roots:
        if not os.path.isdir(base):
            continue
        for f in glob.glob(os.path.join(base, "**", "chromedriver.exe"), recursive=True):
            rel = os.path.relpath(os.path.dirname(f), base)
            m = re.search(r"(\d+)\.", rel)
            if m and int(m.group(1)) == major_version:
                return f
    return None


def _nuke_all_chromedriver_cache():
    """彻底清空所有 chromedriver 缓存目录，防止 SeleniumManager 回退到旧版本"""
    cache_dirs = [
        os.path.join(os.path.expanduser("~"), ".cache", "selenium", "chromedriver"),
        os.path.join(os.path.expanduser("~"), ".wdm", "drivers", "chromedriver"),
    ]
    for d in cache_dirs:
        if os.path.isdir(d):
            try:
                shutil.rmtree(d)
                print(f"    -> Nuked cache: {d}")
            except Exception as e:
                print(f"    -> Could not nuke {d}: {e}")


# ─── ChromeDriver 下载 ─────────────────────────────────────────

def _try_download(full_version, proxies=None):
    """尝试从多个源下载 chromedriver, 返回 exe 路径或 None"""
    for platform in ["win64", "win32"]:
        url = (
            f"https://storage.googleapis.com/chrome-for-testing-public/"
            f"{full_version}/{platform}/chromedriver-{platform}.zip"
        )
        try:
            print(f"    -> Trying storage.googleapis.com/{full_version}/{platform}...")
            resp = requests.get(url, timeout=60, stream=True, proxies=proxies)
            if resp.status_code == 200:
                return _save_chromedriver_zip(resp.content, full_version)
            print(f"    -> HTTP {resp.status_code}")
        except Exception as e:
            print(f"    -> Failed: {e}")

    for platform in ["win64", "win32"]:
        url = (
            f"https://edgedl.me.gvt1.com/edgedl/chrome-for-testing/"
            f"{full_version}/{platform}/chromedriver-{platform}.zip"
        )
        try:
            print(f"    -> Trying edgedl.me.gvt1.com/{full_version}/{platform}...")
            resp = requests.get(url, timeout=60, stream=True, proxies=proxies)
            if resp.status_code == 200:
                return _save_chromedriver_zip(resp.content, full_version)
            print(f"    -> HTTP {resp.status_code}")
        except Exception as e:
            print(f"    -> Failed: {e}")

    try:
        print("    -> Trying chrome-for-testing index (needs proxy)...")
        resp = requests.get(
            "https://googlechromelabs.github.io/chrome-for-testing/"
            "known-good-versions-with-downloads.json",
            timeout=20, proxies=proxies,
        )
        if resp.status_code == 200:
            versions = resp.json().get("versions", [])
            for v in reversed(versions):
                if v["version"].startswith(f"{full_version.rsplit('.', 1)[0]}."):
                    downloads = v.get("downloads", {}).get("chromedriver", [])
                    for d in downloads:
                        if d.get("platform") in ("win64", "win32"):
                            dl_url = d["url"]
                            dl_ver = v["version"]
                            print(f"    -> Found v{dl_ver}, downloading...")
                            resp2 = requests.get(dl_url, timeout=120, stream=True, proxies=proxies)
                            if resp2.status_code == 200:
                                return _save_chromedriver_zip(resp2.content, dl_ver)
                    break
    except Exception as e:
        print(f"    -> CFT index failed: {e}")

    return None


def _save_chromedriver_zip(content, version_str):
    """将 zip 内容解压到缓存目录, 返回 chromedriver.exe 路径"""
    target_dir = os.path.join(
        os.path.expanduser("~"), ".cache", "selenium",
        "chromedriver", version_str
    )
    os.makedirs(target_dir, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(content)).extractall(target_dir)

    for f in glob.glob(os.path.join(target_dir, "**", "chromedriver.exe"), recursive=True):
        print(f"    -> Saved chromedriver v{version_str}: {f}")
        return f
    return None


# ─── ChromeDriver 主解析 ───────────────────────────────────────

def resolve_chromedriver():
    """按优先级查找或下载版本匹配的 chromedriver"""
    manual = os.environ.get("CHROMEDRIVER_PATH")
    if manual and os.path.isfile(manual):
        print(f"    -> Using CHROMEDRIVER_PATH: {manual}")
        return manual

    major, full = _get_chrome_version()
    if major:
        print(f"    -> Detected Chrome version: {full}")
    else:
        print("    -> Could not detect Chrome version")

    if major:
        cached = _find_cached_chromedriver(major)
        if cached:
            print(f"    -> Found matching cached chromedriver: {cached}")
            return cached

    if major:
        sys_cd = shutil.which("chromedriver")
        if sys_cd and os.path.isfile(sys_cd):
            try:
                out = subprocess.check_output([sys_cd, "--version"], text=True, timeout=5)
                m = re.search(r"(\d+)\.", out)
                if m and int(m.group(1)) == major:
                    print(f"    -> Found system chromedriver: {sys_cd}")
                    return sys_cd
            except Exception:
                pass

    if major:
        _nuke_all_chromedriver_cache()

    proxy_url = _detect_proxy()
    proxies = _make_proxies(proxy_url) if proxy_url else None

    if proxy_url:
        print(f"    -> Using proxy: {proxy_url}")
    else:
        print("    -> No proxy detected, trying direct connections...")

    if major and major >= 115 and full:
        exe = _try_download(full, proxies)
        if exe:
            return exe

    if major and major < 115:
        print(f"    -> Trying npmmirror (Chrome {major})...")
        mirror = "https://cdn.npmmirror.com/binaries/chromedriver"
        try:
            resp = requests.get(f"{mirror}/LATEST_RELEASE_{major}", timeout=15)
            if resp.status_code != 200:
                resp = requests.get(f"{mirror}/LATEST_RELEASE", timeout=15)
            if resp.status_code == 200:
                ver = resp.text.strip()
                if ver.startswith(f"{major}."):
                    resp2 = requests.get(f"{mirror}/{ver}/chromedriver_win32.zip", timeout=60, stream=True)
                    if resp2.status_code == 200:
                        exe = _save_chromedriver_zip(resp2.content, ver)
                        if exe:
                            return exe
        except Exception as e:
            print(f"    -> npmmirror failed: {e}")

    print("    -> Falling back to SeleniumManager (cache cleared, will attempt fresh download)...")
    return "__SELENIUM_MANAGER__"


# ─── 浏览器初始化 ──────────────────────────────────────────────

def get_webdriver():
    """初始化并返回无头浏览器实例"""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService

    print("    -> Setting up Chrome...")
    init_start = time.time()

    options = ChromeOptions()
    major_ver, _ = _get_chrome_version()
    options.add_argument("--headless=new")
    print(f"    -> Using --headless=new (Chrome {major_ver or 'unknown'})")

    _headless_profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chrome-headless-profile")
    for _lock in ["lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"]:
        _lp = os.path.join(_headless_profile, _lock)
        if os.path.isfile(_lp):
            try:
                os.remove(_lp)
            except Exception:
                pass
    options.add_argument(f"--user-data-dir={_headless_profile}")
    options.add_argument("--disable-gpu")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.page_load_strategy = "eager"
    options.add_argument("--log-level=3")
    options.add_argument("--silent")

    chromedriver_path = resolve_chromedriver()
    driver = None

    os.environ.setdefault("CHROMIUM_LOG_LEVEL", "3")

    CREATE_NO_WINDOW = 0x08000000

    if chromedriver_path == "__SELENIUM_MANAGER__":
        try:
            print("    -> Launching via SeleniumManager...")
            service = ChromeService()
            service.creationflags = CREATE_NO_WINDOW
            service.log_output = subprocess.DEVNULL
            driver = webdriver.Chrome(service=service, options=options)
        except Exception as e:
            print(f"    -> SeleniumManager failed: {e}")
            raise RuntimeError(
                "\n    Could not initialize Chrome WebDriver!\n"
                "    All download methods failed (likely network issue in China).\n"
                "    Please try one of:\n"
                "      1. Start your proxy (Clash/V2Ray) and retry\n"
                "      2. Set env: set HTTPS_PROXY=http://127.0.0.1:7890\n"
                "      3. Set CHROMEDRIVER_PATH=C:\\path\\to\\chromedriver.exe\n"
                "      4. Manually download chromedriver from:\n"
                "         https://googlechromelabs.github.io/chrome-for-testing/\n"
            )
    else:
        service = ChromeService(executable_path=chromedriver_path)
        service.creationflags = CREATE_NO_WINDOW
        service.log_output = subprocess.DEVNULL
        driver = webdriver.Chrome(service=service, options=options)

    init_elapsed = time.time() - init_start
    print(f"    -> Chrome launched successfully in {init_elapsed:.2f}s!")

    print("    -> Applying anti-detection scripts...")
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    driver.set_page_load_timeout(60)
    driver.implicitly_wait(10)
    return driver


def get_webdriver_bit(browser_id: str):
    """通过 Bit 浏览器 API 打开窗口，返回 Selenium WebDriver 实例"""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService

    print(f"    -> Opening Bit browser (id={browser_id})...")
    init_start = time.time()

    res = _bit_open_browser(browser_id)
    driver_path = res["data"]["driver"]
    debugger_address = res["data"]["http"]
    print(f"    -> Bit browser opened, debugger at {debugger_address}")

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debugger_address)

    chrome_service = ChromeService(driver_path)
    CREATE_NO_WINDOW = 0x08000000
    chrome_service.creationflags = CREATE_NO_WINDOW

    driver = webdriver.Chrome(service=chrome_service, options=chrome_options)
    driver.set_page_load_timeout(60)
    driver.implicitly_wait(10)

    _cleanup_bit_tabs(driver)

    init_elapsed = time.time() - init_start
    print(f"    -> Bit browser connected in {init_elapsed:.2f}s!")
    return driver


def _cleanup_bit_tabs(driver):
    """关闭 Bit 浏览器中残留的多余页签，只保留一个干净的空白页"""
    try:
        handles = driver.window_handles
        if len(handles) > 1:
            print(f"    -> Cleaning up {len(handles) - 1} leftover tab(s)...")
            main_handle = handles[0]
            for h in handles[1:]:
                driver.switch_to.window(h)
                driver.close()
            driver.switch_to.window(main_handle)
        driver.get("about:blank")
    except Exception as e:
        print(f"    -> Tab cleanup warning: {e}")


def _parse_container(container) -> dict:
    """解析单个 .track-bar-container 中的物流信息"""
    tn_el = container.css_first("span.tracking-number")
    if not tn_el:
        return None
    tracking_number = tn_el.attributes.get("value", "") or tn_el.text(strip=True)

    banner_el = container.css_first(".banner-content")
    status = banner_el.text(strip=True) if banner_el else "Unknown"

    events = []
    for step in container.css(".tb-step:not(.toggle-history-container)"):
        date_el = step.css_first(".tb-date")
        location_el = step.css_first(".tb-location")
        detail_el = step.css_first(".tb-status-detail")

        date_str = ""
        if date_el:
            lines = [l.strip() for l in date_el.text().splitlines() if l.strip()]
            date_str = " ".join(lines[:2])

        events.append({
            "date": date_str,
            "location": location_el.text(strip=True) if location_el else "",
            "description": detail_el.text(strip=True) if detail_el else "",
        })

    est_delivery = ""
    date_el = container.css_first(".date")
    month_year_el = container.css_first(".month_year")
    if date_el and month_year_el:
        try:
            day = date_el.text().zfill(2)
            parts = month_year_el.text().splitlines()[0].strip().split(" ")
            if len(parts) >= 2:
                est_delivery = f"{parts[0]} {day}, {parts[1]}"
        except Exception:
            pass

    return {
        "tracking_number": tracking_number,
        "error": None,
        "data": {
            "status": status,
            "estimated_delivery": est_delivery,
            "events": events,
        },
    }


def _parse_bulk_html(html: str, expected_numbers: List[str]) -> List[dict]:
    """从包含多个单号的 USPS 页面中解析所有物流信息"""
    tree = LexborHTMLParser(html)
    containers = tree.css(".track-bar-container")

    parsed = {}
    for container in containers:
        result = _parse_container(container)
        if result:
            parsed[result["tracking_number"]] = result

    results = []
    for tn in expected_numbers:
        if tn in parsed:
            results.append(parsed[tn])
        else:
            results.append({
                "tracking_number": tn,
                "error": "Not found in page response",
                "data": None,
            })
    return results


def track_batch(driver, batch: List[str], timeout: int = BATCH_TIMEOUT) -> List[dict]:
    """一次请求查询最多 35 个单号"""
    labels = "%2C".join(batch) + "%2C"
    url = BULK_TRACKING_URL.format(labels=labels, count=len(batch))

    original_handle = driver.current_window_handle

    try:
        driver.get(url)

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CLASS_NAME, "tracking-number"))
            )
        except Exception:
            pass

        time.sleep(1)

        # USPS 页面可能通过 JS 打开新页签，需要切回并清理
        handles = driver.window_handles
        if len(handles) > 1:
            for h in handles:
                if h != original_handle:
                    driver.switch_to.window(h)
                    try:
                        html = driver.page_source
                    except Exception:
                        html = ""
                    if "track-bar-container" in html:
                        result = _parse_bulk_html(html, batch)
                        for extra in handles:
                            if extra != h:
                                driver.switch_to.window(extra)
                                driver.close()
                        driver.switch_to.window(h)
                        return result
            driver.switch_to.window(original_handle)
            for h in handles:
                if h != original_handle:
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
            driver.switch_to.window(original_handle)

        html = driver.page_source
        if "Akamai" in html or len(html) < 500:
            return [
                {"tracking_number": tn, "error": "Akamai blocked the browser", "data": None}
                for tn in batch
            ]

        return _parse_bulk_html(html, batch)

    except Exception as e:
        try:
            if driver.current_window_handle != original_handle:
                driver.switch_to.window(original_handle)
        except Exception:
            pass
        return [
            {"tracking_number": tn, "error": str(e), "data": None}
            for tn in batch
        ]


def track_bulk(
    tracking_numbers: List[str],
    max_retry: int = MAX_RETRY,
    browser_mode: str = BROWSER_MODE_LOCAL,
    bit_browser_id: Optional[str] = None,
) -> List[dict]:
    """批量追踪: 每次最多 35 个单号, 分批请求, 失败的进入重试队列

    browser_mode: "local" 使用本地无头 Chrome, "bit" 使用 Bit 浏览器
    bit_browser_id: Bit 浏览器窗口 ID (mode=bit 时必填)
    """
    results_map = {}
    total = len(tracking_numbers)
    batches = [
        tracking_numbers[i: i + BATCH_SIZE]
        for i in range(0, total, BATCH_SIZE)
    ]

    print(f"  Total: {total} numbers, split into {len(batches)} batch(es) of up to {BATCH_SIZE}")
    print(f"  Max retry: {max_retry}")
    print(f"  Browser mode: {browser_mode}")

    if browser_mode == BROWSER_MODE_BIT:
        if not bit_browser_id:
            raise ValueError("bit_browser_id is required when browser_mode='bit'")
        print(f"  Initializing Bit browser (id={bit_browser_id})...")
        driver = get_webdriver_bit(bit_browser_id)
    else:
        print("  Initializing local browser...")
        driver = get_webdriver()

    try:
        retry_queue: List[str] = []

        for bi, batch in enumerate(batches):
            batch_start = time.time()
            print(f"\n  === Batch {bi + 1}/{len(batches)} ({len(batch)} numbers) ===")

            batch_results = track_batch(driver, batch)

            for r in batch_results:
                tn = r["tracking_number"]
                if r.get("error"):
                    print(f"    {tn}: ERROR - {r['error'][:50]}")
                    retry_queue.append(tn)
                else:
                    results_map[tn] = r
                    status = (r.get("data") or {}).get("status", "Unknown")
                    n_events = len((r.get("data") or {}).get("events", []))
                    print(f"    {tn}: {status[:60]} ({n_events} events)")

            batch_elapsed = time.time() - batch_start
            print(f"  -> [Batch {bi + 1} completed in {batch_elapsed:.2f} seconds]")

            if bi < len(batches) - 1:
                print("  Waiting before next batch...")
                time.sleep(1)

        for attempt in range(1, max_retry + 1):
            if not retry_queue:
                break

            print(f"\n  {'=' * 50}")
            print(f"  RETRY round {attempt}/{max_retry} — {len(retry_queue)} number(s) to retry")
            print(f"  {'=' * 50}")

            retry_batches = [
                retry_queue[i: i + BATCH_SIZE]
                for i in range(0, len(retry_queue), BATCH_SIZE)
            ]
            next_retry_queue: List[str] = []

            retry_timeout = int(BATCH_TIMEOUT * (1.5 ** attempt))
            print(f"  Retry timeout: {retry_timeout}s")

            for bi, batch in enumerate(retry_batches):
                batch_start = time.time()
                print(f"\n  === Retry Batch {bi + 1}/{len(retry_batches)} ({len(batch)} numbers) ===")

                batch_results = track_batch(driver, batch, timeout=retry_timeout)

                for r in batch_results:
                    tn = r["tracking_number"]
                    if r.get("error"):
                        print(f"    {tn}: ERROR - {r['error'][:50]}")
                        next_retry_queue.append(tn)
                    else:
                        results_map[tn] = r
                        status = (r.get("data") or {}).get("status", "Unknown")
                        n_events = len((r.get("data") or {}).get("events", []))
                        print(f"    {tn}: {status[:60]} ({n_events} events)")

                batch_elapsed = time.time() - batch_start
                print(f"  -> [Retry Batch {bi + 1} completed in {batch_elapsed:.2f} seconds]")

                if bi < len(retry_batches) - 1:
                    print("  Waiting before next batch...")
                    time.sleep(1)

            retry_queue = next_retry_queue

        if retry_queue:
            print(f"\n  [!] {len(retry_queue)} number(s) still failed after {max_retry} retry(ies)")
            for tn in retry_queue:
                results_map[tn] = {
                    "tracking_number": tn,
                    "error": f"Failed after {max_retry} retry(ies)",
                    "data": None,
                }
    finally:
        if browser_mode == BROWSER_MODE_BIT:
            cleanup_and_close_bit(driver, bit_browser_id)
        else:
            driver.quit()

    return [results_map.get(tn, {"tracking_number": tn, "error": "Unknown failure", "data": None})
            for tn in tracking_numbers]


def format_result(r: dict) -> str:
    tn = r["tracking_number"]
    if r.get("error"):
        return f"  {tn}: ERROR - {r['error']}"
    d = r.get("data") or {}
    lines = [f"  {tn}: {d.get('status', 'Unknown')}"]
    if d.get("estimated_delivery"):
        lines.append(f"    Est. Delivery: {d['estimated_delivery']}")
    events = d.get("events") or []
    if events:
        lines.append(f"    Events ({len(events)}):")
        for ev in events[:10]:
            date = ev.get("date", "")
            desc = ev.get("description", "")
            loc = ev.get("location", "")
            loc_str = f" | {loc}" if loc else ""
            lines.append(f"      {date}{loc_str}: {desc}")
        if len(events) > 10:
            lines.append(f"      ... +{len(events) - 10} more events")
    return chr(10).join(lines)


def load_tracking_numbers(filepath: str) -> List[str]:
    numbers = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                numbers.append(line)
    return numbers


def main():
    script_start = time.time()

    parser = argparse.ArgumentParser(description="USPS Direct Tracker (Selenium)")
    parser.add_argument("tracking_numbers", nargs="*")
    parser.add_argument("--file", "-f")
    parser.add_argument("--output", "-o")
    parser.add_argument(
        "--mode", "-m",
        choices=[BROWSER_MODE_LOCAL, BROWSER_MODE_BIT],
        default=BROWSER_MODE_LOCAL,
        help="浏览器模式: local=本地无头Chrome(默认), bit=Bit浏览器",
    )
    parser.add_argument(
        "--bit-id",
        default=os.environ.get("BIT_BROWSER_ID", ""),
        help="Bit 浏览器窗口 ID (mode=bit 时必填，也可通过 BIT_BROWSER_ID 环境变量设置)",
    )
    args = parser.parse_args()

    numbers = list(args.tracking_numbers)
    if args.file:
        numbers.extend(load_tracking_numbers(args.file))
    if not numbers:
        default_file = os.path.join(os.path.dirname(__file__), "tracking_numbers.txt")
        if os.path.exists(default_file):
            print(f"Loading from {default_file}...")
            numbers = load_tracking_numbers(default_file)
    if not numbers:
        parser.error("No tracking numbers provided.")

    if args.mode == BROWSER_MODE_BIT and not args.bit_id:
        parser.error("--bit-id is required when --mode=bit (or set BIT_BROWSER_ID env var)")

    seen = set()
    numbers = [n for n in numbers if n not in seen and not seen.add(n)]

    mode_label = "Bit Browser" if args.mode == BROWSER_MODE_BIT else "Local Chrome"
    print(f"Tracking {len(numbers)} number(s) via USPS ({mode_label})...")
    results = track_bulk(
        numbers,
        browser_mode=args.mode,
        bit_browser_id=args.bit_id or None,
    )

    print(chr(10) + "=" * 70)
    print("TRACKING RESULTS")
    print("=" * 70)
    for r in results:
        print(format_result(r))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"Results saved to {args.output}")

    script_elapsed = time.time() - script_start  # 计算总耗时
    print(f"\n[Done] Total execution time: {script_elapsed:.2f} seconds.")


if __name__ == "__main__":
    main()