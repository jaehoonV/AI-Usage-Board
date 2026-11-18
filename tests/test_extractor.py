"""Run resources/extractor.js under Node against small stand-in pages.

The stand-ins follow the live usage pages as of 2026-11:

* claude.ai - one <section> per window, an <h3> naming it ("주간 한도") and a
  role="meter" named after what it covers ("모든 모델");
* gemini - one <li data-testid="…-usage"> per window, lines in either order.
"""
import json
import shutil
import subprocess

import pytest

from ai_usage_board.config import RESOURCE_DIR

NODE = shutil.which("node")

DOM = r"""
class Node {
  constructor(tag, attrs = {}, children = [], text = "") {
    this.tag = tag; this.attrs = attrs; this.children = children; this.text = text;
    this.parentElement = null;
    children.forEach((child) => { child.parentElement = this; });
  }
  get innerText() {
    if (this.text) return this.text;
    return this.children.map((child) => child.innerText).filter(Boolean).join("\n");
  }
  get textContent() { return this.innerText; }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  getClientRects() { return this.attrs.hidden ? [] : [1]; }
  matchesOne(selector) {
    const m = selector.trim().match(/^([a-z0-9]*)(?:\[([\w-]+)(\$?=)"([^"]*)"\])?$/);
    if (!m) throw new Error("unsupported selector " + selector);
    const [, tag, name, op, value] = m;
    if (tag && tag !== this.tag) return false;
    if (!name) return true;
    const actual = this.attrs[name];
    if (actual === undefined) return false;
    return op === "=" ? actual === value : actual.endsWith(value);
  }
  matches(selector) { return selector.split(",").some((part) => this.matchesOne(part)); }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => node.children.forEach((child) => {
      if (child.matches(selector)) found.push(child);
      walk(child);
    });
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const el = (tag, attrs, ...children) => new Node(tag, attrs, children);
const txt = (text, tag = "span", attrs = {}) => new Node(tag, attrs, [], text);
const page = (body) => {
  const ids = {};
  const index = (node) => { if (node.attrs.id) ids[node.attrs.id] = node; node.children.forEach(index); };
  index(body);
  return {
    querySelector: (s) => body.querySelector(s),
    querySelectorAll: (s) => body.querySelectorAll(s),
    getElementById: (id) => ids[id] || null,
  };
};
globalThis.window = { location: {} };
"""

CLAUDE_PAGE = r"""
const usageRow = (id, name, reset, valuetext) => el("div", {},
  el("div", {}, txt(name, "p", { id }), txt(reset, "p")),
  el("div", {}, txt(valuetext, "p"), el("div", {}, el("div", { role: "meter", "aria-labelledby": id, "aria-valuetext": valuetext }))),
);
const doc = page(el("body", {},
  el("nav", {}, txt("최근 대화")),
  el("div", { role: "dialog" },
    el("section", {}, txt("사용 한도\n팀", "h3"), usageRow("s", "현재 세션", "오전 11:50에 초기화", "45% 사용됨")),
    el("section", {}, txt("주간 한도", "h3"), txt("사용량 제한에 대해 더 알아보기", "a"),
      el("div", {}, usageRow("w1", "모든 모델", "(금) 오전 6:00에 초기화", "61% 사용됨")),
      el("div", {}, usageRow("w2", "Sonnet만", "(금) 오전 6:00에 초기화", "12% 사용됨"))),
    el("section", {}, txt("파일 업로드", "h3"),
      el("div", {}, el("div", { role: "progressbar", "aria-label": "업로드", "aria-valuetext": "40% 사용됨" }))),
    el("section", {}, txt("주간 한도", "h3"),
      el("div", { "aria-hidden": "true" }, el("div", { role: "meter", "aria-label": "숨김", "aria-valuetext": "99% 사용됨" }))),
  ),
));
const where = { protocol: "https:", hostname: "claude.ai", pathname: "/new", hash: "#settings/usage" };
"""

GEMINI_PAGE = r"""
const card = (id, ...lines) => el("li", { "data-testid": id }, ...lines.map((line) => txt(line)));
const doc = page(el("body", {},
  el("main", {},
    txt("방금 업데이트됨", "p", { "data-testid": "usage-updated" }),
    el("ul", {},
      card("current-usage", "현재 사용량", "info", "0% 사용됨", "오후 2:27에 초기화"),
      card("weekly-usage", "주간 한도", "11월 25일 오전 7:27에 초기화", "30% 남음")),
    el("ul", { "data-testid": "usage-settings-group" },
      card("smart-model-selection-row", "자동 모델 선택", "50% 사용됨")),
  ),
));
const where = { protocol: "https:", hostname: "gemini.google.com", pathname: "/usage", hash: "" };
"""


def read_page(fixture: str):
    script = (
        DOM
        + fixture
        + (RESOURCE_DIR / "extractor.js").read_text(encoding="utf-8")
        + "\nconsole.log(JSON.stringify(AIUsageBoardReader.read(doc, where)));"
    )
    done = subprocess.run([NODE, "-e", script], capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return json.loads(done.stdout.decode("utf-8"))


def rows(result):
    return [(w["label"], w["used"], w["resetText"]) for w in result["windows"]]


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_claude_meters_are_named_by_their_section():
    result = read_page(CLAUDE_PAGE)
    assert result["provider"] == "claude"
    assert result["note"] == ""
    assert rows(result) == [
        ("현재 세션", 45, "오전 11:50에 초기화"),
        ("주간 한도 · 모든 모델", 61, "(금) 오전 6:00에 초기화"),
        ("주간 한도 · Sonnet만", 12, "(금) 오전 6:00에 초기화"),
    ]


@needs_node
def test_gemini_cards_read_in_either_line_order():
    result = read_page(GEMINI_PAGE)
    assert result["provider"] == "gemini"
    assert rows(result) == [
        ("현재 사용량", 0, "오후 2:27에 초기화"),
        ("주간 한도", 70, "11월 25일 오전 7:27에 초기화"),
    ]


@needs_node
def test_other_pages_are_not_read():
    script = (
        DOM
        + (RESOURCE_DIR / "extractor.js").read_text(encoding="utf-8")
        + "\nconst r = AIUsageBoardReader;"
        + "\nconsole.log(JSON.stringify(["
        + "r.providerFor({ protocol: 'https:', hostname: 'claude.ai', pathname: '/settings/usage/', hash: '' }),"
        + "r.providerFor({ protocol: 'http:', hostname: 'claude.ai', pathname: '/settings/usage', hash: '' }),"
        + "r.providerFor({ protocol: 'https:', hostname: 'claude.ai', pathname: '/chat/1', hash: '' }),"
        + "r.providerFor({ protocol: 'https:', hostname: 'evil.example', pathname: '/usage', hash: '' }),"
        + "r.usedPercent('7% used'), r.usedPercent('25% left'), r.usedPercent('45%')"
        + "]));"
    )
    done = subprocess.run([NODE, "-e", script], capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert json.loads(done.stdout) == ["claude", None, None, None, 7, 75, None]
