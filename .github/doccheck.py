#!/usr/bin/env python3
"""Documentation-drift gate for the Kodi tree: stale names and dead pointers in LIVING docs.

    bin/doccheck                 every repo in the config's default_repos that is checked out
    bin/doccheck --repo PATH     one repo (repeatable)
    bin/doccheck --list-terms    print the retired-term list and exit

Stdlib only. The config is doccheck.json NEXT TO THIS FILE. The canonical pair is the meta
root's bin/doccheck + bin/doccheck.json; each sibling repo carries a byte-identical copy at
.github/doccheck.py + .github/doccheck.json so its own CI can run it (the meta repo is private,
so a public repo cannot fetch it). tests/test_doccheck.py in the meta root fails when a copy
drifts from the original.

Which files: the config's `include` globs, minus its `exclude` globs, minus any file whose
first five lines carry a HISTORICAL, SUPERSEDED or RETIRED banner (upper case, as a word).
Files come from `git ls-files --cached --others --exclude-standard` (so a nested checkout or
an ignored build dir is never read), or a plain walk outside git.

Checks, one finding per line as `path:line: [check] message`:
  retired   a retired term (config `retired_terms`) whose SENTENCE does not say it is history.
            A sentence says so when it holds one of `history_words` (case-insensitive, whole
            words, code spans ignored): renamed, retired, was, deleted, until, earlier and the
            rest. A sentence ends at . ! or ? and never crosses a blank line, so wrapped prose
            counts as one; a fenced block counts as one. That is the whole allowance rule.
  link      a relative markdown link [x](path) whose target is missing (anchors ignored).
            Links that leave the repo or enter an untracked directory (a nested checkout) are
            skipped, and so is a sentence holding one of `future_words` (a plan's new file).
  path      a backticked path (`dir/file`) whose first part is a tracked top-level directory
            of THIS repo and not in `path_shared_dirs` (tools/, tests/, docs/ and friends exist
            in every repo, so `tests/x.py` may be another repo's), and which does not exist.
            Spans with spaces, globs or placeholders (<id>, {x}, $VAR, ...) are skipped, a
            trailing :line is ignored, and history or future sentences are skipped.
  section   a bare `§N` / `§N.M` must resolve to a heading numbered N / N.M in the same
            document, unless the document numbers no headings at all or the sentence names
            another document (a `.md` name or one of `external_docs`, such as vault).
            `NAME §N` is resolved in NAME's file when NAME is in the `documents` map and the
            file exists in this repo; any other name is skipped.
  char      U+2014, U+2013, U+2015 or U+1F916 anywhere in a living doc.
  version   for each add-on the repo builds (an addon.xml at most two directories deep), `<id>`
            followed within 20 characters by a version must state the current version,
            unless the sentence says it is history.
Fenced code is skipped by link, path, section and version; retired and char read every line.
Exit 0 clean, 1 on any finding, 2 on a usage or config error.
"""

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "doccheck.json")
HOME_ROOT = os.path.dirname(HERE)

BANNER = re.compile(r"\b(HISTORICAL|SUPERSEDED|RETIRED)\b|(?i:\bstatus:\s*history\b)")
LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
SPAN = re.compile(r"`([^`\n]+)`")
PATHLIKE = re.compile(r"^[A-Za-z0-9_.@+~-]+(?:/[A-Za-z0-9_.@+~-]*)+$")
LINE_SUFFIX = re.compile(r":\d+(?:[-,:]\d+)*$")
HEADING = re.compile(r"^#{1,6}\s+§?(\d+(?:\.\d+)*)\.?(?=\s|$)")
SECTION = re.compile(r"§\s?(\d+(?:\.\d+)*)")
VERSION = r"(\d{4}\.\d{1,2}\.\d{1,2}(?:\.\d+)?|\d+\.\d+\.\d+(?:[.-][0-9A-Za-z]+)*)"
BANNED = {
    "—": "em dash (U+2014)",
    "–": "en dash (U+2013)",
    "―": "horizontal bar (U+2015)",
    "\U0001f916": "robot emoji (U+1F916)",
}


def glob_re(pattern):
    """A path glob as a regex: `**/` any number of directories, `*` and `?` within one."""
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            j = pattern.find("]", i)
            if j < 0:
                out += re.escape(c)
            else:
                out += "[" + pattern[i + 1 : j].replace("\\", "\\\\") + "]"
                i = j
        else:
            out += re.escape(c)
        i += 1
    return re.compile(out + r"\Z")


