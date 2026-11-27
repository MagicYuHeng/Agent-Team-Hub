"""Stateless text normalization, clause similarity, inline diff, and AGENTS.md block utilities."""

import difflib
import hashlib
import re


SHARED_START = '<!-- SHARED-START -->'
SHARED_END = '<!-- SHARED-END -->'

GROUP_LABELS = {
    'root': '容器根',
    'shared': '主项目共享组',
    'standalone': '独立工作树',
}

_PATH_TOKEN_RE = re.compile(
    r'(?:\.{1,2}[/\\]|%[A-Za-z_][A-Za-z0-9_]*%[/\\]|[A-Za-z]:[/\\])[^\s，。；：、（）()\[\]\'"`]+'
)
_CODE_TOKEN_RE = re.compile(r'`([^`\n]+)`')
_PAREN_VAR_RE = re.compile(r'[（(][^（）()\n]{1,24}[）)]')
_BULLET_PREFIX_RE = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+')
_SENTENCE_SPLIT_RE = re.compile(r'[^。！？；\n]+[。！？；]?')


def _normalize_plain(text):
    s = _BULLET_PREFIX_RE.sub('', (text or '').strip())
    s = s.replace('`', '').replace('**', '').replace('__', '')
    s = re.sub(r'\s+', '', s).lower()
    return s


def _normalize_skeleton(text):
    s = _BULLET_PREFIX_RE.sub('', (text or '').strip())
    s = _CODE_TOKEN_RE.sub(
        lambda m: '<PATH>' if any(ch in m.group(1) for ch in ('/', '\\', '%')) else '<CODE>',
        s,
    )
    s = _PATH_TOKEN_RE.sub('<PATH>', s)
    s = _PAREN_VAR_RE.sub('<VAR>', s)
    s = s.replace('**', '').replace('__', '')
    s = re.sub(r'\s+', '', s).lower()
    return s


def _bigram_jaccard(a, b):
    if not a or not b:
        return 0.0
    if len(a) < 2 or len(b) < 2:
        sa, sb = set(a), set(b)
    else:
        sa = {a[i:i + 2] for i in range(len(a) - 1)}
        sb = {b[i:i + 2] for i in range(len(b) - 1)}
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def _clause_similarity(query, candidate):
    q_plain = _normalize_plain(query)
    c_plain = _normalize_plain(candidate)
    if not q_plain or not c_plain:
        return 0.0
    if q_plain == c_plain:
        return 1.0

    # Exact substring match when lengths are comparable.
    sub_score = 0.0
    if len(q_plain) >= 6 and (q_plain in c_plain or c_plain in q_plain):
        ratio = min(len(q_plain), len(c_plain)) / max(len(q_plain), len(c_plain))
        if ratio >= 0.45:
            sub_score = max(0.82, ratio)

    sm_plain = difflib.SequenceMatcher(None, q_plain, c_plain)
    plain_ratio = sm_plain.ratio()
    plain_bi = _bigram_jaccard(q_plain, c_plain)
    plain_score = 0.55 * plain_ratio + 0.45 * plain_bi

    # Containment boost when query matches a contiguous span inside a moderately longer candidate.
    if len(q_plain) >= 8 and len(c_plain) <= len(q_plain) * 2.2:
        matched_chars = sum(block.size for block in sm_plain.get_matching_blocks())
        contain = matched_chars / len(q_plain)
        len_penalty = min(1.0, len(q_plain) / max(len(c_plain), 1) + 0.25)
        plain_score = max(plain_score, contain * len_penalty * 0.92)

    q_skel = _normalize_skeleton(query)
    c_skel = _normalize_skeleton(candidate)
    skel_score = 0.0
    q_core = re.sub(r'<(?:path|code|var)>', '', q_skel)
    c_core = re.sub(r'<(?:path|code|var)>', '', c_skel)
    if len(q_core) >= 4 and len(c_core) >= 4:
        skel_ratio = difflib.SequenceMatcher(None, q_skel, c_skel).ratio()
        skel_bi = _bigram_jaccard(q_skel, c_skel)
        skel_score = (0.55 * skel_ratio + 0.45 * skel_bi) * 0.97

    return round(min(1.0, max(sub_score, plain_score, skel_score)), 3)


def _diff_segments(query, candidate):
    q = (query or '').strip()
    c = (candidate or '').strip()
    sm = difflib.SequenceMatcher(None, q, c)
    segs = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        chunk = c[j1:j2]
        if not chunk:
            continue
        kind = 'same' if tag == 'equal' else 'diff'
        if segs and segs[-1]['type'] == kind:
            segs[-1]['text'] += chunk
        else:
            segs.append({'type': kind, 'text': chunk})
    return segs


