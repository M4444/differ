#!/usr/bin/env python3
"""Show a git diff in Phabricator's Differential side-by-side view.

    differ [git diff arguments]

The arguments go to `git diff`, so `differ`, `differ --staged` and `differ HEAD~3` all work. The
page is written to /tmp/differ-<repository>-<hash>.html, named after the repository and the diff,
and opened with xdg-open.

This is a port of the code that renders a diff in Phabricator, from Phabricator, Arcanist and
libphutil. Each section names the PHP it comes from. Phabricator works on bytes rather than
characters, and so does this port, since the intraline highlighting counts bytes.
"""

import base64
import hashlib
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))

# DifferentialChangesetParser
CONTEXT_LINES = 8
HIGHLIGHT_BYTE_LIMIT = 262144

# PhabricatorDifferentialConfigOptions, "differential.whitespace-matters".
WHITESPACE_MATTERS = [r'\.py$', r'\.l?hs$', r'\.ya?ml$']

# Characters PHP's trim() removes.
PHP_WS = b' \t\n\r\0\x0b'

SHOW_ALL = 'show-all'
IGNORE_MOST = 'ignore-most'


class DiffError(Exception):
    pass


# -- HTML ---------------------------------------------------------------------


def escape(text):
    """PHP's htmlspecialchars() with ENT_QUOTES, on bytes."""
    if isinstance(text, str):
        text = text.encode()
    return (
        text.replace(b'&', b'&amp;')
        .replace(b'<', b'&lt;')
        .replace(b'>', b'&gt;')
        .replace(b'"', b'&quot;')
        .replace(b"'", b'&#039;')
    )


def tag(name, attrs=(), content=b''):
    """phutil_tag(): attributes whose value is None are left out. Content is a list of
    already-escaped HTML, or None."""
    out = [b'<', name.encode()]
    for key, value in attrs:
        if value is None:
            continue
        out += [b' ', key.encode(), b'="', escape(str(value)), b'"']
    out.append(b'>')
    if isinstance(content, (list, tuple)):
        out += [c for c in content if c]
    elif content:
        out.append(content)
    out += [b'</', name.encode(), b'>']
    return b''.join(out)


# -- Utilities (libphutil utils.php and utf8.php) -----------------------------


def split_lines(corpus):
    """phutil_split_lines(), keeping line endings."""
    if not corpus:
        return [b'']
    lines = re.split(rb'(?<=\n)', corpus)
    if lines[-1] == b'':
        lines.pop()
    return lines


def is_combining(char):
    for cp in map(ord, char.decode()):
        if (
            0x0300 <= cp <= 0x036F
            or 0x1DC0 <= cp <= 0x1DFF
            or 0x20D0 <= cp <= 0x20FF
            or 0xFE20 <= cp <= 0xFE2F
        ):
            return True
    return False


def utf8v_combined(string):
    """phutil_utf8v_combined(): characters with combining marks merged. A leading combining mark
    gets a space in front of it, as in PHP."""
    chars = [c.encode() for c in string.decode()]
    if not chars:
        return []
    buf = b' ' if is_combining(chars[0]) else None
    parts = []
    for char in chars:
        if len(char) > 1 and is_combining(char):
            buf += char
        else:
            if buf is not None:
                parts.append(buf)
            buf = char
    parts.append(buf)
    return parts


def stripcslashes(text):
    """PHP's stripcslashes(), for quoted git paths."""
    simple = {'n': 10, 't': 9, 'r': 13, 'a': 7, 'v': 11, 'b': 8, 'f': 12}
    out = bytearray()
    data = text.encode('utf-8', 'surrogateescape')
    i = 0
    while i < len(data):
        c = data[i]
        if c != 0x5C or i + 1 == len(data):
            out.append(c)
            i += 1
            continue
        i += 1
        n = chr(data[i])
        if n in simple:
            out.append(simple[n])
            i += 1
        elif n == 'x' and i + 1 < len(data) and re.match(rb'[0-9a-fA-F]', data[i + 1 : i + 2]):
            m = re.match(rb'[0-9a-fA-F]{1,2}', data[i + 1 :])
            out.append(int(m.group(0), 16))
            i += 1 + len(m.group(0))
        elif n in '01234567':
            m = re.match(rb'[0-7]{1,3}', data[i:])
            out.append(int(m.group(0), 8) & 0xFF)
            i += len(m.group(0))
        else:
            out.append(data[i])
            i += 1
    return out.decode('utf-8', 'replace')


# -- Parsing git diffs (arcanist ArcanistDiffParser) --------------------------


class Hunk:
    """DifferentialHunk."""

    def __init__(self, old_offset, old_len, new_offset, new_len, changes):
        self.old_offset = old_offset
        self.old_len = old_len
        self.new_offset = new_offset
        self.new_len = new_len
        self.changes = changes
        self.lines = split_lines(changes)

    def make_file(self, include):
        """makeContent(), joined: the file text this hunk covers."""
        results = []
        use_next_newline = False
        for line in self.lines:
            first = line[:1]
            if not first:
                continue
            if first == b'\\':
                if use_next_newline:
                    results[-1] = results[-1].rstrip(b'\n')
            elif first not in include:
                use_next_newline = False
            else:
                use_next_newline = True
                results.append(line[1:])
        return b''.join(results)

    def structured_file(self, kind):
        """getStructuredFile(): line number to {'type', 'text'}."""
        number = self.new_offset if kind == b'+' else self.old_offset
        lines = []
        for line in self.lines:
            # getStructuredLines() skips lines whose first byte is empty.
            if not line[:1] or line[:1] == b'0':
                continue
            lines.append({'type': line[:1], 'text': line[1:]})
        result = {}
        keep = True
        for line in lines:
            if line['type'] in (b' ', kind):
                result[number] = line
                number += 1
                keep = True
            elif line['type'] == b'\\':
                if keep and number - 1 in result:
                    prev = result[number - 1]
                    result[number - 1] = dict(prev, text=prev['text'].rstrip(b'\r\n'))
                    result[number] = line
                    number += 1
                    keep = False
            else:
                keep = False
        return result

    def count(self, kind):
        return sum(1 for line in self.lines if line[:1] == kind)


class Changeset:
    """ArcanistDiffChange and DifferentialChangeset."""

    def __init__(self, path):
        self.filename = path
        self.old_file = None
        self.change_type = 'change'
        self.file_type = 'text'
        self.hunks = []
        self.old_props = {}
        self.new_props = {}
        self.away_paths = []
        self.copy_lines = {}

    def make_old_file(self):
        return b''.join(h.make_file(b' -') for h in self.hunks)

    def make_new_file(self):
        return b''.join(h.make_file(b' +') for h in self.hunks)

    def affected_line_count(self):
        return sum(h.count(b'+') + h.count(b'-') for h in self.hunks)

    def sort_key(self):
        return self.filename.replace('.h', '.!h')


def unescape_filename(name):
    if re.match(r'^".+"$', name):
        return stripcslashes(name[1:-1])
    return name


def extract_git_common_filename(paths):
    """extractGitCommonFilename(): the path in "diff --git a/x b/x", or None for a rename."""
    prefix = r'(?:[^/]+/)?'
    pattern = (
        r'^(?P<old>(?P<oldq>"?)' + prefix + r'(?P<common>.+)(?P=oldq))'
        r' (?P<new>(?P<newq>"?)' + prefix + r'(?P=common)(?P=newq))$'
    )
    m = re.match(pattern, paths.rstrip('\r\n'))
    if not m:
        return None
    return unescape_filename(m.group('newq') + m.group('common') + m.group('newq'))


