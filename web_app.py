import argparse
import base64
import html
import json
import logging
import mimetypes
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import re
import string

import requests

import general
from app_version import __version__
from coursera_dl import main_f
from define import OPENCOURSE_ONDEMAND_SPECIALIZATIONS_V1
from downloaders import DownloadCancelled, set_download_control_events
from localdb import SimpleDB


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
TREE_DEBUG = os.environ.get("COURSERA_DOWNLOADER_TREE_DEBUG", "").lower() in {"1", "true", "yes", "on"}


def disable_console_quick_edit():
    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        quick_edit = 0x0040
        extended_flags = 0x0080
        new_mode = (mode.value | extended_flags) & ~quick_edit
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        logging.debug("Could not disable console QuickEdit mode.", exc_info=True)


class JobLogHandler(logging.Handler):
    def __init__(self, job):
        super().__init__()
        self.job = job
        self.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    def emit(self, record):
        message = self.format(record)
        if '127.0.0.1 - "' in message:
            return
        self.job.add_log(message)


class DownloadJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.started_at = None
        self.finished_at = None
        self.status = "idle"
        self.error = ""
        self.command = []
        self.logs = []
        self.last_payload = None
        self.last_resume = False
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()

    def add_log(self, message):
        with self.lock:
            timestamp = time.strftime("%H:%M:%S")
            self.logs.append(f"[{timestamp}] {message}")
            self.logs = self.logs[-500:]

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "status": self.status,
                "paused": self.pause_event.is_set(),
                "error": self.error,
                "command": redact_command(self.command),
                "logs": list(self.logs),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "can_retry": self.last_payload is not None,
                "last_was_resume": self.last_resume,
            }

    def remember_request(self, payload, resume):
        with self.lock:
            self.last_payload = dict(payload)
            self.last_resume = resume

    def retry_request(self):
        with self.lock:
            if self.last_payload is None:
                raise RuntimeError("No previous download request to retry.")
            return dict(self.last_payload), self.last_resume

    def has_request_for(self, course_input, download_path):
        with self.lock:
            if not self.last_payload:
                return False
            last_course = self.last_payload.get("course", "").strip()
            last_path = self.last_payload.get("path", "").strip()
        return last_course == course_input and Path(last_path).expanduser() == Path(download_path).expanduser()

    def start(self, command):
        with self.lock:
            if self.running:
                raise RuntimeError("A download is already running.")
            self.running = True
            self.status = "running"
            self.error = ""
            self.command = list(command)
            self.logs = []
            self.started_at = time.time()
            self.finished_at = None
            self.cancel_event.clear()
            self.pause_event.clear()

        thread = threading.Thread(target=self._run, args=(list(command),), daemon=True)
        thread.start()

    def pause(self):
        with self.lock:
            if not self.running:
                raise RuntimeError("No download is running.")
            if self.status == "canceling":
                raise RuntimeError("Download is already canceling.")
            self.pause_event.set()
            self.status = "paused"
        self.add_log("Download paused.")

    def resume(self):
        with self.lock:
            if not self.running:
                raise RuntimeError("No download is running.")
            self.pause_event.clear()
            self.status = "running"
        self.add_log("Download resumed.")

    def cancel(self):
        with self.lock:
            if not self.running:
                raise RuntimeError("No download is running.")
            self.cancel_event.set()
            self.pause_event.clear()
            self.status = "canceling"
        self.add_log("Cancel requested.")

    def _run(self, command):
        handler = JobLogHandler(self)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        set_download_control_events(self.cancel_event, self.pause_event)
        try:
            self.add_log("Resume started." if "--resume" in command else "Download started.")
            main_f(command)
        except DownloadCancelled:
            with self.lock:
                self.status = "canceled"
                self.error = ""
            self.add_log("Download canceled.")
        except Exception as exc:
            with self.lock:
                self.status = "failed"
                self.error = friendly_error_message(exc)
            self.add_log(traceback.format_exc())
        else:
            with self.lock:
                self.status = "completed"
            self.add_log("Download completed.")
        finally:
            set_download_control_events(None, None)
            root_logger.removeHandler(handler)
            with self.lock:
                self.running = False
                self.pause_event.clear()
                self.cancel_event.clear()
                self.finished_at = time.time()


JOB = DownloadJob()
AUTH_LOCK = threading.Lock()
AUTH_CACHE = None
AUTH_CACHE_TTL = 60
PROGRAM_COURSE_CACHE = {}
PROGRAM_COURSE_FALLBACKS = {
    "ibm-ai-product-manager": [
        "product-management-an-introduction",
        "product-management-foundations-and-stakeholder-collaboration",
        "product-management-initial-product-strategy-and-plan",
        "product-management-developing-and-delivering-a-new-product",
        "introduction-to-ai",
        "generative-ai-introduction-and-applications",
        "generative-ai-prompt-engineering-for-everyone",
        "generative-ai-foundation-models-and-platforms",
        "product-management-building-ai-powered-products",
        "generative-ai-supercharge-your-product-management-career",
    ],
}


def redact_command(command):
    redacted = []
    hide_next = False
    for item in command:
        if hide_next:
            redacted.append("[redacted]")
            hide_next = False
            continue
        redacted.append(item)
        if item in {"--cauth", "-ca", "--password", "-p"}:
            hide_next = True
    return redacted


def friendly_error_message(exc):
    message = str(exc)
    if "appbound encryption" in message.lower():
        return (
            "Browser cookie access failed because Chrome/Edge protects cookies with "
            "Windows app-bound encryption. Start the app with start_web_app.bat, "
            "then click Login again."
        )
    return message


def auth_detection_error_message(details):
    logging.debug("Coursera login auto-detection details: %s", details)
    return (
        "Could not auto-detect your Coursera login. Start the app with "
        "start_web_app.bat, then click Login again."
    )


def detect_cauth_cookie():
    try:
        import rookiepy
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency: rookiepy. Install dependencies with "
            "python -m pip install -r requirements.txt, then restart the app."
        ) from exc

    errors = []
    browser_order = ["firefox", "chrome", "edge", "brave"]
    for browser in browser_order:
        if browser not in general.ALLOWED_BROWSERS:
            continue
        cookie_loader = getattr(rookiepy, browser, None)
        if cookie_loader is None:
            continue

        try:
            cookies = cookie_loader(["coursera.org"])
        except Exception as exc:
            errors.append(f"{browser}: {friendly_error_message(exc)}")
            continue

        for cookie in cookies:
            name = cookie.get("name") if isinstance(cookie, dict) else getattr(cookie, "name", None)
            if name == "CAUTH":
                value = cookie.get("value") if isinstance(cookie, dict) else getattr(cookie, "value", None)
                if value:
                    return browser, value, cookies

        errors.append(f"{browser}: CAUTH cookie not found")

    detail = "; ".join(errors) if errors else "No supported browser cookie reader was available."
    raise RuntimeError(auth_detection_error_message(detail))


def detect_auth_status():
    browser, cauth_cookie, browser_cookies = detect_cauth_cookie()
    user_name = get_coursera_user_name(cauth_cookie, browser_cookies)
    message = f"Logged in as {user_name}." if user_name else "Logged in."
    if not user_name:
        logging.debug("Authentication detected from %s, but no profile name was found.", browser)
    return {
        "authenticated": True,
        "browser": browser,
        "user_name": user_name,
        "message": message,
    }