def _intraline_segments(old_line, new_line):
    sm = difflib.SequenceMatcher(None, old_line, new_line)
    old_segs, new_segs = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        o_chunk = old_line[i1:i2]
        n_chunk = new_line[j1:j2]
        if tag == 'equal':
            if o_chunk:
                old_segs.append({'type': 'same', 'text': o_chunk})
            if n_chunk:
                new_segs.append({'type': 'same', 'text': n_chunk})
        else:
            if o_chunk:
                old_segs.append({'type': 'emph', 'text': o_chunk})
            if n_chunk:
                new_segs.append({'type': 'emph', 'text': n_chunk})
    return old_segs or [{'type': 'same', 'text': old_line}], new_segs or [{'type': 'same', 'text': new_line}]


def _build_inline_diff(old_text, new_text):
    old_lines = (old_text or '').replace('\r\n', '\n').splitlines()
    new_lines = (new_text or '').replace('\r\n', '\n').splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    rows = []
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                rows.append({
                    'type': 'context',
                    'line_no': j1 + k + 1,
                    'old_no': i1 + k + 1,
                    'segments': [{'type': 'same', 'text': new_lines[j1 + k]}],
                })
        elif tag == 'delete':
            for idx in range(i1, i2):
                removed += 1
                rows.append({
                    'type': 'del',
                    'line_no': idx + 1,
                    'old_no': idx + 1,
                    'segments': [{'type': 'same', 'text': old_lines[idx]}],
                })
        elif tag == 'insert':
            for idx in range(j1, j2):
                added += 1
                rows.append({
                    'type': 'add',
                    'line_no': idx + 1,
                    'old_no': None,
                    'segments': [{'type': 'same', 'text': new_lines[idx]}],
                })
        elif tag == 'replace':
            o_chunk = old_lines[i1:i2]
            n_chunk = new_lines[j1:j2]
            pair_n = min(len(o_chunk), len(n_chunk))
            del_rows = []
            add_rows = []
            for k in range(len(o_chunk)):
                removed += 1
                if k < pair_n:
                    o_segs, _ = _intraline_segments(o_chunk[k], n_chunk[k])
                else:
                    o_segs = [{'type': 'same', 'text': o_chunk[k]}]
                del_rows.append({'type': 'del', 'line_no': i1 + k + 1, 'old_no': i1 + k + 1, 'segments': o_segs})
            for k in range(len(n_chunk)):
                added += 1
                if k < pair_n:
                    _, n_segs = _intraline_segments(o_chunk[k], n_chunk[k])
                else:
                    n_segs = [{'type': 'same', 'text': n_chunk[k]}]
                add_rows.append({'type': 'add', 'line_no': j1 + k + 1, 'old_no': None, 'segments': n_segs})
            rows.extend(del_rows)
            rows.extend(add_rows)
    return {'added': added, 'removed': removed, 'rows': rows}


def _extract_sections(text):
    sections = []
    for line in (text or '').replace('\r\n', '\n').splitlines():
        s = line.strip()
        if s.startswith('## ') and len(s) > 3:
            heading = s[3:].strip()
            if heading and heading not in sections:
                sections.append(heading)
    return sections


def _extract_clause_units(text):
    norm = (text or '').replace('\r\n', '\n')
    lines = norm.split('\n')
    shared = _extract_shared(norm)
    start_line = shared[0] if shared else -1
    end_line = shared[1] if shared else -1

    units = []
    offset = 0
    for idx, raw_line in enumerate(lines):
        line_len = len(raw_line)
        stripped = raw_line.strip()
        if start_line != -1:
            if idx < start_line:
                zone = 'before'
            elif idx <= end_line:
                zone = 'shared'
            else:
                zone = 'after'
        else:
            zone = 'whole'

        skip = (
            not stripped
            or stripped.startswith('#')
            or stripped in (SHARED_START, SHARED_END)
            or re.match(r'^\|[-:\s|]+\|$', stripped)
        )
        if not skip:
            lead_ws = len(raw_line) - len(raw_line.lstrip())
            trail_ws = len(raw_line) - len(raw_line.rstrip())
            line_start = offset + lead_ws
            line_end = offset + line_len - trail_ws
            units.append({
                'line_no': idx + 1,
                'zone': zone,
                'kind': 'line',
                'start_offset': line_start,
                'end_offset': line_end,
                'text': raw_line[lead_ws:line_len - trail_ws],
            })
            # Also extract individual sentences when a line contains multiple sentences.
            body_text = raw_line[lead_ws:line_len - trail_ws]
            sent_matches = [m for m in _SENTENCE_SPLIT_RE.finditer(body_text) if m.group(0).strip()]
            if len(sent_matches) > 1:
                for m in sent_matches:
                    seg = m.group(0)
                    ltrim = len(seg) - len(seg.lstrip())
                    rtrim = len(seg) - len(seg.rstrip())
                    s_clean = seg[ltrim:len(seg) - rtrim]
                    if len(_normalize_plain(s_clean)) >= 4:
                        units.append({
                            'line_no': idx + 1,
                            'zone': zone,
                            'kind': 'sentence',
                            'start_offset': line_start + m.start() + ltrim,
                            'end_offset': line_start + m.end() - rtrim,
                            'text': s_clean,
                        })
        offset += line_len + 1
    return units


