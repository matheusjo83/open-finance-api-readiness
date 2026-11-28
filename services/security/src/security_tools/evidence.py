"""Evidence for the conformance suite's review placeholders, so a headless run can finish.

Where the authorisation server may answer with an error page instead of redirecting back (PAR
7.3: an invalid, reused or expired request_uri; a request without a request object), the suite
waits for a screenshot of that page and marks the module REVIEW. Its scripted browser (HtmlUnit)
takes no screenshots, but it logs the HTTP response it got. Once the browser has finished its
tasks on an error page (4XX or 5XX; the plan's automation already waited for the error text), this uploads a PNG
of that response's status and visible text, saying what it is. The module stays REVIEW: the
image is evidence for a reviewer, not a verdict."""

import base64
import html
import io
import re
import threading
import time
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw, ImageFont

HEADER = ("Not a screenshot: the HTTP response the conformance suite's scripted browser (HtmlUnit) "
          "received, rendered as text by ofar security_tools.")
DONE_STATES = {"FINISHED", "INTERRUPTED"}


def at_callback(url: str) -> bool:
    """Whether the browser is back at the suite's redirect_uri (<suite>/test/a/<alias>/callback).
    The path only: the authorisation request carries the redirect_uri in its query."""
    return urlsplit(url).path.endswith("/callback")


def visible_text(page: str) -> str:
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    return re.sub(r"\s+", " ", text).strip()


def render(lines: list[str], width: int = 110) -> str:
    """A PNG data URI with the lines, wrapped at width characters."""
    wrapped = []
    for line in lines:
        wrapped += [line[i:i + width] for i in range(0, max(len(line), 1), width)]
    font = ImageFont.load_default()
    image = Image.new("RGB", (8 + 7 * width, 12 + 16 * len(wrapped)), "white")
    draw = ImageDraw.Draw(image)
    for n, line in enumerate(wrapped):
        draw.text((6, 6 + 16 * n), line, fill="black", font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def error_page(response: dict) -> bool:
    try:
        return int(response.get("response_status_code") or 0) >= 400
    except (TypeError, ValueError):
        return False


def pending(log: list[dict]) -> list[tuple[str, dict]]:
    """Placeholders not filled yet where the scripted browser stopped on an error page of the
    authorisation server, each with that page's HTTP response (the last one the browser got
    before stopping there).

    Not back at the suite's callback, where there is no error page to show; and not on a page
    the server answered with success, such as its login page: the browser is still on its way
    there (it logs in and goes on), and a placeholder filled then ends the module, so the
    callback that follows interrupts it."""
    filled = {e.get("upload") for e in log if e.get("img")}
    found = []
    for i, entry in enumerate(log):
        placeholder = entry.get("upload")
        if entry.get("result") != "REVIEW" or not placeholder or placeholder in filled or entry.get("img"):
            continue
        after = log[i + 1:]
        for j, e in enumerate(after):
            if (e.get("src") != "WebRunner" or e.get("msg") != "Completed processing of webpage"
                    or at_callback(str(e.get("url", "")))):
                continue
            responses = [r for r in after[:j] if r.get("msg") == "Scripted browser HTTP response"]
            if responses and error_page(responses[-1]):
                found.append((placeholder, responses[-1]))
                break
    return found


class Uploader(threading.Thread):
    """Watches the running modules and fills their review placeholders; records what it did."""

    def __init__(self, api_base: str, *, interval: float = 3.0, verify=False):
        super().__init__(daemon=True)
        self.client = httpx.Client(base_url=api_base.rstrip("/") + "/api/", verify=verify, timeout=30)
        self.interval = interval
        self.stopping = threading.Event()
        self.done: set[str] = set()
        self.uploads: list[dict] = []

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.sweep()
            except httpx.HTTPError as e:
                print(f"evidence: {e}")
            self.stopping.wait(self.interval)

    def sweep(self) -> None:
        for test in self.client.get("runner/running").raise_for_status().json():
            if test in self.done:
                continue
            info = self.client.get(f"info/{test}").raise_for_status().json()
            if info.get("status") in DONE_STATES:
                self.done.add(test)
                continue
            for placeholder, response in pending(self.client.get(f"log/{test}").raise_for_status().json()):
                page = str(response.get("response_content") or "")
                status = f"{response.get('response_status_code')} {response.get('response_status_text') or ''}".strip()
                image = render([HEADER, f"module {info.get('testName')} ({test})", f"HTTP {status}", "",
                                visible_text(page)[:2000]])
                self.client.post(f"log/{test}/images/{placeholder}", content=image,
                                 headers={"Content-Type": "text/plain"}).raise_for_status()
                self.uploads.append({"module": info.get("testName"), "instance": test, "placeholder": placeholder,
                                     "status": status, "text": visible_text(page)[:300], "at": time.time()})
                print(f"evidence: filled {placeholder} of {info.get('testName')} ({status})")

    def stop(self) -> None:
        self.stopping.set()
        self.join(timeout=10)
        self.client.close()
