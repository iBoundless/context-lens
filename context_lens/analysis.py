"""Non-overlapping payload blocks. Token estimates are deliberately not usage."""

import hashlib
import json
import math

from .skills import segment_skill_text, summarize_skills, valid_name


ANALYSIS_VERSION = 2


def text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def mapping(value):
    return value if isinstance(value, dict) else {}


def multimodal(value, content=False):
    if isinstance(value, list):
        return any(multimodal(v, content) for v in value)
    if isinstance(value, dict):
        kind = value.get('type', '')
        known_text = {'text', 'input_text', 'output_text', 'thinking', 'redacted_thinking',
                      'tool_use', 'tool_result', 'function_call', 'function_call_output'}
        attachment = isinstance(kind, str) and kind in {
            'image', 'image_url', 'input_image', 'input_audio', 'audio', 'document',
            'input_file', 'file', 'video', 'input_video', 'media'}
        unknown_content = content and 'type' in value and (not isinstance(kind, str) or kind not in known_text)
        return attachment or unknown_content or any(multimodal(v, k == 'content') for k, v in value.items())
    return False


def analyze(payload):
    blocks, warnings = [], []

    def add(category, source, value, attribution='protocol', *, scan=False):
        unknown_tokens = multimodal(value)
        if unknown_tokens and 'multimodal-tokens-unknown' not in warnings:
            warnings.append('multimodal-tokens-unknown')
        explicit = source[6:] if category == 'skills' and source.startswith('skill:') else None
        if valid_name(explicit):
            segments = [{'content': text(value), 'skill_name': explicit, 'skill_kind': 'loaded',
                         'attribution': attribution}]
        elif scan:
            segments = text_segments(value)
        else:
            segments = [{'content': text(value)}]
        # Keep the exact serialized envelope in ordinary blocks. Skill spans alone
        # have known text size even when another field contains an attachment.
        pending = ''
        merged = []
        for part in segments:
            if 'skill_name' in part:
                if pending:
                    merged.append({'content': pending})
                    pending = ''
                merged.append(part)
            else:
                pending += part['content']
        if pending or not merged:
            merged.append({'content': pending})
        for i, part in enumerate(merged):
            content = part['content']
            skill = 'skill_name' in part
            block_source = source if len(merged) == 1 else f'{source}/part[{i}]'
            blocks.append({'id': hashlib.sha256((block_source + content).encode()).hexdigest()[:20],
                           'category': 'skills' if skill else category, 'source': block_source,
                           'attribution': part.get('attribution', attribution), **part,
                           'estimated_tokens': None if unknown_tokens and not skill else math.ceil(len(content.encode()) / 4)})

    completeness = 'client-payload'
    if not isinstance(payload, dict):
        add('unknown', 'payload', payload)
        completeness = 'unknown-protocol'
    else:
        if payload.get('previous_response_id'):
            completeness = 'server-state-unknown'
            warnings.append('server-state-unknown')
        for key in ['instructions', 'system']:
            if payload.get(key):
                parts = payload[key]
                if isinstance(parts, list):
                    for i, part in enumerate(parts):
                        sources = mapping(mapping(mapping(part).get('metadata')).get('experimental')).get('instructions', [])
                        valid = (isinstance(sources, list) and sources and all(isinstance(s, dict) and isinstance(s.get('text'), str)
                                                 and isinstance(s.get('key'), str) for s in sources))
                        if valid and '\n'.join(s['text'] for s in sources) == part.get('text'):
                            for source in sources:
                                name = source['key']
                                category = ('memory' if 'AGENTS.md' in name or name.startswith('memory:')
                                            else 'skills' if name.startswith('skill:') else 'harness')
                                add(category, name, source['text'], 'source-metadata', scan=category != 'skills')
                        else:
                            add('harness', f'{key}[{i}]', part, scan=True)
                else:
                    add('harness', key, parts, scan=True)
        tools = payload.get('tools', [])
        if isinstance(tools, dict):
            tools = [{'name': k, **v} if isinstance(v, dict) else {'name': k, 'schema': v}
                     for k, v in tools.items()]
        if isinstance(tools, list):
            for i, tool in enumerate(tools):
                name = mapping(tool).get('name', mapping(mapping(tool).get('function')).get('name', ''))
                if not isinstance(name, str):
                    name = ''
                kind = tool.get('type', 'function') if isinstance(tool, dict) else ''
                category = ('mcp' if name.startswith(('mcp_', 'mcp__')) else 'builtin') if isinstance(kind, str) and kind in {'function', 'custom', ''} else 'unknown'
                add(category, f'tools[{i}]/{name or kind}', tool,
                    'name-heuristic' if category == 'mcp' else 'protocol')
        for key in ['messages', 'input']:
            messages = payload.get(key, [])
            if isinstance(messages, str):
                add('user', key, messages, scan=True)
                continue
            if not isinstance(messages, list):
                if messages:
                    add('unknown', key, messages)
                continue
            for i, message in enumerate(messages):
                if not isinstance(message, dict):
                    add('unknown', f'{key}[{i}]', message)
                    continue
                role, kind = message.get('role'), message.get('type', '')
                role = role if isinstance(role, str) else None
                kind = kind if isinstance(kind, str) else ''
                category = {'system': 'harness', 'developer': 'harness', 'user': 'user',
                            'assistant': 'assistant', 'tool': 'tool'}.get(role, 'unknown')
                if kind in {'function_call_output', 'custom_tool_call_output'}:
                    category = 'tool'
                elif kind in {'function_call', 'custom_tool_call', 'reasoning'}:
                    category = 'assistant'
                add(category, f'{key}[{i}]/{role or kind or "unknown"}', message, scan=True)
        if not blocks:
            add('unknown', 'payload', payload)
            completeness = 'unknown-protocol'
    if completeness == 'unknown-protocol':
        warnings.append('unknown-protocol')
    counts = [b['estimated_tokens'] for b in blocks]
    return {'blocks': blocks, 'skills': summarize_skills(blocks), 'analysis_version': ANALYSIS_VERSION,
            'estimated_tokens': None if None in counts else sum(counts),
            'estimate_method': 'utf8-bytes/4', 'context_completeness': completeness,
            'warnings': warnings}