def load_config(path=CONFIG):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    words = []
    for w in cfg["history_words"]:
        if w[-1].isalnum():
            words.append(r"\b" + re.escape(w) + r"\b")
        else:
            words.append(r"\b" + re.escape(w))
    cfg["_history"] = re.compile("|".join(words), re.IGNORECASE)
    cfg["_future"] = re.compile(
        r"\b(?:" + "|".join(map(re.escape, cfg["future_words"])) + r")\b", re.IGNORECASE
    )
    other = [r"\.md\b"] + [r"\b" + re.escape(w) + r"\b" for w in cfg["external_docs"]]
    cfg["_other_doc"] = re.compile("|".join(other))
    cfg["_include"] = [glob_re(g) for g in cfg["include"]]
    cfg["_exclude"] = [glob_re(g) for g in cfg["exclude"]]
    return cfg


def repo_files(root):
    """Every file this repo owns, relative, as git sees it (tracked or new, never ignored)."""
    if os.path.exists(os.path.join(root, ".git")):
        out = subprocess.run(
            [
                "git",
                "-C",
                root,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", "replace")
        files = [p for p in out.split("\0") if p]
        return {p for p in files if os.path.isfile(os.path.join(root, p))}
    found = set()
    for d, dirs, names in os.walk(root):
        dirs[:] = [
            x
            for x in dirs
            if x != ".git" and not os.path.exists(os.path.join(d, x, ".git"))
        ]
        for n in names:
            found.add(os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/"))
    return found


def living_docs(root, files, cfg):
    out = []
    for p in sorted(files):
        if not p.endswith(".md"):
            continue
        if not any(r.match(p) for r in cfg["_include"]) or any(
            r.match(p) for r in cfg["_exclude"]
        ):
            continue
        with open(os.path.join(root, p), encoding="utf-8", errors="replace") as f:
            head = [next(f, "") for _ in range(5)]
        if any(BANNER.search(line) for line in head):
            continue
        out.append(p)
    return out


def addons(root, files):
    """[(id, version)] for every addon.xml at depth <= 2 of the repo."""
    out = []
    for p in sorted(files):
        if os.path.basename(p) != "addon.xml" or p.count("/") > 2:
            continue
        try:
            el = ET.parse(os.path.join(root, p)).getroot()
        except ET.ParseError:
            continue
        if el.tag == "addon" and el.get("id") and el.get("version"):
            out.append((el.get("id"), el.get("version")))
    return out


def fenced(lines):
    """For each line, whether it sits inside (or opens/closes) a fenced code block."""
    flags, fence = [], None
    for line in lines:
        s = line.strip()
        if fence:
            flags.append(True)
            if s.startswith(fence):
                fence = None
            continue
        if s.startswith("```") or s.startswith("~~~"):
            fence = s[:3]
            flags.append(True)
            continue
        flags.append(False)
    return flags


SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`(*_\[>#-])")


def sentences(lines):
    """For each line, [(first column, last column, sentence text)] for the sentences on it.

    A sentence never crosses a blank line or a fence edge, and ends at . ! or ? followed by
    a space and a capital, a digit or markup, so wrapped prose is one sentence however many
    lines it spans. Inside fenced code the whole blank-line-delimited block is one sentence."""
    out = [[] for _ in lines]
    code = fenced(lines)

    def flush(first, block):
        text = "\n".join(block)
        offsets, pos = [], 0
        for line in block:
            offsets.append(pos)
            pos += len(line) + 1
        ends = [] if code[first] else [m.end() for m in SENTENCE_END.finditer(text)]
        cut = [0] + ends + [len(text) + 1]
        for i, line in enumerate(block):
            a, b = offsets[i], offsets[i] + len(line)
            for x, y in zip(cut, cut[1:]):
                if x <= b and y > a:
                    out[first + i].append((max(x, a) - a, min(y, b + 1) - a, text[x:y]))

    para, start, prev = [], 0, None
    for i, line in enumerate(lines):
        if para and (not line.strip() or code[i] != prev):
            flush(start, para)
            para = []
        if line.strip():
            if not para:
                start = i
            para.append(line)
        prev = code[i]
    if para:
        flush(start, para)
    return out


def sentence_at(spans, col):
    for a, b, text in spans:
        if a <= col < b:
            return text
    return spans[-1][2] if spans else ""


def headings_of(path, cache):
    if path not in cache:
        nums = set()
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
        for line, code in zip(lines, fenced(lines)):
            m = None if code else HEADING.match(line)
            if m:
                nums.add(m.group(1))
        cache[path] = nums
    return cache[path]


def check_repo(root, cfg):
    root = os.path.abspath(root)
    files = repo_files(root)
    dirs = set()
    for p in files:
        parts = p.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    top_dirs = {p.split("/")[0] for p in files if "/" in p}
    docs = living_docs(root, files, cfg)
    ids = addons(root, files)
    shared = set(cfg["path_shared_dirs"])
    heads = {}
    findings = []

    def exists(rel):
        return rel in files or rel in dirs

    def says(pattern, text):
        return bool(pattern.search(SPAN.sub(" ", text)))

    for doc in docs:
        with open(os.path.join(root, doc), encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
        code = fenced(lines)
        context = sentences(lines)
        for n, (line, in_code) in enumerate(zip(lines, code), 1):
            where = f"{doc}:{n}"
            spans = context[n - 1]

            def history_at(col, spans=spans):
                return says(cfg["_history"], sentence_at(spans, col))

            for ch, name in BANNED.items():
                if ch in line:
                    findings.append(f"{where}: [char] {name}")
            for t in cfg["retired_terms"]:
                col = line.find(t)
                while col >= 0:
                    if not history_at(col):
                        findings.append(
                            f"{where}: [retired] '{t}' is a retired name; update it, or say in the same sentence that it is history"
                        )
                        break
                    col = line.find(t, col + 1)
            if in_code:
                continue
            for m in LINK.finditer(line):
                target = m.group(1).split("#", 1)[0].split("?", 1)[0]
                if not target or SCHEME.match(target):
                    continue
                base = "" if target.startswith("/") else os.path.dirname(doc)
                rel = os.path.normpath(
                    os.path.join(base, target.replace("%20", " ").lstrip("/"))
                ).replace(os.sep, "/")
                if (
                    rel.startswith("..")
                    or rel == "."
                    or ("/" in rel and rel.split("/")[0] not in top_dirs)
                ):
                    continue
                if not exists(rel) and not says(
                    cfg["_future"], sentence_at(spans, m.start())
                ):
                    findings.append(
                        f"{where}: [link] {m.group(1)} -> {rel} does not exist"
                    )
            for m in SPAN.finditer(line):
                p = LINE_SUFFIX.sub("", m.group(1).strip()).rstrip("/")
                if (
                    "..." in p
                    or not PATHLIKE.match(p)
                    or p.split("/")[0] not in top_dirs - shared
                    or exists(p)
                ):
                    continue
                around = sentence_at(spans, m.start())
                if not says(cfg["_history"], around) and not says(
                    cfg["_future"], around
                ):
                    findings.append(
                        f"{where}: [path] `{p}` does not exist in this repo"
                    )
            plain = SPAN.sub(lambda m: " " * len(m.group(0)), line)
            for m in SECTION.finditer(plain):
                num, before = m.group(1), plain[: m.start()].rstrip()
                word = re.search(r"([A-Za-z][\w-]*)(?:\.md)?$", before)
                name = word.group(1) if word else ""
                target = cfg["documents"].get(name)
                if target:
                    if target not in files:
                        continue
                    label = f"{name} §{num}"
                else:
                    around = sentence_at(spans, m.start())
                    if cfg["_other_doc"].search(around) or not headings_of(
                        os.path.join(root, doc), heads
                    ):
                        continue
                    target, label = doc, f"§{num}"
                if num not in headings_of(os.path.join(root, target), heads):
                    findings.append(
                        f"{where}: [section] {label} resolves to no heading in {target}"
                    )
            for aid, ver in ids:
                for m in re.finditer(
                    re.escape(aid) + r"(?![\w.-]*\w)[^\n]{0,20}?" + VERSION, line
                ):
                    if m.group(1) != ver and not history_at(m.start()):
                        findings.append(
                            f"{where}: [version] {aid} stated as {m.group(1)}, current is {ver}"
                        )
    return findings, len(docs)


def default_repos(cfg):
    out = []
    for r in cfg["default_repos"]:
        p = os.path.normpath(os.path.join(HOME_ROOT, r))
        if os.path.isdir(p):
            out.append(p)
    return out


def main(argv):
    repos, list_terms, i = [], False, 0
    while i < len(argv):
        a = argv[i]
        if a == "--repo" and i + 1 < len(argv):
            repos.append(argv[i + 1])
            i += 2
            continue
        if a == "--list-terms":
            list_terms = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print(f"doccheck: unknown argument {a!r}", file=sys.stderr)
            return 2
        i += 1
    try:
        cfg = load_config()
    except (OSError, ValueError, KeyError) as e:
        print(f"doccheck: cannot load {CONFIG}: {e}", file=sys.stderr)
        return 2
    if list_terms:
        for t in cfg["retired_terms"]:
            print(t)
        return 0
    repos = repos or default_repos(cfg)
    total, ndocs = [], 0
    for r in repos:
        if not os.path.isdir(r):
            print(f"doccheck: no such repo {r!r}", file=sys.stderr)
            return 2
        found, n = check_repo(r, cfg)
        name = os.path.basename(os.path.abspath(r))
        for f in found:
            print(f"{name}/{f}")
        total += found
        ndocs += n
    if total:
        print(
            f"doccheck: {len(total)} finding(s) in {ndocs} living docs across {len(repos)} repo(s)"
        )
        return 1
    print(f"doccheck: clean, {ndocs} living docs across {len(repos)} repo(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