def _classify_worktree(wt, text):
    if wt['is_root']:
        return 'root'
    if text and _extract_shared(text):
        return 'shared'
    return 'standalone'


def _insert_text_at_position(existing_text, addition, position='before_shared', section_heading=''):
    norm = (existing_text or '').replace('\r\n', '\n')
    add = (addition or '').replace('\r\n', '\n').strip()
    if not add:
        raise ValueError('要添加的文本不能为空。')
    lines = norm.split('\n')
    if lines and lines[-1] == '':
        lines = lines[:-1]

    resolved_label = '文件末尾'
    insert_at = len(lines)
    need_leading_blank = bool(lines and lines[-1].strip())
    need_trailing_blank = False

    if position == 'before_shared':
        shared = _extract_shared(norm)
        if shared:
            insert_at = shared[0]
            while insert_at > 0 and not lines[insert_at - 1].strip():
                insert_at -= 1
            need_leading_blank = bool(insert_at > 0 and not add.startswith('- '))
            # If previous non-empty line is a bullet and addition is also a bullet, keep tight; otherwise blank line.
            if insert_at > 0 and lines[insert_at - 1].lstrip().startswith('- ') and add.lstrip().startswith('- '):
                need_leading_blank = False
            elif insert_at > 0:
                need_leading_blank = True
            need_trailing_blank = True
            resolved_label = f'第 {insert_at + 1} 行（<!-- SHARED-START --> 之前）'
        else:
            resolved_label = f'第 {len(lines) + 1} 行（无共享标记，落至文件末尾）'
    elif position == 'section' and section_heading:
        target_h = f'## {section_heading.strip()}'
        h_idx = next((i for i, l in enumerate(lines) if l.strip() == target_h), -1)
        if h_idx != -1:
            end_idx = len(lines)
            for j in range(h_idx + 1, len(lines)):
                s = lines[j].strip()
                if s.startswith('## ') or s == SHARED_END:
                    end_idx = j
                    break
            insert_at = end_idx
            while insert_at > h_idx + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            prev_is_bullet = insert_at > h_idx + 1 and lines[insert_at - 1].lstrip().startswith('- ')
            need_leading_blank = not (prev_is_bullet and add.lstrip().startswith('- '))
            need_trailing_blank = insert_at < len(lines) and bool(lines[insert_at].strip())
            resolved_label = f'## {section_heading.strip()} 末尾（第 {insert_at + 1} 行）'
        else:
            resolved_label = f'未找到「## {section_heading.strip()}」，落至文件末尾'
    else:
        prev_is_bullet = bool(lines and lines[-1].lstrip().startswith('- '))
        need_leading_blank = bool(lines) and not (prev_is_bullet and add.lstrip().startswith('- '))
        resolved_label = f'第 {len(lines) + 1} 行（文件末尾）'

    add_lines = add.split('\n')
    chunk = ([] if not need_leading_blank else ['']) + add_lines + ([] if not need_trailing_blank else [''])
    # Avoid duplicating blank lines around insertion point.
    before = lines[:insert_at]
    after = lines[insert_at:]
    if before and not before[-1].strip() and chunk and not chunk[0].strip():
        chunk = chunk[1:]
    if after and not after[0].strip() and chunk and not chunk[-1].strip():
        chunk = chunk[:-1]
    rebuilt = '\n'.join(before + chunk + after).rstrip() + '\n'
    return rebuilt, resolved_label


def sha256_text(text):
    normalized = (text or '').replace('\r\n', '\n')
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _extract_shared(text):
    lines = text.splitlines()
    start = end = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == SHARED_START and start == -1:
            start = i
        elif stripped == SHARED_END and start != -1:
            end = i
            break
    if start == -1 or end == -1:
        return None
    return start, end, '\n'.join(lines[start + 1:end]).strip()


def _replace_shared(target_text, new_shared_body):
    found = _extract_shared(target_text)
    if not found:
        return None
    start, end, _ = found
    lines = target_text.splitlines()
    rebuilt = lines[:start] + [SHARED_START, new_shared_body.strip(), SHARED_END] + lines[end + 1:]
    return '\n'.join(rebuilt).rstrip() + '\n'


def _extract_independent(text):
    found = _extract_shared(text or '')
    if not found:
        return text or ''
    start, end, _ = found
    lines = (text or '').replace('\r\n', '\n').splitlines()
    before = '\n'.join(lines[:start]).strip()
    after = '\n'.join(lines[end + 1:]).strip()
    joined = '\n\n'.join(part for part in (before, after) if part)
    return (joined + '\n') if joined else ''


def _combine_independent_and_shared(independent_text, existing_text):
    found = _extract_shared(existing_text or '')
    ind = (independent_text or '').replace('\r\n', '\n').strip()
    if not found:
        return (ind + '\n') if ind else ''
    shared_body = found[2].strip()
    if ind:
        return f'{ind}\n\n{SHARED_START}\n{shared_body}\n{SHARED_END}\n'
    return f'{SHARED_START}\n{shared_body}\n{SHARED_END}\n'