def text_segments(value):
    """Scan only protocol text fields; JSON fragments reassemble the original value."""
    def walk(node, allowed=True):
        if not allowed:
            return [{'content': json.dumps(node, ensure_ascii=False, separators=(',', ':'))}]
        if isinstance(node, str):
            parts = segment_skill_text(node)
            return [{'content': '"'},
                    *[part | {'content': json.dumps(part['content'], ensure_ascii=False)[1:-1]} for part in parts],
                    {'content': '"'}]
        if isinstance(node, list):
            output = [{'content': '['}]
            for i, child in enumerate(node):
                if i:
                    output.append({'content': ','})
                output.extend(walk(child, allowed))
            return output + [{'content': ']'}]
        if isinstance(node, dict):
            output = [{'content': '{'}]
            kind = node.get('type')
            text_part = isinstance(kind, str) and kind in {'text', 'input_text', 'output_text'}
            for i, (key, child) in enumerate(node.items()):
                if i:
                    output.append({'content': ','})
                output.append({'content': json.dumps(key, ensure_ascii=False) + ':'})
                is_text = key in {'content', 'output'} or (key == 'text' and text_part)
                output.extend(walk(child, is_text))
            return output + [{'content': '}'}]
        return [{'content': text(node)}]

    if isinstance(value, str):
        return segment_skill_text(value)
    # Root objects can be text content parts or messages. Metadata is never scanned.
    return walk(value)


def response_objects(value):
    """Parse JSON or SSE without treating partial records as complete responses."""
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, str):
        return []
    try:
        obj = json.loads(value)
        return [obj] if isinstance(obj, dict) else []
    except (ValueError, TypeError):
        objects = []
        for record in value.replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip() for line in record.splitlines() if line.startswith('data:'))
            try:
                obj = json.loads(data)
                if isinstance(obj, dict):
                    objects.append(obj)
            except ValueError:
                pass
        return objects


def usage_from(payload):
    usage = None
    for obj in response_objects(payload):
        candidate = obj.get('usage') or mapping(obj.get('response')).get('usage')
        if isinstance(candidate, dict):
            usage = (usage or {}) | clean_usage(candidate)
        message = obj.get('message', {})
        if isinstance(message, dict) and isinstance(message.get('usage'), dict):
            usage = (usage or {}) | clean_usage(message['usage'])
    return usage


def clean_usage(usage):
    result = {}
    for key, value in usage.items():
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
        elif isinstance(value, dict):
            result[key] = clean_usage(value)
    return result