def detect_auth_status_cached():
    global AUTH_CACHE

    now = time.time()
    if AUTH_CACHE and now - AUTH_CACHE["saved_at"] < AUTH_CACHE_TTL:
        return dict(AUTH_CACHE["status"])

    with AUTH_LOCK:
        now = time.time()
        if AUTH_CACHE and now - AUTH_CACHE["saved_at"] < AUTH_CACHE_TTL:
            return dict(AUTH_CACHE["status"])

        status = detect_auth_status()
        AUTH_CACHE = {
            "saved_at": now,
            "status": dict(status),
        }
        return status


def apply_browser_cookies(session, browser_cookies):
    if not browser_cookies:
        return

    for cookie in browser_cookies:
        name = cookie.get("name") if isinstance(cookie, dict) else getattr(cookie, "name", None)
        value = cookie.get("value") if isinstance(cookie, dict) else getattr(cookie, "value", None)
        if not name or value is None:
            continue
        domain = cookie.get("domain") if isinstance(cookie, dict) else getattr(cookie, "domain", None)
        session.cookies.set(name, value, domain=domain or ".coursera.org")


def get_coursera_user_name(cauth_cookie, browser_cookies=None):
    session = requests.Session()
    apply_browser_cookies(session, browser_cookies)
    session.cookies.set("CAUTH", cauth_cookie, domain=".coursera.org")
    endpoints = [
        "https://api.coursera.org/api/externalBasicProfiles.v1?q=me&fields=name,fullName,displayName,email,emailAddress,timezone,locale,privacy",
        "https://www.coursera.org/api/externalBasicProfiles.v1?q=me&fields=name,fullName,displayName,email,emailAddress,timezone,locale,privacy",
        "https://www.coursera.org/api/userProfiles.v1?q=me&fields=fullName,name",
        "https://www.coursera.org/api/users.v1/me?fields=fullName,name",
        "https://www.coursera.org/api/memberships.v1?q=me&fields=name,fullName,email,emailAddress",
    ]
    seen_profile_ids = set()

    for endpoint in endpoints:
        name, profile_ids = get_name_from_profile_endpoint(session, endpoint)
        if name:
            return name
        seen_profile_ids.update(profile_ids)

    for profile_id in seen_profile_ids:
        for endpoint in profile_endpoints_for_id(profile_id):
            name, _ = get_name_from_profile_endpoint(session, endpoint)
            if name:
                return name

    return get_name_from_coursera_pages(session) or get_name_from_cauth(cauth_cookie)


def profile_endpoints_for_id(profile_id):
    encoded_id = str(profile_id).strip()
    return [
        f"https://api.coursera.org/api/externalBasicProfiles.v1/{encoded_id}?fields=name,fullName,displayName,publicName,email,emailAddress,timezone,locale,privacy",
        f"https://www.coursera.org/api/externalBasicProfiles.v1/{encoded_id}?fields=name,fullName,displayName,publicName,email,emailAddress,timezone,locale,privacy",
        f"https://api.coursera.org/api/onDemandSocialProfiles.v1/{encoded_id}?fields=fullName,publicName,name,photoUrl",
        f"https://www.coursera.org/api/onDemandSocialProfiles.v1/{encoded_id}?fields=fullName,publicName,name,photoUrl",
        f"https://www.coursera.org/api/userProfiles.v1/{encoded_id}?fields=fullName,name,displayName,publicName",
    ]


