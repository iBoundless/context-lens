"""Conservative format recognition, not evidence of skill activation."""

import html
import re
import xml.etree.ElementTree as ET


TAGS = re.compile(r'</?(skill_content|available_skills)\b[^>]*>')
LOADED = re.compile(r'''<skill_content\s+name\s*=\s*(["'])(.*?)\1\s*>''', re.S)
ENTRY = re.compile(r'<skill>.*?</skill>', re.S)


def valid_name(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256 and not any(ord(c) < 32 for c in value)


def segment_skill_text(value: str) -> list[dict]:
    """Return exact, ordered text spans; ambiguous/nested tags remain ordinary text."""
    spans, stack = [], []
    for tag in TAGS.finditer(value):
        closing = tag.group().startswith('</')
        if not closing:
            if stack:
                stack[0]['nested'] = True
            stack.append({'tag': tag, 'nested': False})
            continue
        if not stack:
            continue
        if tag.group(1) != stack[-1]['tag'].group(1):
            stack[0]['nested'] = True
            continue
        opened = stack.pop()
        if stack or opened['nested']:
            continue
        start = opened['tag'].start()
        if tag.group(1) == 'skill_content':
            match = LOADED.fullmatch(opened['tag'].group())
            name = html.unescape(match.group(2)) if match else ''
            if valid_name(name):
                spans.append((start, tag.end(), name, 'loaded'))
        else:
            body_start = opened['tag'].end()
            body = value[body_start:tag.start()]
            if '<!' in body or opened['tag'].group() != '<available_skills>':
                continue
            for entry in ENTRY.finditer(body):
                try:
                    node = ET.fromstring(entry.group())
                except ET.ParseError:
                    continue
                names, descriptions = node.findall('name'), node.findall('description')
                if (len(names) == len(descriptions) == 1 and valid_name(names[0].text)
                        and len(names[0]) == len(descriptions[0]) == 0 and descriptions[0].text
                        and all(child.tag in {'id', 'name', 'description'} for child in node)):
                    spans.append((body_start + entry.start(), body_start + entry.end(), names[0].text, 'description'))
    segments, previous = [], 0
    for start, end, name, kind in spans:
        if start > previous:
            segments.append({'content': value[previous:start]})
        segments.append({'content': value[start:end], 'skill_name': name, 'skill_kind': kind,
                         'attribution': 'format-heuristic'})
        previous = end
    if previous < len(value) or not segments:
        segments.append({'content': value[previous:]})
    return segments


def summarize_skills(blocks: list[dict]) -> list[dict]:
    groups = {}
    for block in blocks:
        if not block.get('skill_name'):
            continue
        key = (block['skill_name'], block['skill_kind'])
        entry = groups.setdefault(key, {'name': key[0], 'kind': key[1], 'estimated_tokens': 0,
                                        'occurrences': 0, 'attributions': set()})
        count = block['estimated_tokens']
        entry['estimated_tokens'] = (None if count is None or entry['estimated_tokens'] is None
                                     else entry['estimated_tokens'] + count)
        entry['occurrences'] += 1
        entry['attributions'].add(block['attribution'])
    return [entry | {'attributions': sorted(entry['attributions'])} for _, entry in sorted(groups.items())]
