#!/usr/bin/env python3
"""Deterministic subprocess benchmark. Measures scheduling, handoff size and
replay prevention - NOT provider speed, cache or billing.

Compare:
  --baseline PATH  pre-change dual_ai.py snapshot (dual before)
  current module   improved dual (after)
  single_ai       same CLI driven by one model only (L4; publish/long-history)

Only simulated CLI subprocesses and temporary repositories are used. No
network, provider credentials or GitHub token operations are performed.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import dual_ai

SCENARIOS = ('publish_prep', 'long_history', 'multi_stage', 'failure_recovery')


def load_baseline(path):
    spec = importlib.util.spec_from_file_location('dual_ai_baseline', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BENCH_CLI = r'''#!/usr/bin/env python3
import json, pathlib, sys
root = pathlib.Path.cwd()
p = sys.stdin.read()
args = sys.argv[1:]
config = json.loads(pathlib.Path('scenario.json').read_text()) if pathlib.Path('scenario.json').exists() else {}
rows = [json.loads(x) for x in pathlib.Path('calls.jsonl').read_text().splitlines()] if pathlib.Path('calls.jsonl').exists() else []
resume = args[args.index('--resume') + 1] if '--resume' in args else args[-2] if 'resume' in args else ''
sid = resume or 'bench-' + str(len(rows) + 1)
with pathlib.Path('calls.jsonl').open('a') as f:
    f.write(json.dumps(dict(prompt_len=len(p), args=args, resume=resume)) + '\n')

def act(kind):
    with pathlib.Path('actions.jsonl').open('a') as f:
        f.write(kind + '\n')

def report(ids, status='passed', summary='done'):
    base = dict(status=status, summary=summary, verification=['local check exit 0'],
                pending_user_actions=['user runs authenticated release'])
    if '当前任务包：' in p:
        payload = dict(base, tasks=[dict(base, id=i, changed_files=['VERSION'],
                                        decisions=['preserve v1'], interfaces=[],
                                        open_issues=[] if status == 'passed' else ['injected failure'])
                                    for i in ids])
    else:
        payload = base
    return 'DUAL_RESULT: ' + json.dumps(payload, ensure_ascii=False)

def tool_event():
    # Deterministic repeated read event; used only for duplicate observation.
    if 'exec' in args:
        print(json.dumps(dict(type='thread.started', thread_id=sid)))
        print(json.dumps(dict(type='item.completed',
            item=dict(type='web_search', id='same-read', query='VERSION'))))
    else:
        print(json.dumps(dict(type='system', subtype='init', session_id=sid)))
        print(json.dumps(dict(type='assistant', message=dict(
            id='same-message', content=[dict(type='tool_use', id='same-read',
                name='Read', input=dict(file_path='VERSION'))]))))

def emit(text):
    tool_event()
    if 'exec' in args:
        print(json.dumps(dict(type='item.completed',
            item=dict(type='agent_message', text=text))))
        print(json.dumps(dict(type='turn.completed',
            usage=dict(input_tokens=100, cached_input_tokens=70, output_tokens=5,
                       reasoning_output_tokens=2))))
    else:
        print(json.dumps(dict(type='result', result=text, session_id=sid,
            usage=dict(input_tokens=30, cache_read_input_tokens=60,
                       cache_creation_input_tokens=10, output_tokens=5))))

if '先为下一个角色压缩有效上下文' in p:
    emit(json.dumps(dict(summary='preserve v1; prepare v2', constraints=['user publishes'],
                         refs=['VERSION:1'], risks=[])))
    sys.exit(0)

if '只输出一个 JSON 对象' in p:
    if config.get('scenario') == 'multi_stage':
        tasks = [dict(title='prepare version', ops=['execute'], points=3),
                 dict(title='review design', ops=['think'], points=3),
                 dict(title='verify integration', ops=['execute', 'monitor'], points=3)]
    else:
        tasks = [dict(title='prepare version files', ops=['execute'], points=3),
                 dict(title='verify candidate', ops=['execute'], points=3)]
    emit(json.dumps(dict(summary='plan', constraints=['user executes token commands'],
                         tasks=tasks), ensure_ascii=False))
    sys.exit(0)

ids = []
titles = []
if '当前任务包：' in p:
    tasks = json.loads(p.split('当前任务包：\n')[1].split('\n\n')[0])
    ids = [t['id'] for t in tasks]
    titles = [t.get('title', '') for t in tasks]
elif '当前任务：' in p:
    try:
        task = json.loads(p.split('当前任务：', 1)[1].split('\n', 1)[0])
        ids = [task.get('id', 'T1')]
        titles = [task.get('title', '')]
    except ValueError:
        ids = ['T1']
else:
    ids = ['T1']

mapping = {'prepare': 'prepare', 'verify': 'verify', 'design': 'design',
           'integration': 'integration'}
kinds = []
for title in titles:
    for key in mapping:
        if key in title.lower():
            kinds.append(mapping[key])
            break
if not kinds:
    kinds = ['prepare', 'verify']  # same two-task scope as the publish fixture

if config.get('fail_once') and not pathlib.Path('failed-once').exists():
    pathlib.Path('failed-once').write_text('first attempt stopped before side effects')
    emit(report(ids, status='blocked', summary='injected failure before side effects'))
    sys.exit(0)

for kind in kinds:
    act(kind)
    if kind == 'prepare':
        pathlib.Path('VERSION').write_text('2.0\n')
    if kind == 'integration':
        assert pathlib.Path('VERSION').read_text() == '2.0\n'
pathlib.Path('RELEASE.md').write_text(
    'v1.0 preserved. v2.0 prepared. Not published. User runs authenticated commands.\n')
if '当前任务包：' in p:
    emit(report(ids))
else:
    emit(report(['T1']))
'''


def _metrics(info):
    stats = info.get('stats') or {}
    input_tokens = read = measured = 0
    cost = known = calls = 0
    for item in stats.values():
        calls += int(item.get('calls') or 0)
        input_tokens += int(item.get('cache_measured_input') or 0)
        read += int(item.get('cache_measured_read') or 0)
        measured += int(item.get('cache_measured_calls') or 0)
        cost += float(item.get('cost') or 0)
        known += int(item.get('cost_known_calls') or 0)
    return {
        'calls': calls,
        'cache_hit_rate': (read / input_tokens) if input_tokens and measured else None,
        'cache_coverage': round(measured / max(1, calls), 4),
        'cost_usd': cost if known == calls and calls else None,
        'known_cost_subtotal_usd': cost if known else None,
        'cost_coverage': round(known / max(1, calls), 4),
        'handoff_tokens_estimate': info.get('handoff_tokens'),
        'handoff_method': info.get('handoff_method'),
        'switch_count': info.get('switch_count'),
        'summary_calls': info.get('summary_calls'),
        'duplicate_tool_calls': info.get('duplicate_tool_calls'),
        'prompt_chars': info.get('prompt_chars'),
        'seconds': round(sum(float(i.get('seconds') or 0) for i in stats.values()), 3),
    }


def check_artifacts(root, scenario, actions):
    """Independent fixture checks; a passed model report alone is insufficient."""
    errors = []
    expected = ['prepare', 'design', 'verify'] if scenario == 'multi_stage' else ['prepare', 'verify']
    if actions != expected:
        errors.append(f'actions: expected {expected!r}, got {actions!r}')
    for name, expected_text in (('VERSION.v1', '1.0\n'), ('VERSION', '2.0\n')):
        path = root / name
        if not path.exists() or path.read_text() != expected_text:
            errors.append(f'{name}: missing or incorrect content')
    release = root / 'RELEASE.md'
    if not release.exists() or release.read_text() != (
            'v1.0 preserved. v2.0 prepared. Not published. User runs authenticated commands.\n'):
        errors.append('RELEASE.md: missing preparation or user release boundary')
    return errors


def run_case(module, label, scenario, mode):
    with tempfile.TemporaryDirectory(prefix='dual-benchmark-') as temp:
        root = Path(temp)
        script = root / 'agent'
        script.write_text(BENCH_CLI)
        script.chmod(0o755)
        (root / 'scenario.json').write_text(json.dumps(dict(scenario=scenario,
                                                            fail_once=scenario == 'failure_recovery')))
        (root / 'VERSION.v1').write_text('1.0\n')
        tier = 'L4' if mode == 'single' else 'L2'
        common = dict(cwd=temp, request='Preserve 1.0, prepare 2.0; user executes authenticated commands',
                      master=module.RoleSpec('codex', 'main'),
                      worker=module.RoleSpec('claude', 'worker'),
                      codex_path=str(script), claude_path=str(script),
                      claude_env={'CODEX_HOME': temp},
                      review_enabled=False, tier=tier)
        rounds = []
        previous = None
        action_cursor = 0
        turn_count = 2 if scenario == 'long_history' else 1
        with patch.object(module, 'RUN_STATE_ROOT', root / 'states'):
            for turn in range(1, turn_count + 1):
                kwargs = dict(common, context='' if turn == 1 else 'previous history ' * 1000)
                if previous is not None:
                    kwargs['previous_state'] = str(previous._journal.root)
                    if hasattr(previous, '_export_sessions'):
                        kwargs.update(session_state=previous._export_sessions(), context_delta='')
                run = module.DualRunThread(**kwargs)
                completed, errors = [], []
                run.completed.connect(completed.append)
                run.failed.connect(errors.append)
                run.run()
                if errors:
                    raise RuntimeError(errors)
                info = completed[0]
                actions = ((root / 'actions.jsonl').read_text().splitlines()
                           if (root / 'actions.jsonl').exists() else [])
                actions = actions[action_cursor:]
                action_cursor += len(actions)
                quality_errors = check_artifacts(root, scenario, actions)
                plan = info.get('tasks') or []
                result = dict(
                    turn=turn, ok=bool(info.get('ok')) and not quality_errors,
                    scheduler_ok=bool(info.get('ok')), quality_errors=quality_errors,
                    actions=actions,
                    repeated_side_effects=max(0, len(actions) - len(set(actions))),
                    omissions=[t.get('id') for t in plan
                               if not any(r.get('id') == t.get('id') and r.get('ok')
                                          for r in info.get('records') or [])],
                    resumed=sum(json.loads(p.read_text()).get('resumed', False)
                                for p in run._journal.root.glob('call-*.json')),
                    plan_tasks=len(plan),
                    **_metrics(info))
                rounds.append(result)
                previous = run
        return dict(version=label, scenario=scenario, mode=mode,
                    tier=tier, rounds=rounds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path,
                        default=Path.home() / '.local/state/claude-console/dual-runs/'
                                             'baseline-pre-task-state/dual_ai.py')
    parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS,
                        default=['publish_prep', 'long_history', 'multi_stage', 'failure_recovery'])
    parser.add_argument('--json-out', type=Path)
    args = parser.parse_args()
    cases = []
    baseline_meta = None
    if args.baseline and args.baseline.is_file():
        baseline_meta = {
            'path': str(args.baseline),
            'sha256': hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        }
        baseline = load_baseline(args.baseline)
    else:
        baseline = None
    for scenario in args.scenarios:
        if baseline is not None:
            cases.append(run_case(baseline, 'dual_before', scenario, 'dual'))
        cases.append(run_case(dual_ai, 'dual_after', scenario, 'dual'))
        if scenario in ('publish_prep', 'long_history'):
            cases.append(run_case(dual_ai, 'single_ai', scenario, 'single'))
    output = {
        'measurement': ('deterministic simulated CLI subprocesses and temporary repositories; '
                        'no provider calls, credentials or GitHub token operations'),
        'limitations': ('does not measure provider latency, prompt-cache behavior, billing or '
                        'task quality beyond deterministic fixture checks; handoff token counts '
                        'are heuristic estimates, not billable tokens'),
        'baseline': baseline_meta,
        'cases': cases,
    }
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.write_text(text, encoding='utf-8')
    print(text)
    if any(not row['ok'] or row['omissions'] or row['repeated_side_effects']
           for case in cases for row in case['rounds']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