def get_name_from_profile_endpoint(session, endpoint):
    try:
        response = session.get(endpoint, timeout=10)
        logging.debug("Profile lookup %s returned HTTP %s.", endpoint, response.status_code)
        if response.status_code in {404, 405}:
            return "", set()
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logging.debug("Profile lookup failed for %s: %s", endpoint, exc)
        return "", set()
    except ValueError as exc:
        logging.debug("Profile lookup returned invalid JSON for %s: %s", endpoint, exc)
        return "", set()

    candidates = []
    if isinstance(data, dict):
        candidates.append(data)
        candidates.extend(data.get("elements", []) if isinstance(data.get("elements"), list) else [])
        linked = data.get("linked", {})
        if isinstance(linked, dict):
            for value in linked.values():
                if isinstance(value, list):
                    candidates.extend(value)
                elif isinstance(value, dict):
                    candidates.append(value)

    profile_ids = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        logging.debug("Profile candidate keys: %s", sorted(candidate.keys()))
        name = extract_profile_name(candidate)
        if name:
            return name, profile_ids
        for key in ("id", "userId", "externalUserId"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                profile_ids.add(value.strip())
            elif isinstance(value, int):
                profile_ids.add(str(value))

    return "", profile_ids


def extract_profile_name(candidate):
    for key in ("fullName", "displayName", "publicName", "name", "firstName", "email", "emailAddress"):
        value = profile_value_to_text(candidate.get(key))
        if is_usable_profile_name(value):
            return value
    return ""


def profile_value_to_text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("_value", "value", "fullName", "displayName", "publicName", "name", "firstName", "email", "emailAddress"):
            text = profile_value_to_text(value.get(key))
            if text:
                return text
        for key in ("en", "en_US", "en-US"):
            text = profile_value_to_text(value.get(key))
            if text:
                return text
        for nested_value in value.values():
            text = profile_value_to_text(nested_value)
            if text:
                return text
    if isinstance(value, list):
        for item in value:
            text = profile_value_to_text(item)
            if text:
                return text
    return ""


def is_usable_profile_name(value):
    if not value:
        return False
    cleaned = str(value).strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if lowered in {"coursera", "learner", "student", "user", "none", "null"}:
        return False
    if len(cleaned) > 120:
        return False
    return True


def get_name_from_coursera_pages(session):
    urls = [
        "https://www.coursera.org/?skipBrowseRedirect=true",
        "https://www.coursera.org/account-profile",
        "https://www.coursera.org/user/edit",
    ]
    for url in urls:
        try:
            response = session.get(url, timeout=10)
        except requests.RequestException as exc:
            logging.debug("Profile page lookup failed for %s: %s", url, exc)
            continue
        logging.debug("Profile page lookup %s returned HTTP %s.", url, response.status_code)
        if response.status_code != 200:
            continue
        name = extract_name_from_html(response.text)
        if name:
            return name
    return ""


def extract_name_from_html(markup):
    decoded = html.unescape(markup or "")
    for key in ("fullName", "displayName", "publicName", "name", "firstName", "emailAddress", "email"):
        pattern = rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"'
        for match in re.finditer(pattern, decoded):
            value = decode_json_string(match.group(1))
            if is_usable_profile_name(value):
                return value
    return ""


def decode_json_string(value):
    try:
        return json.loads(f'"{value}"').strip()
    except (TypeError, ValueError):
        return str(value).strip()


def get_name_from_cauth(cauth_cookie):
    try:
        parts = cauth_cookie.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception:
        logging.debug("Could not decode CAUTH payload for profile fallback.")
        return ""

    logging.debug("CAUTH payload keys: %s", sorted(data.keys()) if isinstance(data, dict) else type(data).__name__)
    if not isinstance(data, dict):
        return ""
    for key in ("fullName", "name", "displayName", "email", "emailAddress", "sub"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def default_download_folder():
    return Path.home() / "Downloads" / "Coursera"


def project_folder(class_name, is_multi_course_program, download_folder):
    base = Path(download_folder).expanduser()
    if is_multi_course_program and class_name:
        return base / class_name
    return base


def migrate_legacy_program_layout(class_name, is_multi_course_program, download_folder):
    if not is_multi_course_program or not class_name:
        return
    base = Path(download_folder).expanduser()
    if not base.exists() or not base.is_dir():
        return
    target = base / class_name
    if base.resolve() == target.resolve():
        return

    course_slugs = list(PROGRAM_COURSE_FALLBACKS.get(class_name, []))
    course_slugs.extend(get_program_course_slugs_from_page(class_name))
    course_slugs.extend(get_program_course_slugs(class_name))
    course_slugs = list(dict.fromkeys(course_slugs))
    if not course_slugs:
        return

    moved = []
    for entry in list(base.iterdir()):
        if entry == target:
            continue
        if entry.is_dir():
            entry_slug = canonical_folder_slug(entry.name)
            if entry_slug in course_slugs:
                moved.append(entry)
                continue
        if entry.is_file() and entry.suffix.lower() == ".json":
            stem = entry.stem
            for slug in course_slugs:
                if stem == f"{slug}-site-tree" or stem == f"{slug}-syllabus-parsed":
                    moved.append(entry)
                    break
    if not moved:
        return

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.warning("Could not create project folder %s: %s", target, exc)
        return

    for entry in moved:
        destination = target / entry.name
        if destination.exists():
            logging.info("Skipping migration of %s (destination already exists)", entry)
            continue
        try:
            entry.rename(destination)
            logging.info("Migrated %s -> %s", entry, destination)
        except OSError as exc:
            logging.warning("Could not migrate %s -> %s: %s", entry, destination, exc)


def allowed_open_roots():
    roots = [ROOT, default_download_folder()]
    try:
        db = SimpleDB("data.bin")
        saved_path = db.get_full_db().get("argdict", {}).get("path", "")
        if saved_path:
            roots.append(Path(saved_path).expanduser())
    except Exception:
        pass
    resolved = []
    seen = set()
    for root in roots:
        try:
            resolved_root = Path(root).expanduser().resolve()
        except OSError:
            continue
        key = str(resolved_root).lower() if os.name == "nt" else str(resolved_root)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(resolved_root)
    return resolved


def is_path_within_allowed_roots(path):
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    for root in allowed_open_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def choose_download_folder(initial_path):
    initial_dir = Path(initial_path or default_download_folder()).expanduser()
    if not initial_dir.exists():
        initial_dir = initial_dir.parent if initial_dir.parent.exists() else Path.home()

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            title="Select download folder",
            initialdir=str(initial_dir),
        )
    finally:
        root.destroy()

    return selected


def build_download_command(payload, resume=False, item=None):
    detected_browser, cauth_cookie, _ = detect_cauth_cookie()
    course_input = payload.get("course", "").strip()
    download_path = payload.get("path", "").strip()
    resolution = payload.get("video_resolution", "720p").strip()
    language_name = payload.get("subtitle_language", "English").strip()

    class_name, is_multi_course_program = general.parse_course_input(course_input)
    if not class_name:
        raise ValueError("Enter a valid Coursera course, specialization, or professional certificate URL.")

    item = item or {}
    item_type = item.get("type", "")
    item_course = item.get("course", "").strip()
    item_section = item.get("section", "").strip()
    item_lecture = item.get("lecture", "").strip()

    if item_course:
        class_name = item_course
        is_multi_course_program = False

    if not download_path:
        raise ValueError("Enter a download folder path.")

    download_folder = Path(download_path).expanduser()
    try:
        download_folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Could not create the download folder: {exc}") from exc

    if not download_folder.is_dir():
        raise ValueError("The download path must be a folder.")

    if resolution not in {"360p", "540p", "720p"}:
        raise ValueError("Choose a valid video resolution.")

    if language_name not in general.LANG_NAME_TO_CODE_MAPPING:
        raise ValueError("Choose a valid subtitle language.")

    migrate_legacy_program_layout(class_name, is_multi_course_program, download_folder)
    project_root = project_folder(class_name, is_multi_course_program, download_folder)
    try:
        project_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Could not create the project folder: {exc}") from exc

    language_code = general.LANG_NAME_TO_CODE_MAPPING[language_name]
    class_names = [class_name]
    expand_in_downloader = False
    if is_multi_course_program:
        class_names = resolve_program_course_slugs(class_name, project_root)
        expand_in_downloader = class_names == [class_name]
        migrate_program_course_folders(project_root, class_names)

    command = [
        *class_names,
        "--path",
        str(project_root),
        "--video-resolution",
        resolution,
        "--subtitle-language",
        language_code if language_code else "en",
        "--download-quizzes",
        "--download-notebooks",
        "--disable-url-skipping",
        "--unrestricted-filenames",
        "--combined-section-lectures-nums",
        "--cache-syllabus",
        "--jobs",
        "1",
    ]

    if is_multi_course_program and len(class_names) > 1:
        command.append("--number-course-folders")

    command[0:0] = ["--cauth", cauth_cookie]

    if language_code == "":
        command.extend(["--ignore-formats", "srt"])

    if resume:
        command.append("--resume")

    if expand_in_downloader:
        command.append("--specialization")

    if item_type == "section" and item_section:
        command.extend(["--section_filter", re.escape(item_section)])
    elif item_type == "lecture" and item_lecture:
        command.extend(["--lecture_filter", re.escape(item_lecture)])

    return command, {
        "browser": detected_browser,
        "argdict": {
            "ca": "",
            "classname": course_input,
            "path": str(download_folder),
            "video_resolution": resolution,
            "sl": language_code,
            "ca": "",
        },
    }


def get_download_state(payload):
    course_input = payload.get("course", "").strip()
    download_path = payload.get("path", "").strip()
    quick = bool(payload.get("quick"))
    class_name, is_multi_course_program = general.parse_course_input(course_input)
    if not class_name or not download_path:
        tree_debug(
            "download-state skipped: course_input=%r class_name=%r download_path=%r",
            course_input,
            class_name,
            download_path,
        )
        return {"can_resume": False, "class_name": class_name}

    download_folder = Path(download_path).expanduser()
    project_root = project_folder(class_name, is_multi_course_program, download_folder)
    course_folder = project_root / class_name
    cache_file = Path(f"{class_name}-syllabus-parsed.json")
    cache_file_in_downloads = project_root / f"{class_name}-syllabus-parsed.json"
    cache_file_legacy = download_folder / f"{class_name}-syllabus-parsed.json"
    found_course_folder = find_existing_course_folder(class_name, project_root)
    if not found_course_folder and is_multi_course_program:
        found_course_folder = find_existing_program_course_folder(class_name, project_root)
    if quick:
        activity_groups = build_quick_activity(class_name, is_multi_course_program, download_folder)
    else:
        activity_groups = build_downloaded_activity(class_name, is_multi_course_program, download_folder)
    can_resume = (
        course_folder.exists()
        or cache_file.exists()
        or cache_file_in_downloads.exists()
        or cache_file_legacy.exists()
        or found_course_folder is not None
        or JOB.has_request_for(course_input, download_path)
    )
    tree_debug(
        "download-state: input=%r class=%s multi=%s path=%s can_resume=%s groups=%s",
        course_input,
        class_name,
        is_multi_course_program,
        download_folder,
        can_resume,
        summarize_activity_groups(activity_groups),
    )

    return {
        "can_resume": can_resume,
        "class_name": class_name,
        "course_folder": str(found_course_folder or course_folder),
        "resume_path": str((found_course_folder or course_folder).parent) if can_resume else "",
        "cache_file": str(cache_file),
        "download_cache_file": str(cache_file_in_downloads),
        "site_tree_files": [
            str(path) for path in find_site_tree_files(class_name, is_multi_course_program, download_folder)
        ],
        "activity_groups": activity_groups,
    }


def find_existing_program_course_folder(program_slug, selected_folder):
    for course_slug in resolve_program_course_slugs(program_slug, selected_folder):
        found_course_folder = find_existing_course_folder(course_slug, selected_folder)
        if found_course_folder:
            return found_course_folder
    return None


def migrate_program_course_folders(download_folder, course_slugs):
    if len(course_slugs) <= 1:
        return

    for index, course_slug in enumerate(course_slugs, start=1):
        plain = Path(download_folder).expanduser() / course_slug
        numbered = Path(download_folder).expanduser() / ordered_course_folder_name(index, course_slug)
        if numbered.exists():
            continue
        if plain.exists() and plain.is_dir():
            try:
                plain.rename(numbered)
                logging.info("[tree-debug] Renamed course folder %s -> %s", plain, numbered)
            except OSError as exc:
                logging.warning("Could not rename course folder %s -> %s: %s", plain, numbered, exc)


def build_quick_activity(class_name, is_multi_course_program, selected_folder):
    migrate_legacy_program_layout(class_name, is_multi_course_program, selected_folder)
    project_root = project_folder(class_name, is_multi_course_program, selected_folder)
    if is_multi_course_program:
        course_slugs = resolve_program_course_slugs(class_name, project_root)
    else:
        course_slugs = [class_name]

    groups = []
    course_total = len(course_slugs)
    for index, course_slug in enumerate(course_slugs, start=1):
        site_tree = load_site_tree(course_slug, project_root)
        course_folder = find_existing_course_folder(course_slug, project_root)
        if not site_tree:
            groups.append({
                "title": numbered_course_title(index, course_total, course_slug),
                "slug": course_slug,
                "status": "manifest-missing",
                "tree_source": "missing",
                "path": str(course_folder or ""),
                "file_count": 0,
                "downloadable_count": 0,
                "expected_count": 0,
                "order_index": index,
                "order_total": course_total,
                "modules": [],
                "loose": [
                    "Site tree manifest is missing. Refresh/resume this course to create "
                    f"{course_slug}-site-tree.json."
                ],
            })
            continue

        modules, downloadable_total, expected_total = quick_modules_from_site_tree(site_tree)
        groups.append({
            "title": numbered_course_title(index, course_total, course_slug),
            "slug": course_slug,
            "status": "manifest",
            "tree_source": "site",
            "path": str(course_folder or ""),
            "file_count": 0,
            "downloadable_count": downloadable_total,
            "expected_count": expected_total,
            "order_index": index,
            "order_total": course_total,
            "modules": modules,
            "loose": [],
        })
    return groups


def quick_modules_from_site_tree(site_tree):
    modules = []
    downloadable_total = 0
    expected_total = 0
    for site_module in site_tree.get("modules", []) if isinstance(site_tree, dict) else []:
        module_slug = str(site_module.get("slug", ""))
        sections, mod_downloadable, mod_expected = quick_sections_from_site_tree(site_module.get("sections", []))
        modules.append({
            "title": site_module.get("title") or display_slug(module_slug),
            "slug": module_slug,
            "sections": sections,
            "file_count": 0,
            "downloadable_count": mod_downloadable,
            "expected_count": mod_expected,
            "tree_source": "site",
        })
        downloadable_total += mod_downloadable
        expected_total += mod_expected
    return modules, downloadable_total, expected_total


def quick_sections_from_site_tree(site_sections):
    sections = []
    downloadable_total = 0
    expected_total = 0
    for site_section in site_sections if isinstance(site_sections, list) else []:
        section_slug = str(site_section.get("slug", ""))
        items = []
        section_downloadable = 0
        section_expected = 0
        for index, site_item in enumerate(site_section.get("items", []) if isinstance(site_section, dict) else []):
            section_expected += 1
            downloadable = bool(site_item.get("downloadable"))
            if downloadable:
                section_downloadable += 1
            items.append({
                "kind": site_item.get("type") or "item",
                "text": site_item.get("title") or display_slug(site_item.get("slug", "")) or f"Item {index + 1}",
                "slug": site_item.get("slug", ""),
                "status": "pending" if downloadable else "not-downloadable",
                "downloadable": downloadable,
                "downloaded_count": 0,
                "expected_count": int(site_item.get("download_file_count") or 0),
                "sources": [],
                "reason": item_not_downloadable_reason(site_item),
            })
        sections.append({
            "title": site_section.get("title") or display_slug(section_slug),
            "slug": section_slug,
            "items": items,
            "file_count": 0,
            "downloadable_count": section_downloadable,
            "expected_count": section_expected,
            "tree_source": "site",
        })
        downloadable_total += section_downloadable
        expected_total += section_expected
    return sections, downloadable_total, expected_total


def build_downloaded_activity(class_name, is_multi_course_program, selected_folder):
    migrate_legacy_program_layout(class_name, is_multi_course_program, selected_folder)
    project_root = project_folder(class_name, is_multi_course_program, selected_folder)
    if is_multi_course_program:
        course_slugs = resolve_program_course_slugs(class_name, project_root)
        migrate_program_course_folders(project_root, course_slugs)
    else:
        course_slugs = [class_name]

    groups = []
    course_total = len(course_slugs)
    tree_debug(
        "build activity: class=%s multi=%s selected=%s project=%s course_slugs=%s",
        class_name,
        is_multi_course_program,
        selected_folder,
        project_root,
        course_slugs,
    )
    for index, course_slug in enumerate(course_slugs, start=1):
        course_folder = find_existing_course_folder(course_slug, project_root)
        if course_folder:
            groups.append(scan_course_folder(course_slug, course_folder, index, course_total))
        elif is_multi_course_program:
            site_tree = load_site_tree(course_slug, project_root)
            if site_tree:
                modules, downloaded_items, downloadable_total, expected_total = site_tree_activity_modules(site_tree, [])
                tree_source = "site"
                loose = ["Course folder is missing. Showing full site tree from manifest."]
            else:
                modules, downloaded_items, downloadable_total, expected_total = [], 0, 0, 0
                tree_source = "missing"
                loose = [
                    "Site tree manifest is missing. Refresh/resume this course to create "
                    f"{course_slug}-site-tree.json. Folder scan fallback is disabled."
                ]
            groups.append({
                "title": numbered_course_title(index, course_total, course_slug),
                "slug": course_slug,
                "status": "missing" if site_tree else "manifest-missing",
                "tree_source": tree_source,
                "path": "",
                "file_count": downloaded_items,
                "downloadable_count": downloadable_total,
                "expected_count": expected_total,
                "order_index": index,
                "order_total": course_total,
                "modules": modules,
                "loose": loose,
            })
            tree_debug(
                "missing course folder: slug=%s site_tree=%s expected_total=%s",
                course_slug,
                bool(site_tree),
                expected_total,
            )

    return groups


def resolve_program_course_slugs(program_slug, selected_folder):
    page_slugs = get_program_course_slugs_from_page(program_slug)
    api_slugs = get_program_course_slugs(program_slug)
    fallback_slugs = list(PROGRAM_COURSE_FALLBACKS.get(program_slug, []))
    course_slugs = list(page_slugs or fallback_slugs or api_slugs)
    for slug in api_slugs:
        if slug not in course_slugs:
            course_slugs.append(slug)

    discovered = discover_downloaded_program_course_slugs(program_slug, selected_folder)
    for slug in discovered:
        if slug not in course_slugs:
            course_slugs.append(slug)

    tree_debug(
        "resolve program: program=%s page=%s api=%s fallback=%s discovered=%s final=%s",
        program_slug,
        page_slugs,
        api_slugs,
        fallback_slugs,
        discovered,
        course_slugs or [program_slug],
    )
    return course_slugs or [program_slug]


def discover_downloaded_program_course_slugs(program_slug, selected_folder):
    known_slugs = set(PROGRAM_COURSE_FALLBACKS.get(program_slug, []))
    if not known_slugs:
        return []

    roots = [Path(selected_folder).expanduser()]
    try:
        db = SimpleDB("data.bin")
        saved_path = db.get_full_db().get("argdict", {}).get("path", "")
        if saved_path:
            roots.append(Path(saved_path).expanduser())
    except Exception:
        pass

    roots.append(default_download_folder())
    deduped_roots = []
    seen_roots = set()
    for root in roots:
        root_key = str(root).lower()
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        deduped_roots.append(root)

    found = []
    checked_roots = []
    for root in deduped_roots:
        checked_roots.append(str(root))
        if not root.exists() or not root.is_dir():
            continue
        for entry in root.iterdir():
            entry_slug = canonical_folder_slug(entry.name)
            if entry.is_dir() and entry_slug in known_slugs and entry_slug not in found:
                found.append(entry_slug)
    tree_debug(
        "discover downloaded courses: program=%s roots=%s found=%s",
        program_slug,
        checked_roots,
        found,
    )
    return found


def scan_course_folder(course_slug, course_folder, order_index=0, order_total=0):
    scanned_modules = []
    loose_files = []
    total_files = 0
    course_folder = Path(course_folder)
    expected = load_expected_activity_counts(course_slug, course_folder.parent)
    expected_modules = expected.get("modules", {})

    for entry in sorted(course_folder.iterdir(), key=lambda item: natural_sort_key(item.name)):
        if entry.is_dir():
            module, file_count = scan_module_folder(entry, expected_modules)
            scanned_modules.append(module)
            total_files += file_count
        elif entry.is_file():
            loose_files.append(file_activity_item(entry))
            total_files += 1

    site_tree = load_site_tree(course_slug, course_folder.parent)
    if site_tree:
        modules, downloaded_items, downloadable_total, expected_total = site_tree_activity_modules(site_tree, scanned_modules)
        status_target = downloadable_total or expected_total
        status = "downloaded" if status_target and downloaded_items >= status_target else "partial"
        display_count = downloaded_items
        loose = [
            f"{downloaded_items} downloaded website item{'s' if downloaded_items != 1 else ''} "
            f"from {downloadable_total} downloadable item{'s' if downloadable_total != 1 else ''} "
            f"({expected_total} site item{'s' if expected_total != 1 else ''}); "
            f"{total_files} real file{'s' if total_files != 1 else ''} on disk."
        ]
    else:
        modules = []
        downloadable_total = 0
        expected_total = 0
        status = "manifest-missing"
        display_count = 0
        loose = [
            "Site tree manifest is missing. Refresh/resume this course to create "
            f"{course_slug}-site-tree.json. Folder scan fallback is disabled."
        ]

    if loose_files and site_tree:
        modules.insert(0, {
            "title": "Course files",
            "slug": "course-files",
            "sections": [{
                "title": "Files",
                "slug": "files",
                "items": loose_files[:80],
            }],
        })

    tree_debug(
        "scan course: slug=%s path=%s site_tree=%s modules=%s files=%s display=%s expected=%s status=%s",
        course_slug,
        course_folder,
        bool(site_tree),
        len(modules),
        total_files,
        display_count,
        expected_total,
        status,
    )
    return {
        "title": numbered_course_title(order_index, order_total, course_slug),
        "slug": course_slug,
        "status": status,
        "tree_source": "site" if site_tree else "missing",
        "path": str(course_folder),
        "file_count": display_count,
        "downloadable_count": downloadable_total,
        "expected_count": expected_total,
        "downloaded_file_count": total_files,
        "order_index": order_index,
        "order_total": order_total,
        "modules": modules,
        "loose": loose,
    }


def scan_module_folder(module_folder, expected_modules=None):
    sections = []
    direct_files = []
    total_files = 0
    expected_modules = expected_modules or {}
    expected_module = expected_modules.get(canonical_folder_slug(module_folder.name), {})
    expected_sections = expected_module.get("sections", {})

    for entry in sorted(module_folder.iterdir(), key=lambda item: natural_sort_key(item.name)):
        if entry.is_dir():
            section_items = []
            section_file_count = 0
            for file_path in sorted(entry.rglob("*"), key=lambda item: natural_sort_key(str(item.relative_to(entry)))):
                if file_path.is_file():
                    section_file_count += 1
                    if len(section_items) < 80:
                        section_items.append(file_activity_item(file_path, entry))
            total_files += section_file_count
            sections.append({
                "title": display_slug(entry.name),
                "slug": entry.name,
                "items": section_items,
                "file_count": section_file_count,
                "expected_count": expected_sections.get(canonical_folder_slug(entry.name), section_file_count),
            })
        elif entry.is_file():
            direct_files.append(file_activity_item(entry))
            total_files += 1

    if direct_files:
        sections.insert(0, {
            "title": "Files",
            "slug": "files",
            "items": direct_files[:80],
            "file_count": len(direct_files),
        })

    existing_section_slugs = {canonical_folder_slug(section.get("slug", "")) for section in sections}
    for section_slug, section_expected in expected_sections.items():
        if section_slug not in existing_section_slugs:
            sections.append({
                "title": display_slug(section_slug),
                "slug": section_slug,
                "items": [],
                "file_count": 0,
                "expected_count": section_expected,
            })

    tree_debug(
        "scan module: path=%s sections=%s files=%s expected=%s",
        module_folder,
        len(sections),
        total_files,
        expected_module.get("total", total_files),
    )
    return {
        "title": display_slug(module_folder.name),
        "slug": module_folder.name,
        "sections": sections,
        "file_count": total_files,
        "expected_count": expected_module.get("total", total_files),
    }, total_files


def load_site_tree(course_slug, selected_folder):
    candidates = [
        Path(selected_folder).expanduser() / f"{course_slug}-site-tree.json",
        ROOT / f"{course_slug}-site-tree.json",
    ]
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            tree_debug("site tree: slug=%s file=%s read_error=%s", course_slug, candidate, exc)
            continue
        tree_debug("site tree: slug=%s file=%s modules=%s", course_slug, candidate, len(data.get("modules", [])))
        return data
    tree_debug("site tree: slug=%s selected=%s manifest=missing", course_slug, selected_folder)
    return None


def site_tree_activity_modules(site_tree, scanned_modules):
    scanned_by_slug = {
        canonical_folder_slug(module.get("slug", "")): module
        for module in scanned_modules
    }
    modules = []
    downloaded_total = 0
    downloadable_total = 0
    expected_total = 0

    for site_module in site_tree.get("modules", []) if isinstance(site_tree, dict) else []:
        module_slug = str(site_module.get("slug", ""))
        scanned_module = scanned_by_slug.get(canonical_folder_slug(module_slug), {})
        module_sections, module_done, module_downloadable, module_expected = site_tree_activity_sections(
            site_module.get("sections", []), scanned_module)
        modules.append({
            "title": site_module.get("title") or display_slug(module_slug),
            "slug": module_slug,
            "sections": module_sections,
            "file_count": module_done,
            "downloadable_count": module_downloadable,
            "expected_count": module_expected,
            "tree_source": "site",
        })
        downloaded_total += module_done
        downloadable_total += module_downloadable
        expected_total += module_expected

    return modules, downloaded_total, downloadable_total, expected_total


def site_tree_activity_sections(site_sections, scanned_module):
    scanned_sections = scanned_module.get("sections", []) if isinstance(scanned_module, dict) else []
    scanned_by_slug = {
        canonical_folder_slug(section.get("slug", "")): section
        for section in scanned_sections
    }
    sections = []
    downloaded_total = 0
    downloadable_total = 0
    expected_total = 0

    for site_section in site_sections if isinstance(site_sections, list) else []:
        section_slug = str(site_section.get("slug", ""))
        scanned_section = scanned_by_slug.get(canonical_folder_slug(section_slug), {})
        files = scanned_section.get("items", []) if isinstance(scanned_section, dict) else []
        section_items, section_done, section_downloadable, section_expected = site_tree_activity_items(
            site_section.get("items", []), files)
        sections.append({
            "title": site_section.get("title") or display_slug(section_slug),
            "slug": section_slug,
            "items": section_items,
            "file_count": section_done,
            "downloadable_count": section_downloadable,
            "expected_count": section_expected,
            "downloaded_file_count": scanned_section.get("file_count", 0) if isinstance(scanned_section, dict) else 0,
            "tree_source": "site",
        })
        downloaded_total += section_done
        downloadable_total += section_downloadable
        expected_total += section_expected

    return sections, downloaded_total, downloadable_total, expected_total


def site_tree_activity_items(site_items, files):
    items = []
    downloaded_total = 0
    downloadable_total = 0
    expected_total = 0
    for index, site_item in enumerate(site_items if isinstance(site_items, list) else []):
        expected_total += 1
        matched_sources = matching_files(site_item, files)
        match_count = len(matched_sources)
        downloadable = bool(site_item.get("downloadable"))
        if downloadable:
            downloadable_total += 1
        expected_files = int(site_item.get("download_file_count") or 0)
        if downloadable and expected_files and match_count >= expected_files:
            status = "downloaded"
            downloaded_total += 1
        elif downloadable and match_count:
            status = "partial"
        elif downloadable:
            status = "missing"
        else:
            status = "not-downloadable"
        items.append({
            "kind": site_item.get("type") or "item",
            "text": site_item.get("title") or display_slug(site_item.get("slug", "")) or f"Item {index + 1}",
            "slug": site_item.get("slug", ""),
            "status": status,
            "downloadable": downloadable,
            "downloaded_count": match_count,
            "expected_count": expected_files,
            "sources": matched_sources,
            "reason": item_not_downloadable_reason(site_item),
        })
    return items, downloaded_total, downloadable_total, expected_total


def item_not_downloadable_reason(site_item):
    reason = str(site_item.get("reason", "") or "")
    if site_item.get("downloadable"):
        return reason

    typename = str(site_item.get("type", ""))
    generic_reasons = {
        "",
        "This Coursera item type is not supported by the downloader yet.",
        "Staff-graded assignments are not downloadable through this downloader.",
    }
    if reason and reason not in generic_reasons:
        return reason
    if typename == "staffGraded":
        return "Staff-graded assignments are interactive Coursera submission items and do not expose a downloadable file URL."
    if typename in {"ungradedAssignment", "discussionPrompt"}:
        return "This is an interactive Coursera activity. It can be completed in the browser, but Coursera does not expose it as a downloadable file."
    if typename in {"ungradedWidget", "coach"}:
        return "This is an embedded Coursera plugin/lab item. It runs in the browser and does not expose a downloadable file URL."
    return reason


def matching_files(site_item, files):
    slug = canonical_folder_slug(site_item.get("slug", ""))
    if not slug:
        return []
    needle = comparable_slug(slug)
    matches = []
    for item in files:
        text = str(item.get("text", ""))
        stem = re.sub(r"\.[^.\\/]+$", "", text)
        if needle and needle in comparable_slug(stem):
            matches.append({
                "text": text,
                "path": item.get("path", ""),
                "kind": item.get("kind", "file"),
            })
    return sorted(matches, key=source_sort_key)


def source_sort_key(source):
    suffix = Path(str(source.get("path") or source.get("text", ""))).suffix.lower()
    priority = {
        ".mp4": 0,
        ".mkv": 0,
        ".webm": 0,
        ".html": 1,
        ".pdf": 2,
        ".srt": 3,
        ".vtt": 3,
        ".txt": 4,
    }
    return (priority.get(suffix, 9), str(source.get("text", "")))


def comparable_slug(value):
    return re.sub(r"[^a-z0-9]+", "-", canonical_folder_slug(value).lower()).strip("-")


def file_activity_item(file_path, relative_root=None):
    name = file_path.name if relative_root is None else str(file_path.relative_to(relative_root))
    return {
        "kind": file_kind(file_path),
        "text": name,
        "path": str(file_path),
        "size": file_path.stat().st_size if file_path.exists() else 0,
    }


def file_kind(file_path):
    suffix = file_path.suffix.lower()
    if suffix in {".mp4", ".mkv", ".webm"}:
        return "video"
    if suffix in {".srt", ".vtt", ".txt"}:
        return "text"
    if suffix in {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".zip"}:
        return "resource"
    return "file"


def expected_activity_modules(expected):
    return [
        expected_activity_module(module_slug, module_expected)
        for module_slug, module_expected in expected.get("modules", {}).items()
    ]


def expected_activity_module(module_slug, module_expected):
    return {
        "title": display_slug(module_slug),
        "slug": module_slug,
        "sections": [
            {
                "title": display_slug(section_slug),
                "slug": section_slug,
                "items": [],
                "file_count": 0,
                "expected_count": section_expected,
            }
            for section_slug, section_expected in module_expected.get("sections", {}).items()
        ],
        "file_count": 0,
        "expected_count": module_expected.get("total", 0),
    }


def load_expected_activity_counts(course_slug, selected_folder):
    syllabus_file = find_syllabus_file(course_slug, selected_folder)
    if not syllabus_file:
        tree_debug("expected counts: slug=%s selected=%s syllabus=missing", course_slug, selected_folder)
        return {"total": 0, "modules": {}}

    try:
        data = json.loads(syllabus_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logging.debug("Could not read syllabus counts for %s: %s", course_slug, exc)
        tree_debug("expected counts: slug=%s syllabus=%s read_error=%s", course_slug, syllabus_file, exc)
        return {"total": 0, "modules": {}}

    modules = {}
    total = 0
    for module in data if isinstance(data, list) else []:
        if not isinstance(module, list) or len(module) < 2:
            continue
        module_slug = str(module[0])
        module_total = 0
        sections = {}
        for section in module[1] if isinstance(module[1], list) else []:
            if not isinstance(section, list) or len(section) < 2:
                continue
            section_slug = str(section[0])
            section_total = count_syllabus_resources(section[1])
            sections[section_slug] = section_total
            module_total += section_total
        modules[module_slug] = {
            "total": module_total,
            "sections": sections,
        }
        total += module_total

    tree_debug(
        "expected counts: slug=%s syllabus=%s modules=%s total=%s",
        course_slug,
        syllabus_file,
        len(modules),
        total,
    )
    return {"total": total, "modules": modules}


def tree_debug(message, *args):
    if TREE_DEBUG:
        logging.info("[tree-debug] " + message, *args)


def summarize_activity_groups(groups):
    summary = []
    for group in groups:
        modules = group.get("modules", []) if isinstance(group, dict) else []
        sections = sum(len(module.get("sections", [])) for module in modules if isinstance(module, dict))
        summary.append(
            {
                "slug": group.get("slug", ""),
                "modules": len(modules),
                "sections": sections,
                "files": group.get("file_count", 0),
                "expected": group.get("expected_count", 0),
                "status": group.get("status", ""),
            }
        )
    return summary


def count_syllabus_resources(value):
    if isinstance(value, dict):
        return sum(len(item) for item in value.values() if isinstance(item, list))
    if isinstance(value, list):
        return sum(count_syllabus_resources(item) for item in value)
    return 0


def find_syllabus_file(course_slug, selected_folder):
    candidates = [
        Path(selected_folder).expanduser() / f"{course_slug}-syllabus-parsed.json",
        ROOT / f"{course_slug}-syllabus-parsed.json",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def find_site_tree_files(class_name, is_multi_course_program, selected_folder):
    selected_folder = Path(selected_folder).expanduser()
    course_slugs = resolve_program_course_slugs(class_name, selected_folder) if is_multi_course_program else [class_name]
    found = []
    for course_slug in course_slugs:
        for candidate in [
            selected_folder / f"{course_slug}-site-tree.json",
            ROOT / f"{course_slug}-site-tree.json",
        ]:
            if candidate.exists() and candidate.is_file() and candidate not in found:
                found.append(candidate)
    return found


def canonical_folder_slug(value):
    return re.sub(r"^\d+[_\-\s]*", "", str(value))


def display_slug(value):
    text = re.sub(r"^\d+[_\-\s]*", "", str(value))
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or str(value)


def numbered_course_title(index, total, slug):
    title = display_slug(slug)
    if index and total > 1:
        return f"{index}. {title}"
    return title


def ordered_course_folder_name(index, slug):
    return f"{index:02d}_{slug}"


def is_ordered_course_folder_name(folder_name, slug):
    return re.fullmatch(r"\d+[_\-\s]*" + re.escape(slug), str(folder_name)) is not None


def natural_sort_key(value):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value))]


