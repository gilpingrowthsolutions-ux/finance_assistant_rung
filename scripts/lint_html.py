#!/usr/bin/env python3
"""Structural HTML lint for Rung templates.

Usage:
    python scripts/lint_html.py [path/to/template.html]

Default path: templates/index.html (assumes run from project root).

Exits 0 on clean, 1 if any real structural issues are found.
Warnings (void-tag misuse, self-closing non-void tags) are reported
but do NOT cause a non-zero exit.

Detects:
- Unclosed tags at EOF (still on the parser stack)
- Stray </tag> with no matching opener
- Improper nesting (closing a tag that's not the most recent open)
- Unclosed <!-- comments
- Script/style region balance
- Void tags with explicit </tag> (warning only)
- Unclosed attribute quotes (heuristic)
"""

from __future__ import annotations

import os
import re
import sys
from html.parser import HTMLParser
import glob

VOID_TAGS = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
})

CDATA_TAGS = frozenset({'script', 'style'})


class LintCollector(HTMLParser):
    """Parses HTML and collects structural issues."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[tuple[str, tuple[int, int]]] = []
        self.in_cdata: str | None = None
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.tag_count: dict[str, int] = {}
        self.endtag_count: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.in_cdata:
            return
        if tag in VOID_TAGS:
            return
        if tag in CDATA_TAGS:
            self.in_cdata = tag
        self.stack.append((tag, self.getpos()))
        self.tag_count[tag] = self.tag_count.get(tag, 0) + 1

    def handle_endtag(self, tag: str) -> None:
        if self.in_cdata and tag == self.in_cdata:
            self.in_cdata = None
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    self.stack.pop(i)
                    self.endtag_count[tag] = self.endtag_count.get(tag, 0) + 1
                    return
            self.errors.append(
                f"CDATA </{tag}> with no matching opener at line {self.getpos()[0]}"
            )
            return

        if tag in VOID_TAGS:
            self.warnings.append(
                f"</{tag}> at line {self.getpos()[0]} (void tag — end tag unnecessary)"
            )
            return

        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                if i != len(self.stack) - 1:
                    excess = self.stack[i + 1:]
                    for ex_tag, ex_pos in excess:
                        self.errors.append(
                            f"Improperly nested </{tag}> at line {self.getpos()[0]} "
                            f"closed <{ex_tag}> opened at line {ex_pos[0]} (still open)"
                        )
                self.stack.pop(i)
                self.endtag_count[tag] = self.endtag_count.get(tag, 0) + 1
                return
        self.errors.append(
            f"Stray </{tag}> with no matching opener at line {self.getpos()[0]}"
        )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in VOID_TAGS:
            self.tag_count[tag] = self.tag_count.get(tag, 0) + 1
            return
        if tag in CDATA_TAGS:
            self.errors.append(
                f"Self-closing <{tag}/> at line {self.getpos()[0]} — illegal in HTML5"
            )
            return
        self.warnings.append(
            f"Self-closing <{tag}/> at line {self.getpos()[0]} — non-void, illegal in HTML5"
        )


def lint_file(filepath: str) -> int:
    """Lint *filepath* and return an exit code (0 = clean, 1 = issues)."""
    src = open(filepath, encoding='utf-8').read()

    # --- Raw scan for unclosed <!-- comments ---
    unclosed_comments: list[int] = []
    i = 0
    while True:
        j = src.find('<!--', i)
        if j < 0:
            break
        k = src.find('-->', j + 4)
        if k < 0:
            line_no = src[:j].count('\n') + 1
            unclosed_comments.append(line_no)
            break
        i = k + 3

    # --- Parse ---
    parser = LintCollector()
    try:
        parser.feed(src)
    except Exception as e:
        parser.errors.append(f"Parser exception: {e!r}")

    # --- Report ---
    print("=" * 72)
    print(f"STRUCTURAL HTML LINT — {filepath}")
    print("=" * 72)
    print()

    # 1. Tag stack at EOF
    print("[1] Unclosed tags at end of file:")
    if parser.stack:
        for tag, (ln, col) in parser.stack:
            print(f"     <{tag}> opened at line {ln}, col {col}  <- NEVER CLOSED")
    else:
        print("     (none — every opened tag has a closing tag) OK")
    print()

    # 2. Tag open/close counts
    all_tags = sorted(set(parser.tag_count.keys()) | set(parser.endtag_count.keys()))
    mismatched: list[tuple[str, int, int, int]] = []
    print("[2] Tag open/close balance:")
    print(f"     {'tag':<14} {'opens':>6} {'closes':>7}  {'delta':>6}")
    print(f"     {'-' * 14} {'-' * 6} {'-' * 7}  {'-' * 6}")
    for tag in all_tags:
        o = parser.tag_count.get(tag, 0)
        c = parser.endtag_count.get(tag, 0)
        d = o - c
        flag = "" if d == 0 else "  WARN"
        print(f"     {tag:<14} {o:>6} {c:>7}  {d:>6}{flag}")
        if d != 0:
            mismatched.append((tag, o, c, d))
    print()
    if mismatched:
        print(f"     WARN: {len(mismatched)} tag(s) with unbalanced opens vs closes:")
        for tag, o, c, d in mismatched:
            print(f"       {tag}: opened {o}x, closed {c}x, delta {d:+d}")
    else:
        print("     OK: All non-void tags have balanced open/close counts")
    print()

    # 3. Stray / improperly nested end tags
    print("[3] Stray or improperly nested end tags:")
    if parser.errors:
        for e in parser.errors:
            print(f"     {e}")
    else:
        print("     (none) OK")
    print()

    # 4. Warnings (void tag misuse, non-void self-close)
    print("[4] Warnings (suspicious but not necessarily broken):")
    if parser.warnings:
        for w in parser.warnings:
            print(f"     {w}")
    else:
        print("     (none) OK")
    print()

    # 5. Comment regions
    oc = src.count('<!--')
    cc = src.count('-->')
    print("[5] Comment-region balance:")
    print(f"     <!-- openers: {oc}")
    print(f"     -->  closers: {cc}")
    print(f"     delta:        {oc - cc}")
    if unclosed_comments:
        print(f"     WARN: unclosed <!-- at line(s): {unclosed_comments}")
    else:
        print("     OK: every <!-- has a matching -->")
    print()

    # 6. Script / style region balance
    # Strip comment content so <style>/<script> mentions inside comments
    # don't produce false positives (e.g. "the `<style>` block inside a comment")
    print("[6] Script/style region balance:")
    src_no_comments = re.sub(r'<!--.*?-->', '', src, flags=re.DOTALL)
    script_opens = len(re.findall(r'<script\b[^>]*>', src_no_comments))
    script_closes = len(re.findall(r'</script\s*>', src_no_comments))
    style_opens = len(re.findall(r'<style\b[^>]*>', src_no_comments))
    style_closes = len(re.findall(r'</style\s*>', src_no_comments))
    print(f"     <script> openers: {script_opens}, closers: {script_closes}, delta: {script_opens - script_closes}")
    print(f"     <style>  openers: {style_opens},  closers: {style_closes},  delta: {style_opens - style_closes}")
    if script_opens != script_closes:
        print("     WARN: script region imbalance")
    if style_opens != style_closes:
        print("     WARN: style region imbalance")
    print()

    # 7. Attribute quote balance (heuristic)
    print("[7] Attribute quote balance (heuristic, per-tag):")
    unbalanced_quotes: list[tuple[int, str]] = []
    i = 0
    while i < len(src):
        if src[i] == '<':
            if src.startswith('<!--', i):
                k = src.find('-->', i + 4)
                if k < 0:
                    break
                i = k + 3
                continue
            if src.startswith('<!', i):
                j = src.find('>', i)
                if j < 0:
                    break
                i = j + 1
                continue
            in_dq = False
            in_sq = False
            j = i + 1
            while j < len(src):
                c = src[j]
                if c == '"' and not in_sq:
                    in_dq = not in_dq
                elif c == "'" and not in_dq:
                    in_sq = not in_sq
                elif c == '>' and not in_dq and not in_sq:
                    break
                j += 1
            if j >= len(src):
                line_no = src[:i].count('\n') + 1
                unbalanced_quotes.append((line_no, "tag never closed (no '>' found)"))
                break
            if in_dq or in_sq:
                line_no = src[:i].count('\n') + 1
                unbalanced_quotes.append((line_no, f"unbalanced quote (dq={in_dq}, sq={in_sq})"))
            i = j + 1
        else:
            i += 1

    if unbalanced_quotes:
        for ln, msg in unbalanced_quotes[:10]:
            print(f"     WARN line {ln}: {msg}")
    else:
        print("     OK: every tag's attribute quotes are balanced")
    print()

    # --- Summary ---
    print("=" * 72)
    total = (
        len(parser.errors)             # real structural errors
        + len(parser.stack)            # unclosed tags at EOF
        + len(unclosed_comments)       # unclosed comment openers
        + len(unbalanced_quotes)       # unbalanced attribute quotes
        + max(0, script_opens - script_closes)  # script region imbalance
        + max(0, style_opens - style_closes)    # style region imbalance
    )
    if total == 0:
        print("RESULT: CLEAN (no structural bugs found)")
        return 0
    print(f"RESULT: {total} potential issue(s) found")
    return 1


def main() -> int:
    """Parse CLI args and run the linter on all specified files.

    Usage:
        python scripts/lint_html.py                - lint all templates/*.html
        python scripts/lint_html.py templates/*.html - lint specific files
        python scripts/lint_html.py --help          - print this message

    Returns 0 if all files are clean, 1 if any file has issues.
    """
    if '--help' in sys.argv or '-h' in sys.argv:
        print(__doc__)
        return 0

    if len(sys.argv) > 1:
        targets = sys.argv[1:]
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = os.path.join(base, 'templates', '*.html')
        targets = sorted(glob.glob(pattern))

    if not targets:
        print("No HTML files to lint.")
        return 0

    exit_code = 0
    for t in targets:
        ec = lint_file(t)
        if ec != 0:
            exit_code = 1
        print()
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
