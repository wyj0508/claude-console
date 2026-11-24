#!/usr/bin/env python3
"""Read-only usage comparison. No provider calls, prompt text or credentials in output."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dual_ai import usage_metrics


def load_records(path):
    path = Path(path)
    if path.is_dir():
        try:
            result = json.loads((path / 'result.json').read_text())
        except (OSError, ValueError):
            result = {}
        records = []
        for file in sorted(path.glob('call-*.json')):
            value = json.loads(file.read_text())
            spec = result.get('stats', {}).get(value.get('role'), {})
            source = value.get('source') or spec.get('source') or (
                'codex' if 'cached_input_tokens' in value.get('usage', {}) else 'claude')
            records.append(dict(value, source=source, model=spec.get('model', 'unknown')))
        return records, {'kind': 'dual-cli', 'legacy_totals': result.get('stats', {})}
    if path.suffix == '.jsonl':
        # Streaming transcripts may repeat a message id; the last usage is authoritative.
        messages = {}
        dates = []
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if value.get('timestamp'):
                    dates.append(value['timestamp'])
                message = value.get('message', {})
                if value.get('type') == 'assistant' and isinstance(message, dict) and message.get('id'):
                    messages[message['id']] = {'source': 'claude', 'model': message.get('model', 'unknown'),
                                               'usage': message.get('usage', {})}
        span = None
        if dates:
            try:
                span = (datetime.fromisoformat(dates[-1].replace('Z', '+00:00')) -
                        datetime.fromisoformat(dates[0].replace('Z', '+00:00'))).total_seconds()
            except ValueError:
                pass
        return list(messages.values()), {'kind': 'transcript', 'session_span_seconds': span}
    value = json.loads(path.read_text())
    return [dict(value, cost_usd=value.get('cost'))], {'kind': 'single-sdk'}


def summarize(records):
    groups = {}
    fields = ('input_tokens', 'uncached_input_tokens', 'cache_read_input_tokens',
              'cache_write_input_tokens', 'output_tokens', 'reasoning_tokens')
    for record in records:
        key = (record.get('role', 'single'), record.get('source', 'claude'), record.get('model', 'unknown'))
        group = groups.setdefault(key, dict(role=key[0], source=key[1], model=key[2],
                                           observations=0, resumed=0, cost_usd=None, elapsed=None,
                                           known={}, measured_input=0, measured_read=0, cache_measured=0))
        group['observations'] += 1
        group['resumed'] += bool(record.get('resumed'))
        metrics = usage_metrics(key[1], record.get('usage'))
        for field in fields:
            if metrics[field] is not None:
                group[field] = group.get(field, 0) + metrics[field]
                group['known'][field] = group['known'].get(field, 0) + 1
        if metrics['cache_hit_rate'] is not None:
            group['measured_input'] += metrics['input_tokens']
            group['measured_read'] += metrics['cache_read_input_tokens']
            group['cache_measured'] += 1
        for field in ('cost_usd', 'elapsed'):
            value = record.get(field)
            if isinstance(value, (int, float)):
                group[field] = (group[field] or 0) + value
                group['known'][field] = group['known'].get(field, 0) + 1
    for group in groups.values():
        group['cache_hit_rate'] = (group['measured_read'] / group['measured_input']
                                   if group['measured_input'] else None)
        group['cache_coverage'] = round(group['cache_measured'] / max(1, group['observations']), 4)
        group['usage_coverage'] = round(
            group['known'].get('input_tokens', 0) / max(1, group['observations']), 4)
        group['cost_coverage'] = round(
            group['known'].get('cost_usd', 0) / max(1, group['observations']), 4)
        for field in fields:
            group.setdefault(field, None)
    return list(groups.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='+', type=Path, help='run directory, SDK metadata JSON or transcript JSONL')
    args = parser.parse_args()
    output = []
    for path in args.paths:
        records, metadata = load_records(path)
        output.append(dict(path=str(path), **metadata, groups=summarize(records)))
    print(json.dumps({
        'measurement': 'historical observations; not a controlled A/B',
        'note': ('transcript observations are model messages, not CLI calls; '
                 'unknown remains null; cache hit = cache_read/total input per '
                 'role/model/provider; cost coverage shows how many calls returned cost'),
        'token_method': 'provider-returned usage fields; no character-count estimates',
        'inputs': output}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