def get_program_course_slugs_from_page(program_slug):
    url = f"https://www.coursera.org/professional-certificates/{program_slug}"
    try:
        response = requests.get(url, timeout=10)
        logging.debug("Program page lookup %s returned HTTP %s.", url, response.status_code)
        response.raise_for_status()
    except requests.RequestException as exc:
        logging.debug("Program page lookup failed for %s: %s", program_slug, exc)
        return []

    page = html.unescape(response.text)
    start = page.find("Professional Certificate")
    end = page.find("Earn a career certificate", start)
    if start != -1 and end != -1:
        page = page[start:end]

    by_number = {}
    pattern = re.compile(
        r'href="[^"]*/learn/([^"?/#]+)[^"]*"(?:(?!href=).){0,6000}?Course\s+(\d+)',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(page):
        slug = match.group(1).strip()
        number = int(match.group(2))
        if slug and number not in by_number:
            by_number[number] = slug

    return [by_number[number] for number in sorted(by_number)]


def get_program_course_slugs(program_slug):
    if program_slug in PROGRAM_COURSE_CACHE:
        return PROGRAM_COURSE_CACHE[program_slug]

    endpoint = OPENCOURSE_ONDEMAND_SPECIALIZATIONS_V1.format(class_name=program_slug)
    slugs = []
    try:
        response = requests.get(endpoint, timeout=10)
        logging.debug("Program course lookup %s returned HTTP %s.", endpoint, response.status_code)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logging.debug("Program course lookup failed for %s: %s", program_slug, exc)
        PROGRAM_COURSE_CACHE[program_slug] = slugs
        return slugs

    linked = data.get("linked", {}) if isinstance(data, dict) else {}
    courses = linked.get("courses.v1", []) if isinstance(linked, dict) else []
    by_id = {}
    for course in courses:
        if isinstance(course, dict):
            course_id = course.get("id")
            slug = course.get("slug")
            if isinstance(course_id, str) and isinstance(slug, str) and slug.strip():
                by_id[course_id] = slug.strip()

    ordered_ids = []
    for element in data.get("elements", []) if isinstance(data, dict) else []:
        if not isinstance(element, dict):
            continue
        for course_id in element.get("courseIds", []) or []:
            if isinstance(course_id, str) and course_id not in ordered_ids:
                ordered_ids.append(course_id)
        for course_id in element.get("interchangeableCourseIds", []) or []:
            if isinstance(course_id, str) and course_id not in ordered_ids:
                ordered_ids.append(course_id)

    for course_id in ordered_ids:
        slug = by_id.get(course_id)
        if slug and slug not in slugs:
            slugs.append(slug)

    for course in courses:
        if isinstance(course, dict):
            slug = course.get("slug")
            if isinstance(slug, str) and slug.strip() and slug.strip() not in slugs:
                slugs.append(slug.strip())

    PROGRAM_COURSE_CACHE[program_slug] = slugs
    return slugs


def find_existing_course_folder(class_name, selected_folder):
    candidates = []
    selected_folder = Path(selected_folder).expanduser()
    candidates.append(selected_folder / class_name)
    candidates.extend(find_numbered_course_folder_candidates(selected_folder, class_name))

    try:
        db = SimpleDB("data.bin")
        saved_path = db.get_full_db().get("argdict", {}).get("path", "")
        if saved_path:
            saved_root = Path(saved_path).expanduser()
            candidates.append(saved_root / class_name)
            candidates.extend(find_numbered_course_folder_candidates(saved_root, class_name))
    except Exception:
        pass

    home = Path.home()
    for root in [
        default_download_folder(),
        home / "Downloads" / "Coursera",
        home / "OneDrive" / "Desktop",
    ]:
        candidates.append(root / class_name)
        candidates.extend(find_numbered_course_folder_candidates(root, class_name))

    if os.name == "nt":
        for drive in string.ascii_uppercase:
            root = Path(f"{drive}:\\")
            if root.exists():
                for candidate_root in [
                    root,
                    root / "My Drive",
                    root / "My Drive" / "Project Manager Courses",
                ]:
                    candidates.append(candidate_root / class_name)
                    candidates.extend(find_numbered_course_folder_candidates(candidate_root, class_name))

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate

    return None


def find_numbered_course_folder_candidates(root, class_name):
    root = Path(root).expanduser()
    if not root.exists() or not root.is_dir():
        return []
    return [
        entry for entry in root.iterdir()
        if entry.is_dir() and is_ordered_course_folder_name(entry.name, class_name)
    ]


def load_config():
    db = SimpleDB("data.bin")
    data = db.get_full_db()
    argdict = data.get("argdict", {})
    language_code = argdict.get("sl", "en")
    language_name = next(
        (name for name, code in general.LANG_NAME_TO_CODE_MAPPING.items() if code == language_code),
        "English",
    )

    saved_path = argdict.get("path", "")

    return {
        "version": __version__,
        "languages": sorted(general.LANG_NAME_TO_CODE_MAPPING.keys()),
        "auth": {
            "mode": "auto",
            "browsers": ["firefox", "chrome", "edge", "brave"],
        },
        "defaults": {
            "course": argdict.get("classname", ""),
            "path": saved_path or str(default_download_folder()),
            "video_resolution": argdict.get("video_resolution", "720p"),
            "subtitle_language": language_name,
        },
    }


def save_config(config):
    db = SimpleDB("data.bin")
    db.upsert("browser", config["browser"])
    for key, value in config["argdict"].items():
        db.upsert(f"argdict.{key}", value)


class WebHandler(BaseHTTPRequestHandler):
    server_version = "CourseraDownloaderWeb/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        logging.debug("GET %s", parsed.path)
        if parsed.path == "/api/config":
            self.send_json(load_config())
            return
        if parsed.path == "/api/status":
            self.send_json(JOB.snapshot())
            return
        self.serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        logging.debug("POST %s", parsed.path)
        if not self.origin_is_allowed():
            self.send_json({"error": "Cross-origin request rejected."}, status=403)
            return
        try:
            payload = self.read_json()
            if parsed.path == "/api/download":
                self.start_download(payload, resume=False)
                return
            if parsed.path == "/api/resume":
                self.start_download(payload, resume=True)
                return
            if parsed.path == "/api/retry":
                self.retry_download()
                return
            if parsed.path == "/api/download-state":
                self.download_state(payload)
                return
            if parsed.path == "/api/download-item":
                self.start_download(payload, resume=False, item=payload.get("item", {}))
                return
            if parsed.path == "/api/resume-item":
                self.start_download(payload, resume=True, item=payload.get("item", {}))
                return
            if parsed.path == "/api/pause":
                self.pause_download()
                return
            if parsed.path == "/api/resume-job":
                self.resume_download()
                return
            if parsed.path == "/api/cancel":
                self.cancel_download()
                return
            if parsed.path == "/api/select-folder":
                self.select_folder(payload)
                return
            if parsed.path == "/api/open-path":
                self.open_path(payload)
                return
            if parsed.path == "/api/open-login":
                self.open_login()
                return
            if parsed.path == "/api/check-auth":
                self.check_auth()
                return
            self.send_json({"error": "Not found"}, status=404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)

    def start_download(self, payload, resume, item=None):
        remembered_payload = dict(payload)
        if item:
            remembered_payload["item"] = dict(item)
        JOB.remember_request(remembered_payload, resume)
        command, config = build_download_command(payload, resume=resume, item=item)
        save_config(config)
        JOB.start(command)
        self.send_json({"ok": True, "status": JOB.snapshot()})

    def retry_download(self):
        payload, resume = JOB.retry_request()
        item = payload.get("item", {})
        command, config = build_download_command(payload, resume=resume, item=item)
        save_config(config)
        JOB.start(command)
        self.send_json({"ok": True, "status": JOB.snapshot(), "retry_resume": resume})

    def download_state(self, payload):
        self.send_json(get_download_state(payload))

    def pause_download(self):
        JOB.pause()
        self.send_json({"ok": True, "status": JOB.snapshot()})

    def resume_download(self):
        JOB.resume()
        self.send_json({"ok": True, "status": JOB.snapshot()})

    def cancel_download(self):
        JOB.cancel()
        self.send_json({"ok": True, "status": JOB.snapshot()})

    def select_folder(self, payload):
        selected = choose_download_folder(payload.get("path", ""))
        if selected:
            self.send_json({"path": selected})
        else:
            self.send_json({"path": payload.get("path", "")})

    def open_path(self, payload):
        raw_path = str(payload.get("path", "")).strip()
        if not raw_path:
            raise ValueError("No path was provided.")
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise ValueError("Path does not exist.")
        if not is_path_within_allowed_roots(path):
            raise ValueError("Path is outside the allowed download folders.")

        if os.name == "nt":
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if path.is_file() else str(path), str(path)] if path.is_file() else ["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])
        self.send_json({"ok": True})

    def open_login(self):
        webbrowser.open("https://www.coursera.org/?authMode=login")
        JOB.add_log("Opened Coursera login page.")
        self.send_json({
            "ok": True,
            "message": "Coursera login opened in your browser. After logging in, click Check Auth.",
        })

    def check_auth(self):
        try:
            status = detect_auth_status_cached()
            JOB.add_log(status["message"])
            self.send_json(status)
        except Exception as exc:
            message = friendly_error_message(exc)
            logging.debug("Coursera auth check failed: %s", message)
            self.send_json({
                "authenticated": False,
                "message": message,
            }, status=200)

    def origin_is_allowed(self):
        host_header = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        referer = self.headers.get("Referer", "")
        if origin:
            try:
                origin_host = urlparse(origin).netloc
            except ValueError:
                return False
            return origin_host == host_header
        if referer:
            try:
                referer_host = urlparse(referer).netloc
            except ValueError:
                return False
            return referer_host == host_header
        return True

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            logging.debug("Client disconnected before response could be sent.")

    def serve_static(self, request_path):
        relative_path = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        file_path = (WEB_ROOT / relative_path).resolve()

        if WEB_ROOT not in file_path.parents and file_path != WEB_ROOT:
            self.send_error(403)
            return

        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return

        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logging.debug("%s - %s", self.address_string(), format % args)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the Coursera Downloader web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=7500, type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--startup-delay", default=0, type=float)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if args.startup_delay > 0:
        time.sleep(args.startup_delay)

    disable_console_quick_edit()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(message)s")
    server = ThreadingHTTPServer((args.host, args.port), WebHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Coursera Downloader web app running at {url}")
    if not args.no_browser:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
