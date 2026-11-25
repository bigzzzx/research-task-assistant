"""Drive the local app with Playwright and record the 3–5 minute demo video.

Run the backend first (with a real DEEPSEEK_API_KEY and a fresh data directory),
then execute this script. It records the browser viewport to
``recordings/raw/*.webm`` and writes ``recordings/timeline.json`` with the start
time of every scene so narration can be aligned afterwards.

    .venv\\Scripts\\python.exe -m demo.record_demo --user demo --password demo1234

The overlay bar and cursor highlight are injected into the page only for the
recording; they are not part of the application.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UPLOAD = ROOT / "demo" / "my_sequences.fasta"

OVERLAY = """
() => {
  if (document.getElementById('demo-bar')) return;
  const style = document.createElement('style');
  style.id = 'demo-style';
  style.textContent = `
    body { padding-bottom: 72px !important; box-sizing: border-box; background: #0b1220; }
    .workspace { height: calc(100dvh - 72px) !important; }
    .account-page { min-height: calc(100dvh - 72px) !important; }
    #demo-bar {
      position: fixed; left: 0; right: 0; bottom: 0; height: 72px; z-index: 2147483647;
      display: flex; align-items: center; gap: 16px; padding: 0 26px; box-sizing: border-box;
      background: linear-gradient(90deg, rgba(9,20,38,.97), rgba(13,32,55,.97));
      border-top: 1px solid rgba(94,234,212,.45); color: #f1f5f9;
      font: 16px/1.5 "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
      pointer-events: none; box-shadow: 0 -8px 24px rgba(0,0,0,.35);
    }
    #demo-step {
      flex: none; padding: 4px 10px; border-radius: 999px; font-size: 13px; font-weight: 700;
      letter-spacing: .08em; color: #042f2e; background: #5eead4;
    }
    #demo-text { font-size: 16.5px; }
    #demo-cursor {
      position: fixed; left: 0; top: 0; width: 20px; height: 20px; border-radius: 50%;
      z-index: 2147483646; pointer-events: none; margin: -10px 0 0 -10px;
      border: 2px solid rgba(94,234,212,.95); background: rgba(94,234,212,.28);
      box-shadow: 0 0 0 7px rgba(94,234,212,.12); transition: width .12s, height .12s;
    }
    #demo-cursor.press { width: 30px; height: 30px; margin: -15px 0 0 -15px;
      background: rgba(248,113,113,.55); border-color: rgba(248,113,113,.95); }
  `;
  document.head.appendChild(style);
  const bar = document.createElement('div');
  bar.id = 'demo-bar';
  bar.innerHTML = '<span id="demo-step"></span><span id="demo-text"></span>';
  document.body.appendChild(bar);
  const dot = document.createElement('div');
  dot.id = 'demo-cursor';
  document.body.appendChild(dot);
  window.addEventListener('mousemove', event => {
    dot.style.left = event.clientX + 'px';
    dot.style.top = event.clientY + 'px';
  }, true);
  window.addEventListener('mousedown', () => dot.classList.add('press'), true);
  window.addEventListener('mouseup', () => dot.classList.remove('press'), true);
}
"""


class Demo:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url
        self.timeline: list[dict] = []
        self.started = 0.0
        self.pace = 1.0
        self.caption = ("", "")

    # -- narration overlay -------------------------------------------------
    def install_overlay(self) -> None:
        self.page.evaluate(OVERLAY)
        if self.caption[0]:
            self.page.evaluate(
                """([step, text]) => {
                    document.getElementById('demo-step').textContent = step;
                    document.getElementById('demo-text').textContent = text;
                }""",
                list(self.caption),
            )

    def say(self, step: str, text: str, *, gap: float = 1.2) -> None:
        self.caption = (step, text)
        self.page.evaluate(
            """([step, text]) => {
                document.getElementById('demo-step').textContent = step;
                document.getElementById('demo-text').textContent = text;
            }""",
            [step, text],
        )
        self.timeline.append({"step": step, "text": text, "at": round(time.monotonic() - self.started, 2)})
        print(f"[{time.monotonic() - self.started:6.1f}s] {step} {text}", flush=True)
        self.page.wait_for_timeout(int(gap * self.pace * 1000))

    def reveal(self, selector: str, *, last: bool = True, pause: float = 1.0) -> None:
        """Center a chat element; Playwright only guarantees that it is technically visible."""
        locator = self.page.locator(selector)
        target = locator.last if last else locator.first
        if not target.count():
            return
        target.evaluate("element => element.scrollIntoView({block: 'center'})")
        self.page.wait_for_timeout(int(pause * 1000))

    # -- interaction -------------------------------------------------------
    def click(self, selector: str, *, pause: float = 0.5) -> None:
        locator = self.page.locator(selector).first
        locator.wait_for(state="visible", timeout=30_000)
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if box:
            self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=12)
            self.page.wait_for_timeout(220)
        locator.click()
        self.page.wait_for_timeout(int(pause * 1000))

    def type_into(self, selector: str, text: str) -> None:
        locator = self.page.locator(selector).first
        locator.wait_for(state="visible", timeout=20_000)
        locator.click()
        locator.press_sequentially(text, delay=22)
        self.page.wait_for_timeout(350)

    def send(self, text: str) -> None:
        self.type_into("#chat-input", text)
        self.click("button.send", pause=0.2)

    def open_recent_cards(self, count: int, pause: float = 0.9) -> None:
        """Expand the newest tool cards so parameters and results are visible."""
        cards = self.page.locator("details.tool")
        total = cards.count()
        for index in range(max(0, total - count), total):
            card = cards.nth(index)
            card.scroll_into_view_if_needed()
            if not card.evaluate("element => element.open"):
                card.locator("summary").click()
                self.page.wait_for_timeout(int(pause * 1000))

    def wait_text(self, needle: str, timeout: float = 180) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.page.locator("body").inner_text():
                return
            self.page.wait_for_timeout(400)
        raise PlaywrightTimeout(f"未在 {timeout:.0f}s 内看到文本：{needle}")

    def counts(self) -> tuple[int, int]:
        return self.page.locator("details.tool").count(), self.page.locator(".job-card").count()

    def wait_progress(self, before: tuple[int, int], timeout: float = 120) -> None:
        """Wait until the turn produced a new tool card or a new task card."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tools, jobs = self.counts()
            if tools > before[0] or jobs > before[1]:
                return
            self.page.wait_for_timeout(400)
        raise PlaywrightTimeout("这一轮没有出现新的工具调用或任务卡")

    def ask(self, text: str) -> None:
        """Send a message and wait for the whole turn: tool cards, task, explanation."""
        before = self.counts()
        self.send(text)
        self.wait_progress(before)
        self.wait_settled()

    def wait_settled(self, timeout: float = 240, stable: float = 2.5) -> None:
        """Wait for streaming and tasks to end, and for the page to stay unchanged."""
        deadline = time.monotonic() + timeout
        signature = None
        since = time.monotonic()
        while time.monotonic() < deadline:
            busy, current = self.page.evaluate(
                """() => [
                    document.querySelector('[aria-label="正在输出"]') !== null
                      || [...document.querySelectorAll('.job-card .state-pill')]
                           .some(pill => /排队中|运行中/.test(pill.textContent)),
                    document.body.innerText.length + ':' + document.querySelectorAll('details.tool').length
                ]"""
            )
            now = time.monotonic()
            if busy or current != signature:
                signature = current
                since = now
            elif now - since >= stable:
                self.page.wait_for_timeout(300)
                return
            self.page.wait_for_timeout(400)
        raise PlaywrightTimeout("任务或回答长时间未结束")