HUNK_HEADER = re.compile(rb'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def parse_hunks(lines, i):
    """Read hunks starting at lines[i]. Returns (hunks, next index)."""
    hunks = []
    while i < len(lines):
        m = HUNK_HEADER.match(lines[i])
        if not m:
            break
        old_offset = int(m.group(1))
        old_len = 1 if m.group(2) is None else int(m.group(2))
        new_offset = int(m.group(3))
        new_len = 1 if m.group(4) is None else int(m.group(4))
        i += 1
        old_left, new_left = old_len, new_len
        body = []
        while i < len(lines) and (old_left > 0 or new_left > 0 or lines[i][:1] == b'\\'):
            line = lines[i]
            first = line[:1]
            if first == b'+':
                new_left -= 1
            elif first == b'-':
                old_left -= 1
            elif first != b'\\':
                old_left -= 1
                new_left -= 1
            body.append(line)
            i += 1
        hunks.append(Hunk(old_offset, old_len, new_offset, new_len, b''.join(body)))
    return hunks, i


GIT_HEADERS = [
    r'(?P<new>new) file mode (?P<newmode>\d+)',
    r'(?P<deleted>deleted) file mode (?P<oldmode>\d+)',
    r'old mode (?P<oldmode>\d+)',
    r'new mode (?P<newmode>\d+)',
    r'similarity index ',
    r'rename from (?P<old>.*)',
    r'(?P<move>rename) to (?P<cur>.*)',
    r'copy from (?P<old>.*)',
    r'(?P<copy>copy) to (?P<cur>.*)',
]


def parse_git_diff(data):
    """Parse `git diff` output into changesets, as parseDiff() and parseIndexHunk() do for git."""
    changes = {}

    def build(path):
        if path is not None and path in changes:
            return changes[path]
        change = Changeset(path)
        if path is not None:
            changes[path] = change
        return change

    lines = split_lines(data)
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith(b'diff --git '):
            i += 1
            continue
        oldnew = line[len(b'diff --git ') :].decode('utf-8', 'replace')
        path = extract_git_common_filename(oldnew)
        change = build(path)
        if path is not None:
            change.old_file = path
        i += 1

        # The header lines that describe modes, moves and copies.
        while i < len(lines):
            text = lines[i].rstrip(b'\r\n').decode('utf-8', 'replace')
            for pattern in GIT_HEADERS:
                m = re.match('^' + pattern, text)
                if m:
                    break
            if not m:
                break
            groups = m.groupdict()
            if groups.get('oldmode'):
                change.old_props['unix:filemode'] = groups['oldmode']
            if groups.get('newmode'):
                change.new_props['unix:filemode'] = groups['newmode']
            if groups.get('deleted'):
                change.change_type = 'delete'
            if groups.get('new'):
                if change.change_type == 'delete':
                    change.change_type = 'change'
                else:
                    change.change_type = 'add'
            if groups.get('old'):
                change.old_file = unescape_filename(groups['old'])
            if groups.get('cur'):
                cur = unescape_filename(groups['cur'])
                if change.filename is None:
                    change.filename = cur
                    changes[cur] = change
            if groups.get('copy'):
                change.change_type = 'copy-here'
                old = build(change.old_file)
                if old.change_type == 'move-away':
                    old.change_type = 'multicopy'
                else:
                    old.change_type = 'copy-away'
                old.away_paths.append(change.filename)
            if groups.get('move'):
                change.change_type = 'move-here'
                old = build(change.old_file)
                if old.change_type in ('multicopy', 'move-away', 'copy-away'):
                    old.change_type = 'multicopy'
                else:
                    old.change_type = 'move-away'
                old.away_paths.append(change.filename)
            i += 1

        if i < len(lines) and lines[i].startswith(b'index '):
            i += 1
        if i >= len(lines) or lines[i].startswith(b'diff --git '):
            continue
        text = lines[i].rstrip(b'\r\n')
        if re.match(rb'^(Binary files|Files) .* and .* differ$', text):
            change.file_type = 'binary'
            i += 1
            continue
        if text == b'GIT binary patch':
            change.file_type = 'binary'
            i += 1
            while i < len(lines) and not lines[i].startswith(b'diff --git '):
                i += 1
            continue
        if lines[i].startswith(b'--- '):
            i += 1
        if i < len(lines) and lines[i].startswith(b'+++ '):
            i += 1
        change.hunks, i = parse_hunks(lines, i)

    return list(changes.values())


# -- Hunks to lines (phabricator DifferentialHunkParser) ----------------------


def parse_line_data(hunks):
    """parseHunksForLineData()"""
    old_lines = []
    new_lines = []
    for hunk in hunks:
        types = []
        texts = []
        for index, line in enumerate(hunk.lines):
            first = line[:1]
            if not first:
                types.append(None)
                texts.append(b'')
            elif first == b' ':
                types.append(None)
                texts.append(line[1:])
            elif first in (b'\r', b'\n'):
                types.append(None)
                texts.append(line)
            elif first in (b'+', b'-', b'\\'):
                types.append(first.decode())
                texts.append(line[1:])
            else:
                raise DiffError(
                    'Unexpected leading character %r at line index %d!' % (first, index)
                )

        old_line = hunk.old_offset
        new_line = hunk.new_offset
        for cursor, type_ in enumerate(types):
            data = {'type': type_, 'text': texts[cursor], 'line': new_line}
            if type_ == '\\':
                type_ = types[cursor - 1] if cursor else None
                data['text'] = data['text'].lstrip(PHP_WS)
            if type_ == '+':
                new_lines.append(data)
                new_line += 1
            elif type_ == '-':
                data['line'] = old_line
                old_lines.append(data)
                old_line += 1
            else:
                new_lines.append(data)
                old_lines.append(dict(data, line=old_line))
                new_line += 1
                old_line += 1
    return old_lines, new_lines


def line_type_map(lines):
    return {line['line']: line['type'] for line in lines if line}


def apply_line_type_map(lines, type_map):
    for line in lines:
        line['type'] = type_map.get(line['line'])


def reparse_hunks(old_lines, new_lines, mode):
    """reparseHunksForSpecialAttributes(): pair the two sides up, padding with None, then
    updateChangeTypesForWhitespaceMode()."""
    olds = list(reversed(old_lines))
    news = list(reversed(new_lines))
    rebuild_old = []
    rebuild_new = []
    while olds or news:
        old = olds.pop() if olds else None
        new = news.pop() if news else None
        o_type = old['type'] if old else None
        n_type = new['type'] if new else None
        if o_type is not None and n_type is None:
            rebuild_old.append(old)
            rebuild_new.append(None)
            if new:
                news.append(new)
            continue
        if n_type is not None and o_type is None:
            rebuild_old.append(None)
            rebuild_new.append(new)
            if old:
                olds.append(old)
            continue
        rebuild_old.append(old)
        rebuild_new.append(new)

    if mode != SHOW_ALL:
        # Lines marked unchanged that differ by internal whitespace are marked changed. Leading and
        # trailing whitespace is ignored.
        for old, new in zip(rebuild_old, rebuild_new):
            if not old or not new:
                continue
            if old['type'] is None and new['type'] is None:
                old_text, new_text = old['text'], new['text']
                if old_text != new_text and old_text.strip(PHP_WS) != new_text.strip(PHP_WS):
                    old['type'] = '-'
                    new['type'] = '+'

    return rebuild_old, rebuild_new


def has_changes(old_lines, new_lines, any_change):
    """getHasChanges(): 'any' when any_change, otherwise 'text'."""
    for old, new in zip(old_lines, new_lines):
        if old is None or new is None:
            return True
        if old['type'] != new['type']:
            return True
        if old['text'] != new['text']:
            if any_change:
                return True
            if old['text'].strip(PHP_WS) != new['text'].strip(PHP_WS):
                return True
    return False


def is_deleted(old_lines, new_lines):
    if any(new_lines):
        return False
    return any(old_lines)


def intraline_diffs(old_lines, new_lines):
    """generateIntraLineDiffs()"""
    diffs = {}
    for key, (old, new) in enumerate(zip(old_lines, new_lines)):
        if not old or not new:
            continue
        if old['type'] != new['type']:
            diffs[key] = generate_intraline_diff(old['text'], new['text'])
    return diffs


def visible_lines_mask(old_lines, new_lines, lines_context):
    """generateVisibileLinesMask()"""

    def changed(lines, offset):
        return offset < len(lines) and lines[offset] and lines[offset]['type']

    max_length = max(len(old_lines), len(new_lines))
    visible = False
    last = 0
    mask = set()
    for cursor in range(-lines_context, max_length):
        offset = cursor + lines_context
        if changed(old_lines, offset) or changed(new_lines, offset):
            visible = True
            last = offset
        elif cursor > last + lines_context:
            visible = False
        if visible and cursor > 0:
            mask.add(cursor)
    return mask


def corpus(lines):
    """getCorpus()"""
    out = []
    for line in lines:
        if (line['type'] if line else None) != '\\':
            out.append(line['text'] if line else b'\n')
    return out


# -- Intraline edits (arcanist ArcanistDiffUtils, PhutilEditDistanceMatrix) ---


def edit_string(x, y, max_length):
    """generateEditString(): one of s(ame), i(nsert), d(elete) and x (replace) per character, with
    internal smoothing."""
    insert_cost = 1
    delete_cost = 1
    replace_cost = 2
    alter_cost = 1 / (max_length * 2)

    xl, yl = len(x), len(y)
    shortest = min(xl, yl)
    prefix = 0
    while prefix < shortest and x[prefix] == y[prefix]:
        prefix += 1
    suffix = 0
    for ii in range(1, shortest - prefix + 1):
        if x[xl - ii] != y[yl - ii]:
            break
        suffix += 1
    x = x[prefix : xl - suffix]
    y = y[prefix : yl - suffix]

    def pad(s):
        return 's' * prefix + s + 's' * suffix

    if not x and not y:
        return pad('')
    if not x:
        return pad('i' * len(y))
    if not y:
        return pad('d' * len(x))
    if x == y:
        return pad('s' * len(x))
    if len(x) > max_length or len(y) > max_length:
        return pad('d' * len(x) + 'i' * len(y))

    xl, yl = len(x), len(y)
    m = [[0] * (yl + 1) for _ in range(xl + 1)]
    t = [[''] * (yl + 1) for _ in range(xl + 1)]
    for xx in range(xl + 1):
        m[xx][0] = xx * delete_cost
        t[xx][0] = 'd'
    for yy in range(yl + 1):
        m[0][yy] = yy * insert_cost
        t[0][yy] = 'i'
    t[0][0] = 's'

    for xx in range(1, xl + 1):
        for yy in range(1, yl + 1):
            if x[xx - 1] == y[yy - 1]:
                rep_cost = m[xx - 1][yy - 1] + 0
                rep_type = 's'
            else:
                rep_cost = m[xx - 1][yy - 1] + replace_cost
                rep_type = 'x'
            del_cost = m[xx - 1][yy] + delete_cost
            ins_cost = m[xx][yy - 1] + insert_cost
            if t[xx - 1][yy] != 'd':
                del_cost += alter_cost
            if t[xx][yy - 1] != 'i':
                ins_cost += alter_cost
            if t[xx - 1][yy - 1] != rep_type:
                rep_cost += alter_cost

            if rep_cost <= del_cost and rep_cost <= ins_cost:
                cost, kind = rep_cost, rep_type
            elif ins_cost <= del_cost:
                cost, kind = ins_cost, 'i'
            else:
                cost, kind = del_cost, 'd'
            m[xx][yy] = cost
            t[xx][yy] = kind

    xx, yy = xl, yl
    out = []
    while True:
        kind = t[xx][yy]
        if kind in ('s', 'x'):
            xx -= 1
            yy -= 1
        elif kind == 'i':
            yy -= 1
        else:
            xx -= 1
        out.append(kind)
        if xx <= 0 and yy <= 0:
            break
    s = ''.join(reversed(out))

    # SMOOTHING_INTERNAL: short runs of unchanged characters inside a change become replacements.
    while True:
        original = s
        s = re.sub(r'([xdi])(s{3})([xdi])', r'\1xxx\3', s)
        s = re.sub(r'([xdi])(s{2})([xdi])', r'\1xx\3', s)
        s = re.sub(r'([xdi])(s{1})([xdi])', r'\1x\3', s)
        if s == original:
            break
    return pad(s)


def collapse_runs(runs):
    """collapseIntralineRuns()"""
    runs = [list(r) for r in runs]
    keep = [True] * len(runs)
    for ii in range(len(runs) - 1):
        if runs[ii][0] == runs[ii + 1][0]:
            runs[ii + 1][1] += runs[ii][1]
            keep[ii] = False
    return [r for r, k in zip(runs, keep) if k]


def generate_intraline_diff(o, n):
    """generateIntralineDiff(): byte runs of (highlight, length) for the old and the new line."""
    ol, nl = len(o), len(n)
    if o == n or not ol or not nl:
        return [[0, ol]], [[0, nl]]

    max_glyphs = 80
    if ol + nl > 16 * max_glyphs:
        return [[1, ol]], [[1, nl]]

    # computeIntralineEdits()
    if re.search(rb'[\x80-\xff]', o + n):
        ov = utf8v_combined(o)
        nv = utf8v_combined(n)
        multibyte = True
    else:
        ov = [o[i : i + 1] for i in range(ol)]
        nv = [n[i : i + 1] for i in range(nl)]
        multibyte = False

    result = edit_string(ov, nv, max_glyphs)

    o_pos = n_pos = 0
    o_run = []
    n_run = []
    old_char_len = new_char_len = 1
    for c in result:
        if multibyte:
            old_char_len = len(ov[o_pos]) if o_pos < len(ov) else 0
            new_char_len = len(nv[n_pos]) if n_pos < len(nv) else 0
        if c in ('s', 'x'):
            byte_o, byte_n = old_char_len, new_char_len
            o_pos += 1
            n_pos += 1
        elif c == 'i':
            byte_o, byte_n = 0, new_char_len
            n_pos += 1
        else:
            byte_o, byte_n = old_char_len, 0
            o_pos += 1
        if byte_o:
            o_run.append([0 if c == 's' else 1, byte_o])
        if byte_n:
            n_run.append([0 if c == 's' else 1, byte_n])

    return collapse_runs(o_run), collapse_runs(n_run)


def apply_intraline_diff(text, runs):
    """applyIntralineDiff(): wrap changed bytes of highlighted HTML in <span class="bright">,
    stepping over tags and counting each entity as one byte."""
    open_tag = b'<span class="bright">'
    close_tag = b'</span>'
    stack = list(runs)
    buf = []
    p = s = e = 0
    highlight = in_tag = in_ent = False
    n = len(text)
    for i in range(n):
        if p == e:
            while True:
                if not stack:
                    buf.append(text[i:])
                    return b''.join(buf)
                run = stack.pop(0)
                s = e
                e += run[1]
                if run[0] != 0:
                    break

        if not highlight and not in_tag and not in_ent and p == s:
            buf.append(open_tag)
            highlight = True

        ch = text[i : i + 1]
        if ch == b'<':
            in_tag = True
            if highlight:
                buf.append(close_tag)

        if not in_tag:
            if ch == b'&':
                in_ent = True
            if in_ent and ch == b';':
                in_ent = False
            if not in_ent:
                p += 1

        buf.append(ch)

        if in_tag and ch == b'>':
            in_tag = False
            if highlight:
                buf.append(open_tag)

        if highlight and (p == e or i == n - 1):
            buf.append(close_tag)
            highlight = False

    return b''.join(buf)


# -- Copied and moved code (DifferentialChangesetParser::detectCopiedCode) ----


def detect_copied_code(changesets, min_width=30, min_lines=3):
    code_map = {}
    files = {}
    types = {}
    for cs in changesets:
        name = cs.filename
        for hunk in cs.hunks:
            for line, info in hunk.structured_file(b'-').items():
                if info['type'] == b'\\':
                    continue
                types.setdefault(name, {})[line] = info['type']
                text = info['text'].strip(PHP_WS)
                files.setdefault(name, {})[line] = text
                if len(text) >= min_width:
                    code_map.setdefault(text, []).append((name, line))

    for cs in changesets:
        copies = {}
        best_file = best_line = best_offset = None
        for hunk in cs.hunks:
            added_info = hunk.structured_file(b'+')
            atype = {line: info['type'] for line, info in added_info.items()}
            added = {line: info['text'].strip(PHP_WS) for line, info in added_info.items()}

            skip_lines = 0
            for line, code in added.items():
                if skip_lines:
                    skip_lines -= 1
                    continue
                if atype[line] != b'+':
                    continue
                if not code_map.get(code):
                    continue
                if len(code_map[code]) > 16:
                    continue

                best_length = 0
                for name, orig_line in code_map[code]:
                    length = 1
                    for direction in (-1, 1):
                        offset = direction
                        while True:
                            if line + offset in copies:
                                break
                            if line + offset not in added:
                                break
                            if orig_line + offset not in files[name]:
                                break
                            if files[name][orig_line + offset] != added[line + offset]:
                                break
                            length += 1
                            offset += direction
                    if length < best_length:
                        continue
                    if length == best_length:
                        if types[name].get(orig_line) != b'-':
                            continue
                    best_length = length
                    best_offset = offset - 1
                    best_file = name
                    best_line = orig_line

                source = '' if best_file == cs.filename else best_file
                for i in range(best_length - 1, -1, -1):
                    orig = best_line + best_offset - i
                    if best_length < min_lines:
                        copies[line + best_offset - i] = None
                    else:
                        copies[line + best_offset - i] = (source, orig, types[best_file].get(orig))
                skip_lines = best_offset

        cs.copy_lines = {k: v for k, v in copies.items() if v}


# -- Syntax highlighting (libphutil markup/syntax) ----------------------------


def highlight_console(source):
    """PhutilConsoleSyntaxHighlighter"""
    in_command = False
    lines = source.split(b'\n')
    for key, line in enumerate(lines):
        if in_command:
            pattern = rb'()(.*?)(#.*|\\)?$'
        else:
            pattern = rb'^(\S+[\\/] )?([$] .*?)(#.*|\\)?$'
        m = re.search(pattern, line)
        if m:
            comment = m.group(3) or b''
            lines[key] = (
                escape(m.group(1) or b'')
                + b'<span class="gp">'
                + escape(m.group(2))
                + b'</span>'
                + (b'<span class="k">' + escape(comment) + b'</span>' if comment else b'')
            )
            in_command = comment == b'\\'
        else:
            lines[key] = b'<span class="go">' + escape(line) + b'</span>'
    return b'\n'.join(lines)


# PhutilPygmentsSyntaxHighlighter::getPygmentsLexerNameFromLanguageName()
PYGMENTS_NAMES = {
    'adb': 'ada',
    'ads': 'ada',
    'ahkl': 'ahk',
    'as': 'as3',
    'asax': 'aspx-vb',
    'ascx': 'aspx-vb',
    'ashx': 'aspx-vb',
    'ASM': 'nasm',
    'asm': 'nasm',
    'asmx': 'aspx-vb',
    'aspx': 'aspx-vb',
    'autodelegate': 'myghty',
    'autohandler': 'mason',
    'aux': 'tex',
    'axd': 'aspx-vb',
    'b': 'brainfuck',
    'bas': 'vb.net',
    'bf': 'brainfuck',
    'bmx': 'blitzmax',
    'c++': 'cpp',
    'c++-objdump': 'cpp-objdump',
    'cc': 'cpp',
    'cfc': 'cfm',
    'cfg': 'ini',
    'cfml': 'cfm',
    'cl': 'common-lisp',
    'clj': 'clojure',
    'cmd': 'bat',
    'coffee': 'coffee-script',
    'cs': 'csharp',
    'csh': 'tcsh',
    'cw': 'redcode',
    'cxx': 'cpp',
    'cxx-objdump': 'cpp-objdump',
    'darcspatch': 'dpatch',
    'def': 'modula2',
    'dhandler': 'mason',
    'di': 'd',
    'duby': 'rb',
    'dyl': 'dylan',
    'ebuild': 'bash',
    'eclass': 'bash',
    'el': 'common-lisp',
    'eps': 'postscript',
    'erl': 'erlang',
    'erl-sh': 'erl',
    'f': 'fortran',
    'f90': 'fortran',
    'feature': 'Cucumber',
    'fhtml': 'velocity',
    'flx': 'felix',
    'flxh': 'felix',
    'frag': 'glsl',
    'g': 'antlr-ruby',
    'G': 'antlr-ruby',
    'gdc': 'gooddata-cl',
    'gemspec': 'rb',
    'geo': 'glsl',
    'GNUmakefile': 'make',
    'h': 'c',
    'h++': 'cpp',
    'hh': 'cpp',
    'hpp': 'cpp',
    'hql': 'sql',
    'hrl': 'erlang',
    'hs': 'haskell',
    'htaccess': 'apacheconf',
    'htm': 'html',
    'html': 'html+evoque',
    'hxx': 'cpp',
    'hy': 'hybris',
    'hyb': 'hybris',
    'ik': 'ioke',
    'inc': 'pov',
    'j': 'objective-j',
    'jbst': 'duel',
    'kid': 'genshi',
    'ksh': 'bash',
    'less': 'css',
    'lgt': 'logtalk',
    'lisp': 'common-lisp',
    'll': 'llvm',
    'm': 'objective-c',
    'mak': 'make',
    'Makefile': 'make',
    'makefile': 'make',
    'man': 'groff',
    'mao': 'mako',
    'mc': 'mason',
    'md': 'minid',
    'mhtml': 'mason',
    'mi': 'mason',
    'ml': 'ocaml',
    'mli': 'ocaml',
    'mll': 'ocaml',
    'mly': 'ocaml',
    'mm': 'objective-c',
    'mo': 'modelica',
    'mod': 'modula2',
    'moo': 'moocode',
    'mu': 'mupad',
    'myt': 'myghty',
    'ns2': 'newspeak',
    'pas': 'delphi',
    'patch': 'diff',
    'phtml': 'html+php',
    'pl': 'prolog',
    'plot': 'gnuplot',
    'plt': 'gnuplot',
    'pm': 'perl',
    'po': 'pot',
    'pp': 'puppet',
    'pro': 'prolog',
    'proto': 'protobuf',
    'ps': 'postscript',
    'pxd': 'cython',
    'pxi': 'cython',
    'py': 'python',
    'pyw': 'python',
    'pyx': 'cython',
    'R': 'splus',
    'r': 'rebol',
    'r3': 'rebol',
    'rake': 'rb',
    'Rakefile': 'rb',
    'rbw': 'rb',
    'rbx': 'rb',
    'rest': 'rst',
    'rl': 'ragel-em',
    'robot': 'robotframework',
    'Rout': 'rconsole',
    'rss': 'xml',
    's': 'gas',
    'S': 'splus',
    'sc': 'python',
    'scm': 'scheme',
    'SConscript': 'python',
    'SConstruct': 'python',
    'scss': 'css',
    'sh': 'bash',
    'sh-session': 'console',
    'spt': 'cheetah',
    'sqlite3-console': 'sqlite3',
    'st': 'smalltalk',
    'sv': 'v',
    'tac': 'python',
    'tmpl': 'cheetah',
    'toc': 'tex',
    'tpl': 'smarty',
    'txt': 'text',
    'vapi': 'vala',
    'vb': 'vb.net',
    'vert': 'glsl',
    'vhd': 'vhdl',
    'vimrc': 'vim',
    'vm': 'velocity',
    'weechatlog': 'irc',
    'wlua': 'lua',
    'wsdl': 'xml',
    'xhtml': 'html',
    'xml': 'xml+evoque',
    'xqy': 'xquery',
    'xsd': 'xml',
    'xsl': 'xslt',
    'xslt': 'xml',
    'yml': 'yaml',
}


def highlight_pygments(language, source):
    """PhutilPygmentsSyntaxHighlighter. Phabricator runs pygmentize and shows plain text when it
    fails, as with an unknown lexer name."""
    if re.search(rb'\r(?!\n)', source):
        return escape(source)
    try:
        import pygments
        import pygments.formatters
        import pygments.lexers
        import pygments.util
    except ImportError:
        return escape(source)
    name = PYGMENTS_NAMES.get(language, language)
    try:
        lexer = pygments.lexers.get_lexer_by_name(name, stripnl=False)
    except pygments.util.ClassNotFound:
        return escape(source)
    # Pygments only highlights PHP after "<?php", so a fragment without one gets it added, and the
    # line it makes is dropped again afterwards.
    scrub = name == 'php' and b'<?' not in source
    if scrub:
        source = b'<?php\n' + source
    out = pygments.highlight(
        source.decode('utf-8', 'replace'), lexer, pygments.formatters.HtmlFormatter()
    )
    m = re.match(r'^<div class="highlight"><pre>(.*)</pre></div>\s*$', out, re.S)
    if m:
        out = m.group(1)
    if scrub:
        out = re.sub(r'^.*\n', '', out, count=1)
    return out.encode()


# PhabricatorSyntaxHighlightingConfigOptions, "syntax.filemap", then the default map in
# PhutilDefaultSyntaxHighlighterEngine.
FILENAME_MAP = [
    (r'\.arcconfig$', 'json'),
    (r'\.arclint$', 'json'),
    (r'\.divinerconfig$', 'json'),
    (r'\.([^./]+)$', 1),
]


def language_from_filename(filename):
    for regex, language in FILENAME_MAP:
        m = re.search(regex, filename)
        if m:
            return m.group(language) if isinstance(language, int) else language
    return None


def guess_language(source):
    """PhutilLanguageGuesser"""
    patterns = [
        (rb'^#!.*bin/env\s+(\S+)', 1),
        (rb'^#!.*bin/(\S+)', 1),
        (rb'^\s*<[?](php)', 1),
        (rb'(?m)^.*-[*]-.*mode\s*:\s*(\S+).*-[*]-.*$', 1),
        (rb'(?m)^---.*$\n^[+]{3}.*$\n^@@', 'diff'),
        (rb'^diff --git', 'diff'),
        (rb'^(?:\S+[\\/] )?[$] ', 'console'),
    ]
    for regex, language in patterns:
        m = re.search(regex, source)
        if m:
            if isinstance(language, int):
                return m.group(language).decode('utf-8', 'replace')
            return language
    return None


def highlight(language, source):
    """PhutilDefaultSyntaxHighlighterEngine::getHighlightFuture(). Phabricator has lexers of its own
    for Python, PHP and JSON, which differ leaves to Pygments like every other language. The
    diviner, remarkup, rainbow and invisible highlighters are shown as plain text here."""
    if language is None:
        language = guess_language(source)
    if language == 'console':
        return highlight_console(source)
    if language in ('diviner', 'remarkup', 'rainbow', 'invisible'):
        return escape(source)
    if language and language not in ('text', 'txt'):
        return highlight_pygments(language, source)
    return escape(source)


# -- Processing a changeset (DifferentialChangesetParser::process) ------------


def whitespace_type_maps(old_file, new_file):
    """Diff the whole files again with `diff -bw`, as PhabricatorDifferenceEngine does, and return
    the line types it gives each side."""
    with tempfile.NamedTemporaryFile() as old_tmp, tempfile.NamedTemporaryFile() as new_tmp:
        old_tmp.write(old_file)
        old_tmp.flush()
        new_tmp.write(new_file)
        new_tmp.flush()
        proc = subprocess.run(
            [
                'diff',
                '-bw',
                '-U65535',
                '-L',
                '/dev/universe 9999-99-99',
                '-L',
                '/dev/universe 9999-99-99',
                old_tmp.name,
                new_tmp.name,
            ],
            stdout=subprocess.PIPE,
        )
    if proc.returncode == 0:
        # The same apart from whitespace: every line is unchanged.
        return {}, {}
    if proc.returncode != 1:
        raise DiffError('diff -bw failed')

    # "diff -bw" is inconsistent about "\ No newline at end of file", so Phabricator drops those
    # lines.
    kept = [l for l in proc.stdout.split(b'\n') if l[:1] != b'\\']
    lines = split_lines(b'\n'.join(kept))
    i = 0
    while i < len(lines) and not HUNK_HEADER.match(lines[i]):
        i += 1
    hunks, _ = parse_hunks(lines, i)
    old_lines, new_lines = parse_line_data(hunks)
    return line_type_map(old_lines), line_type_map(new_lines)


class Processed:
    pass


def get_highlighted(filename, corpus_block):
    """getHighlightFuture() and processHighlightedSource()'s input."""
    language = language_from_filename(filename)
    disabled = False
    if language != 'txt' and len(corpus_block) > HIGHLIGHT_BYTE_LIMIT:
        disabled = True
        language = 'txt'
    return highlight(language, corpus_block), disabled


def process(cs, mode):
    ignore_all = mode == IGNORE_MOST
    if ignore_all and any(re.search(r, cs.filename) for r in WHITESPACE_MATTERS):
        ignore_all = False
    if ignore_all:
        if len(cs.hunks) != 1 or cs.hunks[0].old_offset != 1 or cs.hunks[0].new_offset != 1:
            ignore_all = False
    if ignore_all:
        old_file = cs.make_old_file()
        new_file = cs.make_new_file()
        if old_file == new_file:
            ignore_all = False

    old_lines, new_lines = parse_line_data(cs.hunks)
    if ignore_all:
        old_map, new_map = whitespace_type_maps(old_file, new_file)
        apply_line_type_map(old_lines, old_map)
        apply_line_type_map(new_lines, new_map)

    old_lines, new_lines = reparse_hunks(old_lines, new_lines, mode)

    p = Processed()
    p.old = old_lines
    p.new = new_lines
    p.unchanged = not has_changes(old_lines, new_lines, True) and cs.file_type == 'text'
    p.deleted = is_deleted(old_lines, new_lines)
    p.whitespace_only = not has_changes(old_lines, new_lines, False)
    p.move_away = cs.change_type == 'move-away'
    p.intra = intraline_diffs(old_lines, new_lines)
    p.visible = visible_lines_mask(old_lines, new_lines, CONTEXT_LINES)
    p.hunk_starts = {h.old_offset for h in cs.hunks if h.old_offset > 1}

    new_corpus = corpus(new_lines)
    new_block = b''.join(new_corpus)
    p.generated = b'@generated' in new_block

    old_block = b''.join(corpus(old_lines))
    old_html, old_disabled = get_highlighted(cs.filename, old_block)
    new_html, new_disabled = get_highlighted(cs.filename, new_block)
    p.highlighting_disabled = old_disabled or new_disabled

    p.old_render = {}
    p.new_render = {}
    for side, html, render in (
        (old_lines, old_html, p.old_render),
        (new_lines, new_html, p.new_render),
    ):
        for key, line in enumerate(split_lines(html)):
            if key < len(side) and side[key]:
                render[key] = line

    for key, (old_runs, new_runs) in p.intra.items():
        if key in p.old_render:
            p.old_render[key] = apply_intraline_diff(p.old_render[key], old_runs)
        if key in p.new_render:
            p.new_render[key] = apply_intraline_diff(p.new_render[key], new_runs)
    return p


# -- Rendering (DifferentialChangesetHTMLRenderer, TwoUpRenderer) -------------

CHANGE_MESSAGES = {
    'add': 'This file was added.',
    'delete': 'This file was deleted.',
    'move-here': 'This file was moved from %s.',
    'copy-here': 'This file was copied from %s.',
    'move-away': 'This file was moved to %s.',
    'copy-away': 'This file was copied to %s.',
    'multicopy': 'This file was deleted after being copied to %s.',
}

BINARY_MESSAGES = {
    'add': 'This binary file was added.',
    'delete': 'This binary file was deleted.',
    'move-here': 'This binary file was moved from %s.',
    'copy-here': 'This binary file was copied from %s.',
    'move-away': 'This binary file was moved to %s.',
    'copy-away': 'This binary file was copied to %s.',
    'multicopy': 'This binary file was deleted after being copied to %s.',
}


def format_header_messages(messages):
    if not messages:
        return b''
    return tag('ul', [('class', 'differential-meta-notice')], [tag('li', [], m) for m in messages])


def render_change_type_header(cs, force):
    """renderChangeTypeHeader()"""
    messages = BINARY_MESSAGES if cs.file_type == 'binary' else CHANGE_MESSAGES
    template = messages.get(cs.change_type)
    if template is None:
        if cs.file_type == 'binary':
            template = 'This is a binary file.'
        elif force:
            template = 'This file was not modified.'
        else:
            return b''
    if '%s' in template:
        if cs.change_type in ('move-here', 'copy-here'):
            paths = cs.old_file or ''
        else:
            paths = ', '.join(cs.away_paths)
        before, after = template.split('%s')
        message = escape(before) + tag('strong', [], escape(paths)) + escape(after)
    else:
        message = escape(template)
    return format_header_messages([message])


def render_property_change_header(cs):
    """renderPropertyChangeHeader(), for file modes."""
    old = dict(cs.old_props)
    new = dict(cs.new_props)
    if cs.change_type == 'add' and new == {'unix:filemode': '100644'}:
        del new['unix:filemode']
    if cs.change_type == 'delete' and old == {'unix:filemode': '100644'}:
        del old['unix:filemode']
    if old == new:
        return b''
    names = {'unix:filemode': 'File Mode'}
    rows = []
    for key in sorted(set(old) | set(new)):
        oval, nval = old.get(key), new.get(key)
        if oval == nval:
            continue
        rows.append(
            tag(
                'tr',
                [],
                [
                    tag('td', [], escape(names.get(key, key))),
                    tag(
                        'td',
                        [('class', 'oval')],
                        tag('em', [], b'null') if oval is None else escape(oval),
                    ),
                    tag(
                        'td',
                        [('class', 'nval')],
                        tag('em', [], b'null') if nval is None else escape(nval),
                    ),
                ],
            )
        )
    # AphrontTableView, with column classes '', 'oval' and 'nval'.
    header = tag(
        'tr',
        [],
        [
            tag('th', [], b'Property'),
            tag('th', [('class', 'oval')], b'Old Value'),
            tag('th', [('class', 'nval')], b'New Value'),
        ],
    )
    table = tag(
        'div',
        [('class', 'aphront-table-wrap')],
        tag('table', [('class', 'aphront-table-view')], [header] + rows),
    )
    return tag('div', [('class', 'differential-property-table')], table)


def render_undershield_header(highlighting_disabled):
    if not highlighting_disabled:
        return b''
    return format_header_messages(
        [escape('This file is larger than 256 KB, so syntax highlighting is disabled by default.')]
    )


def wrap_change_in_table(content, rows=None):
    if not content:
        return b''
    colgroup = tag(
        'colgroup',
        [],
        [
            tag('col', [('class', 'num')]),
            tag('col', [('class', 'left')]),
            tag('col', [('class', 'num')]),
            tag('col', [('class', 'copy')]),
            tag('col', [('class', 'right')]),
            tag('col', [('class', 'cov')]),
        ],
    )
    return tag(
        'table',
        [
            ('class', 'differential-diff remarkup-code PhabricatorMonospaced diff-2up'),
            ('data-rows', rows),
        ],
        [colgroup, content],
    )


def render_shield(message, link):
    content = [escape(message)]
    if link:
        content += [
            b' ',
            tag(
                'a',
                [('href', '#'), ('class', 'complete'), ('data-show', 'file')],
                b'Show File Contents',
            ),
        ]
    return wrap_change_in_table(
        tag('tr', [], tag('td', [('class', 'differential-shield'), ('colspan', 6)], content))
    )


def show_context_links(top, length, rows):
    """renderShowContextLinks(). The "Show %d Line(s)" after the down arrow has no translation in
    Phabricator, so it stays as written."""
    block_size = 20
    is_large_block = length > block_size * 2
    links = []
    if is_large_block:
        text = ('Show First %d Lines' if top == 0 else '▲ Show %d Lines') % block_size
        links.append(tag('a', [('href', '#'), ('data-show', 'top')], escape(text)))
    links.append(
        tag('a', [('href', '#'), ('data-show', 'all')], escape('Show All %d Lines' % length))
    )
    if is_large_block:
        text = ('Show Last %d Lines' if top + length >= rows else '▼ Show %d Line(s)') % block_size
        links.append(tag('a', [('href', '#'), ('data-show', 'bottom')], escape(text)))
    return escape(' • ').join(links)


def gaps_and_mask(p, mask_force, range_start, range_len):
    """calculateGapsMaskAndDepths()"""
    gaps = []
    gap_start = 0
    in_gap = False
    base_mask = set(p.visible) | set(mask_force)
    base_mask.add(range_start + range_len)
    for ii in range(range_start, range_start + range_len + 1):
        if ii in base_mask:
            if in_gap:
                gap_length = ii - gap_start
                if gap_length <= CONTEXT_LINES:
                    base_mask.update(range(gap_start, gap_start + gap_length + 1))
                else:
                    gaps.append((gap_start, gap_length))
                in_gap = False
        elif not in_gap:
            gap_start = ii
            in_gap = True
    gaps.reverse()

    depths = {}
    last_depth = 0
    range_end = range_start + range_len
    if range_end >= len(p.new) or not p.new[range_end]:
        range_end -= 1
    for ii in range(range_end, range_start - 1, -1):
        new = p.new[ii] if ii < len(p.new) else None
        line = (new['text'] if new else b'').replace(b'\t', b'  ')
        trimmed = line.lstrip(PHP_WS)
        if trimmed != b'':
            last_depth = (len(line) - len(trimmed)) // 2
        depths[ii] = last_depth
    return gaps, base_mask, depths


def render_text_change(cs, p, mask, gaps, depths, range_start, range_len, rows):
    """DifferentialChangesetTwoUpRenderer::renderTextChange(). Lines in a gap are rendered hidden
    after the gap's row, so the page can show them without asking a server."""
    context_not_available = None
    if p.hunk_starts:
        context_not_available = tag(
            'tr', [], tag('td', [('colspan', 6), ('class', 'show-more')], b'Context not available.')
        )

    html = []
    gap_end = -1
    ii = range_start
    while ii < range_start + range_len:
        hidden = ii < gap_end
        if ii not in mask and not hidden:
            top, length = gaps.pop()
            context = None
            context_line = None
            if not (ii + length >= rows) and depths.get(ii + length):
                for l in range(ii + length - 1, ii - 1, -1):
                    new = p.new[l]
                    text = new['text'] if new else b''
                    if depths[l] < depths[ii + length] and text.strip(PHP_WS) != b'':
                        context = p.new_render.get(l)
                        context_line = new['line']
                        break
            html.append(
                tag(
                    'tr',
                    [('data-top', top), ('data-len', length)],
                    [
                        tag(
                            'td',
                            [('colspan', 2), ('class', 'show-more')],
                            show_context_links(top, length, rows),
                        ),
                        tag(
                            'th',
                            [('class', 'show-context-line')],
                            escape(str(context_line)) if context_line else b'',
                        ),
                        tag('td', [('colspan', 3), ('class', 'show-context')], context),
                    ],
                )
            )
            gap_end = ii + length
            hidden = True

        old = p.old[ii] if ii < len(p.old) else None
        new = p.new[ii] if ii < len(p.new) else None

        o_num = None
        o_classes = ''
        o_text = None
        if old:
            o_num = old['line']
            o_text = p.old_render.get(ii)
            if old['type']:
                if old['type'] == '\\':
                    o_text = escape(old['text'])
                    o_classes = 'comment'
                elif not new:
                    o_classes = 'old old-full'
                else:
                    o_classes = 'old'

        n_copy = tag('td', [('class', 'copy')])
        n_classes = ''
        n_num = None
        n_text = None
        if new:
            n_num = new['line']
            n_text = p.new_render.get(ii)
            if new['type']:
                if new['type'] == '\\':
                    n_text = escape(new['text'])
                    n_classes = 'comment'
                elif not old:
                    n_classes = 'new new-full'
                else:
                    n_classes = 'new'
                copy = cs.copy_lines.get(n_num)
                if new['type'] == '\\' or not copy:
                    n_copy = tag('td', [('class', 'copy ' + n_classes)])
                else:
                    orig_file, orig_line, orig_type = copy
                    title = ('Moved' if orig_type == b'-' else 'Copied') + ' from '
                    if orig_file == '':
                        title += 'line %d' % orig_line
                    else:
                        title += '%s:%d in dir %s' % (
                            os.path.basename(orig_file),
                            orig_line,
                            os.path.dirname('/' + orig_file),
                        )
                    kind = 'new-move' if orig_type == b'-' else 'new-copy'
                    n_copy = tag('td', [('class', 'copy ' + kind), ('title', title)])

        if context_not_available and o_num in p.hunk_starts:
            html.append(context_not_available)

        blank = not (new and new['text'].strip(PHP_WS))
        html.append(
            tag(
                'tr',
                [
                    ('class', 'differ-hidden' if hidden else None),
                    ('data-r', ii),
                    ('data-d', depths.get(ii, 0)),
                    ('data-b', 1 if blank else None),
                ],
                [
                    tag('th', [('class', o_classes)], escape(str(o_num)) if o_num else b''),
                    tag('td', [('class', o_classes)], o_text),
                    tag('th', [('class', n_classes)], escape(str(n_num)) if n_num else b''),
                    n_copy,
                    tag(
                        'td',
                        [('class', n_classes), ('colspan', 2)],
                        [
                            tag('span', [('class', 'zwsp')], '​'.encode()),
                            n_text,
                        ],
                    ),
                ],
            )
        )

        if context_not_available and ii == rows - 1:
            html.append(context_not_available)
        ii += 1

    return wrap_change_in_table(b''.join(html), rows)


def render_body(cs, p, mask_force=()):
    rows = max(len(p.old), len(p.new))
    gaps, mask, depths = gaps_and_mask(p, mask_force, 0, rows)
    return render_text_change(cs, p, mask, gaps, depths, 0, rows, rows)


def render_changeset(cs):
    """DifferentialChangesetParser::render() and renderChangesetTable() at the top level. A shielded
    file gets its contents rendered too, hidden until "Show File Contents" is clicked."""
    props = render_property_change_header(cs)

    if cs.file_type != 'text':
        notice = render_change_type_header(cs, not props)
        return (notice + props).replace(b'\t', b'  ')

    p = process(cs, IGNORE_MOST)
    rows = max(len(p.old), len(p.new))

    shield = None
    unshielded = None
    if p.generated:
        shield = render_shield(
            'This file contains generated code, which does not normally need to be reviewed.', True
        )
        unshielded = render_body(cs, p)
    elif p.move_away:
        shield = b''
    elif p.unchanged:
        if cs.change_type == 'add':
            message = 'This is an empty file.'
        else:
            message = 'The contents of this file were not changed.'
        shield = render_shield(message, bool(rows))
        unshielded = render_body(cs, p, range(rows + 1))
    elif p.whitespace_only:
        shield = render_shield('This file was changed only by adding or removing whitespace.', True)
        p = process(cs, SHOW_ALL)
        unshielded = render_body(cs, p)
    elif p.deleted:
        shield = render_shield('This file was completely deleted.', True)
        unshielded = render_body(cs, p)
    elif cs.affected_line_count() > 2500:
        shield = render_shield(
            'This file has a very large number of changes (%s lines).'
            % format(cs.affected_line_count(), ','),
            True,
        )
        unshielded = render_body(cs, p)

    if shield is not None:
        notice = render_change_type_header(cs, not shield and not props)
        body = shield
        if unshielded:
            undershield = render_undershield_header(p.highlighting_disabled)
            body = tag('div', [('class', 'differ-shielded')], shield) + tag(
                'div', [('class', 'differ-unshielded')], undershield + unshielded
            )
        return (notice + props + body).replace(b'\t', b'  ')

    content = render_body(cs, p)
    notice = render_change_type_header(cs, not content and not props)
    undershield = render_undershield_header(p.highlighting_disabled)
    return (notice + props + undershield + content).replace(b'\t', b'  ')


def file_icon(filename):
    """FileTypeIcon::getFileIcon(), by the extension as PHP's pathinfo() finds it: whatever follows
    the last dot in the file's name."""
    name = filename.rsplit('/', 1)[-1]
    extension = name.rsplit('.', 1)[1] if '.' in name else None
    for icon, extensions in (
        ('fa-file-image-o', ('psd', 'ai')),
        ('fa-wrench', ('conf',)),
        ('fa-file-sound-o', ('wav', 'mp3', 'aiff')),
        ('fa-file-movie-o', ('m4v', 'mov')),
        ('fa-database', ('sql', 'db')),
        ('fa-file-excel-o', ('xls', 'csv')),
        ('fa-calendar', ('ics',)),
        ('fa-file-archive-o', ('zip', 'tar', 'bz', 'tgz', 'gz')),
        ('fa-file-picture-o', ('png', 'jpg', 'bmp', 'gif')),
        ('fa-file-text-o', ('txt',)),
        ('fa-file-word-o', ('doc', 'docx')),
        ('fa-file-pdf-o', ('pdf',)),
    ):
        if extension in extensions:
            return icon
    return 'fa-file-text-o'


def render_detail(cs):
    """DifferentialChangesetDetailView, without the icon and buttons. The PhabricatorAnchorView is
    what the file list links to. The banner path goes in data attributes for script.js, split with
    PHP's dirname(), which gives "." for a file at the root."""
    anchor = anchor_name(cs)
    directory, _, name = cs.filename.rpartition('/')
    return tag(
        'div',
        [
            ('class', 'differential-changeset differential-changeset-immutable'),
            ('data-banner-path', (directory or '.') + '/'),
            ('data-banner-file', name),
        ],
        [
            tag('a', [('name', anchor), ('id', anchor), ('class', 'phabricator-anchor-view')]),
            tag(
                'h1',
                [('class', 'differential-file-icon-header')],
                [
                    # PHUIIconView
                    tag(
                        'span',
                        [
                            (
                                'class',
                                'visual-only phui-icon-view phui-font-fa ' + file_icon(cs.filename),
                            ),
                            ('aria-hidden', 'true'),
                        ],
                    ),
                    escape(cs.filename),
                ],
            ),
            tag('div', [('class', 'changeset-view-content')], render_changeset(cs)),
        ],
    )


# -- The file list (PHUIDiffTableOfContentsListView, ItemView) ----------------

# DifferentialChangeType
CHANGE_CHARACTERS = {
    'add': 'A',
    'change': 'M',
    'delete': 'D',
    'move-away': 'V',
    'copy-away': 'P',
    'move-here': 'V',
    'copy-here': 'P',
    'multicopy': 'P',
}
CHANGE_COLORS = {
    'add': 'green',
    'change': 'black',
    'delete': 'red',
    'move-away': 'orange',
    'copy-away': 'black',
    'move-here': 'green',
    'copy-here': 'green',
    'multicopy': 'orange',
}
CHANGE_NAMES = {
    'add': 'Added',
    'change': 'Modified',
    'delete': 'Deleted',
    'move-away': 'Moved Away',
    'copy-away': 'Copied Away',
    'move-here': 'Moved Here',
    'copy-here': 'Copied Here',
    'multicopy': 'Deleted After Multiple Copy',
}
FILE_TYPE_SHORT_NAMES = {'text': None, 'binary': 'bin'}
OLD_LOCATION_TYPES = ('move-away', 'copy-away', 'multicopy')


def digest_for_anchor(string):
    """PhabricatorHash::digestForAnchor()"""
    digest = hashlib.sha1(string.encode()).digest()
    alphabet = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
    result = ''
    accum = 0
    for byte in digest[:12]:
        low_bits = byte & 0x3F
        accum = (accum + byte) % len(alphabet)
        result += alphabet[low_bits] if low_bits < len(alphabet) else alphabet[accum]
    return result


def anchor_name(cs):
    return 'change-' + digest_for_anchor(cs.filename)


def php_slice(items, offset, length):
    """PHP's array_slice(), where a negative length stops that many items before the end."""
    if length < 0:
        return items[offset : len(items) + length]
    return items[offset : offset + length]


def render_rename(name, other, arrow):
    """renderRename(): "dir/{old -> new}" from two paths."""
    old = name.split('/')
    new = other.split('/')

    start = len(old)
    for index, part in enumerate(old):
        if index >= len(new) or part != new[index]:
            start = index
            break

    end = len(old)
    for from_end, part in enumerate(reversed(old)):
        index = len(new) - from_end - 1
        if not 0 <= index < len(new) or part != new[index]:
            end = from_end
            break

    rename = (
        '{'
        + '/'.join(php_slice(old, start, len(old) - end - start))
        + ' '
        + arrow
        + ' '
        + '/'.join(php_slice(new, start, len(new) - end - start))
        + '}'
    )

    length = len(new) - end - start
    stop = len(new) + length if length < 0 else start + length
    new[start:stop] = [rename]
    return '/'.join(new)


def render_toc_path(cs):
    """renderChangesetLink(), renderChangesetLines() and renderChangesetMetadata()."""
    name = cs.filename
    if cs.change_type in OLD_LOCATION_TYPES:
        if len(cs.away_paths) == 1 and cs.change_type == 'move-away':
            name = render_rename(name, cs.away_paths[0], '→')
    elif cs.change_type == 'move-here':
        name = render_rename(name, cs.old_file, '←')
    out = [tag('a', [('href', '#' + anchor_name(cs))], escape(name))]

    lines = cs.affected_line_count()
    if lines:
        out.append(escape(' (%d line)' % lines if lines == 1 else ' (%d lines)' % lines))

    meta = []
    if cs.change_type in OLD_LOCATION_TYPES:
        if len(cs.away_paths) > 1:
            if cs.change_type == 'multicopy':
                meta.append('Deleted after being copied to multiple locations:')
            else:
                meta.append('Copied to multiple locations:')
            meta += cs.away_paths
        elif cs.change_type != 'move-away':
            meta.append('Copied to %s' % cs.away_paths[0])
    elif cs.change_type == 'copy-here':
        meta.append('Copied from %s' % cs.old_file)
    if meta:
        out.append(
            tag(
                'div', [('class', 'differential-toc-meta')], b'<br />'.join(escape(m) for m in meta)
            )
        )
    return b''.join(out)


def render_table_of_contents(changesets):
    """The list of files above the diff, as the diff page renders it: a bare AphrontTableView. A
    pasted diff has no context, coverage or owners packages, so of the optional columns only the
    empty "Packages" one shows."""
    columns = [
        ('differential-toc-char center', None),
        ('differential-toc-prop center', None),
        ('differential-toc-ftype center', None),
        ('differential-toc-file wide', 'Path'),
        (None, 'Packages'),
    ]
    rows = [
        tag(
            'tr',
            [],
            [
                tag('th', [('class', cls)], escape(header) if header else b'')
                for cls, header in columns
            ],
        )
    ]

    for num, cs in enumerate(changesets):
        char = tag(
            'span',
            [
                ('title', CHANGE_NAMES.get(cs.change_type, 'Unknown')),
                ('class', 'phui-text-' + CHANGE_COLORS.get(cs.change_type, 'black')),
            ],
            escape(CHANGE_CHARACTERS.get(cs.change_type, '~')),
        )
        prop = None
        if cs.old_props != cs.new_props:
            prop = tag('span', [('title', 'Properties Modified')], b'M')
        file_type = FILE_TYPE_SHORT_NAMES.get(cs.file_type, '???')
        cells = [
            char,
            prop,
            escape('(%s)' % file_type) if file_type else None,
            render_toc_path(cs),
            None,
        ]
        rows.append(
            tag(
                'tr',
                [('class', 'alt' if num % 2 else None)],
                [tag('td', [('class', cls)], cell) for (cls, _), cell in zip(columns, cells)],
            )
        )

    return tag(
        'div',
        [('class', 'aphront-table-wrap')],
        tag('table', [('class', 'aphront-table-view')], rows),
    )


# -- The page -----------------------------------------------------------------


def font_faces():
    """The fonts, embedded in the page: Lato, and the file icons cut down from Font Awesome."""
    faces = []
    for family, name, kind, weight in (
        ('Lato', 'lato-regular.woff2', 'woff2', 'normal'),
        ('Lato', 'lato-bold.woff2', 'woff2', 'bold'),
        ('differ icons', 'icons.woff', 'woff', 'normal'),
    ):
        with open(os.path.join(HERE, 'fonts', name), 'rb') as f:
            data = base64.b64encode(f.read()).decode()
        faces.append(
            "@font-face {\n  font-family: '%s';\n"
            "  src: url(data:font/%s;base64,%s) format('%s');\n"
            '  font-weight: %s;\n  font-style: normal;\n}\n' % (family, kind, data, kind, weight)
        )
    return ''.join(faces)


def render_page(diff, title):
    """The whole page for the output of `git diff`."""
    diff = diff.decode('utf-8', 'replace').encode()
    changesets = parse_git_diff(diff)
    detect_copied_code(changesets)
    changesets.sort(key=Changeset.sort_key)

    with open(os.path.join(HERE, 'style.css')) as f:
        css = f.read()
    with open(os.path.join(HERE, 'script.js')) as f:
        script = f.read()

    body = [render_detail(cs) for cs in changesets]
    return b''.join(
        [
            b'<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n',
            tag('title', [], escape(title)),
            b'\n',
            tag('style', [], (font_faces() + css).encode()),
            b'\n',
            b'</head>\n<body>\n',
            tag(
                'div',
                [('class', 'differ-page')],
                [
                    render_table_of_contents(changesets),
                    tag('div', [('class', 'differ-box')], body),
                ],
            ),
            b'\n',
            tag('script', [], script.encode()),
            b'\n</body>\n</html>\n',
        ]
    )


def main(argv):
    command = [
        'git',
        '-c',
        'core.quotePath=false',
        'diff',
        '--no-color',
        '--no-ext-diff',
        '--src-prefix=a/',
        '--dst-prefix=b/',
        '-U100000',
    ] + argv
    proc = subprocess.run(command, stdout=subprocess.PIPE)
    if proc.returncode != 0:
        return proc.returncode
    if not proc.stdout.strip():
        print('differ: no changes', file=sys.stderr)
        return 1

    title = ' '.join(['differ'] + argv)
    page = render_page(proc.stdout, title)

    # Named after the repository and a hash of the diff, so the same diff always makes the same
    # file.
    top = (
        subprocess.run(['git', 'rev-parse', '--show-toplevel'], stdout=subprocess.PIPE)
        .stdout.decode()
        .strip()
    )
    repo = re.sub(r'[^\w.-]+', '_', os.path.basename(top)) or 'repo'
    # 6 bytes of the hash in URL-safe base64, which uses "-" and "_" rather than "+" and "/":
    # exactly 8 characters, with no padding.
    digest = base64.urlsafe_b64encode(hashlib.sha1(proc.stdout).digest()[:6]).decode()
    path = os.path.join(tempfile.gettempdir(), 'differ-%s-%s.html' % (repo, digest))
    with open(path, 'wb') as f:
        f.write(page)
    subprocess.Popen(
        ['xdg-open', path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except DiffError as e:
        print('differ: %s' % e, file=sys.stderr)
        sys.exit(1)