def ensure_account(base_url: str, user: str, password: str) -> None:
    payload = json.dumps({"username": user, "password": password}).encode()
    for path, expected in (("/api/auth/register", 201), ("/api/auth/login", 200)):
        request = urllib.request.Request(
            base_url + path, data=payload, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status == expected:
                    print(f"账号已就绪（{path}）")
                    return
        except urllib.error.HTTPError as error:
            if error.code in (401, 409, 422):
                continue
            raise


def record(args: argparse.Namespace) -> Path:
    raw = Path(args.video_dir)
    raw.mkdir(parents=True, exist_ok=True)
    for stale in raw.glob("*.webm"):
        stale.unlink()

    ensure_account(args.base_url, args.user, args.password)
    demo: Demo
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": 1440, "height": 960},
            record_video_dir=str(raw),
            record_video_size={"width": 1440, "height": 960},
            locale="zh-CN",
            device_scale_factor=1,
        )
        page = context.new_page()
        demo = Demo(page, args.base_url)
        demo.pace = args.pace
        demo.started = time.monotonic()
        page.on("load", lambda _: demo.install_overlay())

        page.goto(args.base_url, wait_until="networkidle")
        demo.install_overlay()

        video = page.video
        saved: Path | None = None
        try:
            # 1) 登录
            demo.say("00 开始", "本机运行的研究助手：用中文对话提交 FASTA 分析，工具调用全程可见。", gap=2.0)
            demo.type_into("#account-name", args.user)
            demo.type_into("#account-password", args.password)
            demo.click("button.primary-button")
            page.wait_for_selector(".workspace", timeout=30_000)
            page.wait_for_timeout(1200)
            demo.say("01 登录", "账号彼此隔离：会话、上传文件、任务和工具开关都按账号保存。", gap=2.5)

            # 2) sample1：一次完整的成功任务
            demo.say("02 新建会话", "新建会话，直接说出想做的分析。左侧是历史会话列表，右侧是样例文件和任务状态。", gap=2.0)
            before = demo.counts()
            demo.send("帮我统计一下 sample1 里序列的 GC 含量。")
            demo.say("03 提交任务", "助手自己调用 submit_job（seq_stats + sample1.fasta）拿到任务 ID，再查状态、读日志。", gap=0.2)
            demo.wait_progress(before)
            demo.wait_settled()
            demo.say("04 自动读取结果", "任务结束后助手读取 stdout，用自然语言解释；右侧任务卡可以下载完整日志。", gap=1.2)
            demo.open_recent_cards(3)
            demo.reveal(".assistant-message")
            demo.say("05 工具轨迹", "每次调用都有参数和返回值：seq1 为 45 nt、GC 48.89%；seq2 为 47 nt、GC 61.70%。", gap=3.0)
            demo.reveal(".result-wrap")

            # 3) sample3：失败处理
            demo.say("06 失败任务", "接着说“再对 sample3 做一次”。助手沿用序列统计类型，任务失败后主动去读错误日志。", gap=1.5)
            before = demo.counts()
            demo.send("再对 sample3 做一次。")
            demo.wait_progress(before)
            demo.wait_text("缺少", timeout=180)
            demo.wait_settled()
            demo.say("07 诊断", "日志显示：第 1 条序列缺少以“>”开头的标题行；第 2 行第 7–9 列和第 16–18 列含非法字符。", gap=1.2)
            demo.open_recent_cards(2)
            demo.reveal(".input-recovery", last=False, pause=1.2)
            demo.say("08 待补充文件", "「待补充文件」里列出两处问题；因为 sample3 还含非法字符，系统只接受人工核对后的修正版。", gap=1.2)
            repair_button = page.locator("button:has-text('预览格式修复')")
            if repair_button.count():
                repair_button.first.click()
                page.wait_for_timeout(2000)
                page.locator(".input-repair-preview").first.scroll_into_view_if_needed()
                page.wait_for_timeout(2000)
                before = demo.counts()
                page.locator("button:has-text('确认生成副本并继续')").first.click()
                demo.wait_progress(before)
                demo.wait_settled()
                demo.say("09 修复后成功", "修正后的副本重新提交后正常跑完：失败任务的原日志保留，新任务继续给出解释。", gap=3.0)
            else:
                demo.say("09 修正建议", "自动修复只对“仅缺标题行”的文件开放，避免把非法字符一起放过；这里需要人工修正。", gap=3.0)

            # 4) sample2：另一种分析
            demo.say("10 另一种分析", "换一种分析：找出 sample2 中每条序列最长的开放阅读框（ORF）。", gap=1.5)
            before = demo.counts()
            demo.send("找出 sample2 中每条序列最长的 ORF。")
            demo.wait_progress(before)
            demo.wait_text("gene_A", timeout=180)
            demo.wait_settled()
            demo.reveal(".assistant-message")
            demo.say("11 ORF 结果", "gene_A 为 6–83（78 nt），gene_B 为 7–21（15 nt）；模型只负责解释，坐标来自脚本输出。", gap=3.0)
            demo.reveal(".result-wrap")

            # 5) 上传自己的文件
            demo.say("12 上传文件", "上传自己的 FASTA，文件进入当前账号的「我的文件」，可以被任一会话复用。", gap=1.5)
            with page.expect_file_chooser() as chooser_info:
                demo.click("button.attach", pause=0.2)
            chooser_info.value.set_files(str(Path(args.upload)))
            demo.wait_text(Path(args.upload).name, timeout=60)
            page.wait_for_timeout(1500)
            demo.say("13 分析上传文件", "直接用自然语言指定上传的文件，走的还是同一套三个工具。", gap=1.2)
            before = demo.counts()
            demo.send("用 seq_stats 分析我上传的 my_sequences.fasta。")
            demo.wait_progress(before)
            demo.wait_text("demo_gamma", timeout=180)
            demo.wait_settled()
            demo.reveal(".assistant-message")
            demo.say("14 上传结果", "上传文件和内置样例没有区别：提交、等待、读日志、解释，结果同样落进任务记录。", gap=3.0)
            demo.reveal(".result-wrap")

            # 6) 刷新与历史
            demo.say("15 刷新恢复", "刷新页面：会话、消息、工具记录和任务状态都从 SQLite 恢复，不依赖浏览器缓存。", gap=1.0)
            page.reload(wait_until="networkidle")
            demo.install_overlay()
            page.wait_for_timeout(2800)
            demo.say("16 历史会话", "左侧可以切换历史会话；刚上传的文件也还在。", gap=2.5)
            page.locator(".session").first.click()
            page.wait_for_timeout(2200)

            # 7) 任务管理：执行记录与完整日志
            demo.say("17 任务管理", "任务管理按账号列出每次执行：状态、退出码、标准输出和错误日志都能回看。", gap=1.0)
            page.goto(args.base_url + "/#tasks", wait_until="networkidle")
            demo.install_overlay()
            page.wait_for_timeout(3000)
            demo.say("18 任务详情", "点开一次运行就能看到原始日志，失败原因和行列位置可以人工核对，不只看模型的转述。", gap=1.0)
            detail = page.locator("tr:has-text('sample3.fasta') button:has-text('查看日志')").first
            if not detail.count():
                detail = page.locator("button:has-text('查看日志')").first
            if detail.count():
                detail.click()
                page.wait_for_timeout(1500)
                stderr_tab = page.locator(".task-detail button:has-text('错误日志')").first
                if stderr_tab.count():
                    stderr_tab.click()
                page.wait_for_timeout(3500)

            # 8) 工具列表：三个工具就是本机 MCP 服务里的工具
            demo.say("19 工具列表", "「扩展管理 → 工具列表」显示三个内置工具由本机 MCP 服务提供，可按账号停用，也能接入外部 MCP。", gap=1.0)
            page.goto(args.base_url + "/#tools", wait_until="networkidle")
            demo.install_overlay()
            page.wait_for_timeout(2000)
            tools_tab = page.locator("main.management button:has-text('工具列表')").first
            if tools_tab.count():
                tools_tab.click()
            page.wait_for_timeout(3000)

            # 9) 收尾
            page.goto(args.base_url, wait_until="networkidle")
            demo.install_overlay()
            demo.say("20 架构", "React + FastAPI 单进程；手写 ReAct 循环；三个工具经 MCP 暴露；任务在独立进程执行，数据存 SQLite。", gap=3.0)
            demo.say("21 交付", "源码、测试（66 个用例）、Docker Compose 与 README 都在仓库里。演示结束，谢谢观看。", gap=2.5)
        finally:
            context.close()
            if video:
                saved = Path(video.path())
            browser.close()



    timeline = {"scenes": demo.timeline, "duration": round(time.monotonic() - demo.started, 2)}
    Path("recordings").mkdir(exist_ok=True)
    Path("recordings/timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"录制完成：{saved}，时长约 {timeline['duration']:.1f}s")
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8767")
    parser.add_argument("--video-dir", default="recordings/raw")
    parser.add_argument("--user", default="demo")
    parser.add_argument("--password", default="demo1234")
    parser.add_argument("--upload", default=str(DEFAULT_UPLOAD))
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--pace", type=float, default=1.0, help="字幕停顿倍率，用来把成片控制到 3–5 分钟")
    args = parser.parse_args()
    try:
        record(args)
    except PlaywrightTimeout as error:
        print(f"录制中止：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
