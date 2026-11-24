"""Efficiency regressions: real subprocess fixtures, no provider credentials or calls."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dual_ai as d
from test_dual_protocol import Fixture, result, FAKE_CLI


class Usage(unittest.TestCase):
    def test_provider_input_semantics_and_unknowns(self):
        claude = d.usage_metrics('claude', dict(input_tokens=20, cache_read_input_tokens=70,
                                              cache_creation_input_tokens=10, output_tokens=5))
        codex = d.usage_metrics('codex', dict(input_tokens=100, cached_input_tokens=70,
                                            cache_write_input_tokens=10, output_tokens=5))
        self.assertEqual(claude, codex)
        self.assertEqual(claude['input_tokens'], 100)
        self.assertEqual(claude['uncached_input_tokens'], 20)
        self.assertEqual(claude['cache_hit_rate'], .7)
        self.assertIsNone(d.usage_metrics('codex', {})['cache_hit_rate'])
        self.assertIsNone(d.usage_metrics('claude', {'input_tokens': 0})['input_tokens'])
        for value in (-1, '20', float('nan'), float('inf'), True):
            self.assertIsNone(d.usage_metrics('codex', {'input_tokens': value})['input_tokens'])
        self.assertIsNone(d.usage_metrics('codex', dict(input_tokens=5, cached_input_tokens=10))['cache_hit_rate'])

    def test_aggregation_does_not_double_count_and_tracks_partial_coverage(self):
        run = d.DualRunThread(master=d.RoleSpec('codex', 'm'))
        run._account(d.MASTER, d.AgentResult(usage=dict(input_tokens=100, cached_input_tokens=80, output_tokens=5)))
        run._account(d.MASTER, d.AgentResult(error='timeout'))
        stats = run._stats[d.MASTER]
        self.assertEqual(stats['tokens'], 105)
        self.assertEqual(stats['cache_hit_rate'], .8)
        self.assertEqual(stats['cache_measured_calls'], 1)
        self.assertEqual(stats['calls'], 2)
        self.assertEqual(stats['cost_known_calls'], 0)
        run._account(d.MASTER, d.AgentResult(cost_usd=0))
        self.assertEqual(stats['cost_known_calls'], 1)

    def test_partial_claude_usage_survives_timeout_without_duplicates(self):
        agent = d.CliAgent('/tmp', claude_path='unused')
        def stream(proc, handle):
            for output in (2, 7):
                handle(json.dumps({'type': 'assistant', 'message': {'id': 'one', 'content': [],
                    'usage': dict(input_tokens=20, cache_read_input_tokens=70,
                                  cache_creation_input_tokens=10, output_tokens=output)}}))
            return 1, [], True
        with patch.object(agent, '_popen'), patch.object(agent, '_stream', side_effect=stream):
            report = agent.run_claude('x', d.RoleSpec('claude', 'm'))
        self.assertFalse(report.ok)
        self.assertEqual(report.usage['output_tokens'], 7)
        self.assertIsNone(report.cost_usd)
        self.assertEqual(d.usage_metrics('claude', report.usage)['cache_hit_rate'], .7)

    def test_single_metadata_is_private_and_contains_no_prompt(self):
        with tempfile.TemporaryDirectory() as root, patch.object(d, 'RUN_STATE_ROOT', Path(root) / 'runs'):
            info = dict(session_id='s', usage={}, metrics=d.usage_metrics('claude', {}),
                        secret='never log', result='private answer', prompt='private prompt')
            path = Path(d.record_single_usage(info))
            text = path.read_text()
            self.assertNotIn('private', text)
            self.assertNotIn('never log', text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


class Continuity(Fixture):
    def invoke(self, agent, spec, prompt, mode, readonly, *args, session_id=''):
        self.seen.append((prompt, mode, readonly, session_id))
        r = result(); r.session_id = session_id or 'session-' + str(len(self.seen))
        return r

    def first(self, **kwargs):
        self.seen = []
        run = self.runner(claude_env={'ANTHROPIC_AUTH_TOKEN': 'secret-a', 'CODEX_HOME': str(self.root)}, **kwargs)
        for role in (d.MASTER, d.WORKER):
            run._run_agent(role, 'work', '任务')
        return run

    def test_same_conversation_resumes_both_roles_with_current_request_and_delta(self):
        with patch.object(d.CliAgent, 'run', new=lambda *a, **k: self.invoke(*a, **k)):
            first = self.first()
            second = d.DualRunThread(cwd=str(self.root), request='新的用户需求', context='旧约束' * 10000,
                                     context_delta='新增中间约束', session_state=first._export_sessions(),
                                     previous_state=str(first._journal.root), claude_env=first.claude_env)
            for role in (d.MASTER, d.WORKER):
                second._run_agent(role, 'plan', '规划', display=False, read_only=True)
            second._run_agent(d.MASTER, 'edit', '执行')
        self.assertEqual([x[3] for x in self.seen], ['', '', 'session-1', 'session-2', 'session-1'])
        self.assertIn('新的用户需求', self.seen[2][0])
        self.assertIn('新增中间约束', self.seen[2][0])
        self.assertNotIn('旧约束' * 10, self.seen[2][0])
        self.assertEqual([x[2] for x in self.seen[2:]], [True, True, False])
        self.assertNotIn('新的用户需求', self.seen[4][0])

    def test_key_changes_invalidate_and_disk_checkpoint_alone_never_restores(self):
        with patch.object(d.CliAgent, 'run', new=lambda *a, **k: self.invoke(*a, **k)):
            first = self.first(master=d.RoleSpec('claude', 'main', effort='low', efforts=['low', 'high']))
            base = dict(cwd=str(self.root), master=first.master, worker=first.worker,
                        claude_env=first.claude_env, session_state=first._export_sessions())
            for changes in (
                    {'master': d.RoleSpec('claude', 'changed', effort='low')},
                    {'master': d.RoleSpec('claude', 'main', effort='high', efforts=['low', 'high'])},
                    {'master': d.RoleSpec('codex', 'main', effort='low')},
                    {'permission_mode': 'bypassPermissions'},
                    {'claude_path': '/different/cli'},
                    {'cwd': str(self.root.parent)},
                    {'claude_env': dict(first.claude_env, ANTHROPIC_AUTH_TOKEN='secret-b')},
                    {'claude_env': dict(first.claude_env, ANTHROPIC_BASE_URL='https://another.invalid')},
                    {'session_state': []}):
                with self.subTest(changes=list(changes)):
                    second = d.DualRunThread(**dict(base, **changes))
                    second._run_agent(d.MASTER, 'work', '任务')
                    self.assertEqual(self.seen[-1][3], '')
            checkpoint = self.runner(previous_state=str(first._journal.root))
            checkpoint._ensure_journal()
            self.assertEqual(checkpoint._sessions, {})
            self.assertNotIn('secret-a', (first._journal.root / 'state.json').read_text())

    def test_unchanged_long_history_does_not_add_compression_call_next_round(self):
        script = self.root / 'agent'; script.write_text(FAKE_CLI); script.chmod(0o755)
        kwargs = dict(master=d.RoleSpec('claude', 'main'), worker=d.RoleSpec('claude', 'worker'),
                      claude_path=str(script), tier='L2', review_enabled=False)
        first = self.runner(**kwargs)
        done = []; first.completed.connect(done.append); first.run()
        self.assertTrue(done[-1]['ok'])
        second = self.runner(**kwargs, session_state=first._export_sessions(),
                             previous_state=str(first._journal.root), context='约束' * 15000, context_delta='')
        second.completed.connect(done.append); second.run()
        self.assertTrue(done[-1]['ok'])
        calls = [json.loads(p.read_text()) for p in second._journal.root.glob('call-*.json')]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(row['resumed'] for row in calls))
        self.assertFalse(any('压缩' in row['stage'] for row in calls))
        self.assertEqual(json.loads((second._journal.root / 'input.json').read_text())['context'], second.context)


class Reporting(Fixture):
    def test_absent_wrapper_evidence_is_derived_but_invalid_evidence_rejected(self):
        payload = json.loads(result(ids=['T1', 'T2']).text.split(': ', 1)[1])
        payload.pop('verification')
        r = d.AgentResult(ok=True, text='DUAL_RESULT: ' + json.dumps(payload))
        self.assertTrue(d.accepted_result(r))
        self.assertEqual(d.delivery_report(r)['verification'], ['T1: exit 0', 'T2: exit 0'])
        for invalid in (None, [], 'check'):
            payload['verification'] = invalid
            self.assertIsNone(d.delivery_report(d.AgentResult(ok=True, text='DUAL_RESULT: ' + json.dumps(payload))))
        payload.pop('verification')
        payload['tasks'][1]['status'] = 'blocked'
        self.assertFalse(d.accepted_result(d.AgentResult(ok=True, text='DUAL_RESULT: ' + json.dumps(payload))))

    def test_report_repair_is_readonly_bounded_and_never_replays_work(self):
        run = self.runner()
        tasks = [{'id': 'T1'}, {'id': 'T2'}]
        original = result()  # aggregate pass without per-task evidence
        with patch.object(run, '_run_agent', return_value=result(ids=['T1', 'T2'])) as call:
            repaired = run._repair_result(d.WORKER, tasks, original)
        self.assertTrue(all(x['status'] == 'passed' for x in run._task_reports(tasks, repaired)))
        self.assertEqual(call.call_count, 1)
        self.assertTrue(call.call_args.kwargs['read_only'])
        self.assertTrue(call.call_args.kwargs['report_only'])
        self.assertIn('禁止执行工具', call.call_args.args[1])
        with patch.object(run, '_run_agent', return_value=result()) as call:
            failed = run._repair_result(d.WORKER, tasks, original)
        self.assertIs(failed, original)
        self.assertEqual(call.call_count, 1)

    def test_failures_unknown_ids_and_cancel_never_trigger_report_repair(self):
        run = self.runner(); tasks = [{'id': 'T1'}, {'id': 'T2'}]
        for r in (result('blocked'), result('failed'), result(ids=['T9']),
                  result(ids=['T1', 'T1']), result(ids=['T2', 'T1']),
                  d.AgentResult(ok=False, text=result().text, error='timeout'),
                  d.AgentResult(ok=True, text=result().text, cancelled=True)):
            with patch.object(run, '_run_agent') as call:
                self.assertIs(run._repair_result(d.WORKER, tasks, r), r)
                call.assert_not_called()

    def test_ask_prefix_is_preserved_and_only_remaining_task_is_sent(self):
        run = self.runner(tier='L2', review_enabled=False)
        calls = []; done = []; errors = []
        plan = {'tasks': [{'title': 'prepare', 'ops': ['execute']}, {'title': 'validate', 'ops': ['execute']}]}
        prefix = json.loads(result(ids=['T1']).text.split(': ', 1)[1])['tasks']
        def invoke(role, prompt, label, **kwargs):
            calls.append((role, prompt, label))
            if '规划' in label:
                return d.AgentResult(ok=True, text=json.dumps(plan))
            if '指导' in label:
                return d.AgentResult(ok=True, text='DUAL_GUIDE: {"decision":"保留已完成准备，只续做验证"}')
            if '（续）' in label:
                self.assertIn('当前任务包：', prompt)
                remaining = json.loads(prompt.split('当前任务包：\n')[1].split('\n\n')[0])
                self.assertEqual([t['id'] for t in remaining], ['T2'])
                return result(ids=['T2'])
            return d.AgentResult(ok=True, text='DUAL_ASK: ' + json.dumps(dict(task='T2', problem='需要决定', completed=prefix)))
        run._run_agent = invoke
        run.completed.connect(done.append); run.failed.connect(errors.append); run.run()
        self.assertFalse(errors, errors)
        self.assertTrue(done[0]['ok'])
        self.assertEqual([r['id'] for r in done[0]['records']], ['T1', 'T2'])
        self.assertEqual(len(calls), 4)
        self.assertFalse(any('接管' in c[2] for c in calls))

    def test_invalid_ask_prefix_cannot_mark_work_passed(self):
        run = self.runner(); tasks = [{'id': 'T1'}, {'id': 'T2'}]
        valid = json.loads(result(ids=['T1']).text.split(': ', 1)[1])['tasks']
        for completed in ([dict(valid[0], id='T9')], [dict(valid[0], status='blocked')],
                          [dict(valid[0], verification=[])], [dict(valid[0], open_issues=['broken'])],
                          valid + valid, 'bad'):
            self.assertEqual(run._ask_prefix(tasks, dict(task='T2', completed=completed)), [])

    def test_cli_report_only_removes_claude_tools(self):
        agent = d.CliAgent(str(self.root), claude_path='unused'); agent.report_only = True
        commands = []
        with patch.object(agent, '_popen', side_effect=lambda cmd, text: commands.append(cmd)), \
             patch.object(agent, '_stream', return_value=(0, [], False)):
            agent.run_claude('report only', d.RoleSpec('claude', 'm'), read_only=True)
        cmd = commands[0]
        self.assertEqual(cmd[cmd.index('--tools')+1], '')
        self.assertEqual(cmd[cmd.index('--permission-mode')+1], 'plan')

# A reproducible version-preparation scenario. Credentials and network are never used.
PUBLISH_CLI = r'''#!/usr/bin/env python3
import json, pathlib, re, sys
p = sys.stdin.read(); args = sys.argv[1:]
log = pathlib.Path('calls.jsonl')
rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
resume = args[args.index('--resume')+1] if '--resume' in args else args[-2] if 'resume' in args else ''
sid = resume or 'publish-' + str(len(rows)+1)
with log.open('a') as f:
    f.write(json.dumps(dict(prompt=p, args=args, resume=resume)) + '\n')
base = dict(status='passed', summary='prepared; not published', verification=['local candidate verified'],
            pending_user_actions=['user runs authenticated push and release'])
if '先为下一个角色压缩有效上下文' in p:
    text = json.dumps(dict(summary='preserve v1.0; prepare v2.0', constraints=['user publishes'], refs=[], risks=[]))
elif '只输出一个 JSON 对象' in p:
    text = json.dumps(dict(constraints=['user executes token commands'], tasks=[
        dict(title='prepare version files', ops=['execute']), dict(title='verify candidate', ops=['execute'])]))
elif pathlib.Path('external-change').exists():
    text = 'DUAL_RESULT: ' + json.dumps(dict(base, status='blocked', summary='external writer changed candidate', open_issues=['candidate changed']))
else:
    if '当前任务包：' in p:
        tasks = json.loads(p.split('当前任务包：\n')[1].split('\n\n')[0])
        ids = [t['id'] for t in tasks]
    else:
        ids = ['T1']
    if 'T1' in ids:
        with pathlib.Path('actions.jsonl').open('a') as f: f.write('prepare\n')
        pathlib.Path('VERSION').write_text('2.0\n')
        pathlib.Path('RELEASE.md').write_text('v1.0 preserved. v2.0 ready. Not published. User runs authenticated commands.\n')
    if 'T2' in ids:
        assert pathlib.Path('VERSION').read_text() == '2.0\n'
        with pathlib.Path('actions.jsonl').open('a') as f: f.write('verify\n')
    payload = dict(base, tasks=[dict(base, id=i) for i in ids])
    # Exact historical defect: useful per-task evidence, missing redundant wrapper field.
    if len(ids) > 1: payload.pop('verification')
    text = 'DUAL_RESULT: ' + json.dumps(payload)
if 'exec' in args:
    print(json.dumps(dict(type='thread.started', thread_id=sid)))
    print(json.dumps(dict(type='item.completed', item=dict(type='agent_message', text=text))))
    print(json.dumps(dict(type='turn.completed', usage=dict(input_tokens=100, cached_input_tokens=80, output_tokens=5))))
else:
    print(json.dumps(dict(type='system', subtype='init', session_id=sid)))
    print(json.dumps(dict(type='result', result=text, session_id=sid)))
'''


class PublishPreparation(Fixture):
    def test_publication_is_deferred_without_false_takeover_or_repeated_edits(self):
        script = self.root / 'publish-agent'; script.write_text(PUBLISH_CLI); script.chmod(0o755)
        run = self.runner(master=d.RoleSpec('codex', 'main'), worker=d.RoleSpec('claude', 'worker'),
                          codex_path=str(script), claude_path=str(script), review_enabled=False)
        done = []; errors = []; run.completed.connect(done.append); run.failed.connect(errors.append); run.run()
        self.assertFalse(errors, errors)
        self.assertTrue(done[0]['ok'])
        rows = [json.loads(x) for x in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual((self.root / 'actions.jsonl').read_text().splitlines(), ['prepare', 'verify'])
        self.assertFalse(any('接管' in p.read_text() for p in run._journal.root.glob('call-*.json')))
        self.assertTrue(all(r['report']['pending_user_actions'] for r in done[0]['records']))
        self.assertIn('Not published', (self.root / 'RELEASE.md').read_text())

    def test_external_change_is_still_blocking_and_does_not_claim_published(self):
        script = self.root / 'publish-agent'; script.write_text(PUBLISH_CLI); script.chmod(0o755)
        (self.root / 'external-change').write_text('changed by another process')
        run = self.runner(master=d.RoleSpec('codex', 'main'), worker=d.RoleSpec('claude', 'worker'),
                          codex_path=str(script), claude_path=str(script), review_enabled=False)
        done = []; run.completed.connect(done.append); run.run()
        self.assertFalse(done[0]['ok'])
        self.assertEqual(done[0]['report_repairs'], 0)
        self.assertFalse((self.root / 'VERSION').exists())
        self.assertEqual([r['id'] for r in done[0]['records']], ['T1'])


class TaskStateAndHandoff(Fixture):
    """Versioned task state, incremental handoff, budget and stable prefix."""

    def plan(self, count=2, constraints=None):
        return d.normalize_plan({"constraints": constraints or ["保留旧接口"],
                                 "tasks": [{"title": "task " + str(i), "ops": ["execute"],
                                            "points": 3} for i in range(count)]},
                                "原始目标不可丢失", "L2")

    def record(self, run, task, status="passed", evidence=None, files=None, decisions=None):
        report = dict(id=task["id"], status=status, summary=status + " " + task["id"],
                      verification=evidence if evidence is not None else ["check " + task["id"]],
                      changed_files=files or [], decisions=decisions or [],
                      interfaces=[], open_issues=[] if status == "passed" else ["needs work"])
        return run._record(task, report, result())

    def test_versioned_state_is_persisted_with_fact_status_and_monotonic_revision(self):
        run = self.runner()
        plan = self.plan()
        run._plan = plan
        run._ensure_journal()
        start = run._revision
        first = self.record(run, plan["tasks"][0])
        second = self.record(run, plan["tasks"][1], status="blocked")
        self.assertGreater(first["revision"], start)
        self.assertGreater(second["revision"], first["revision"])
        state = json.loads((run._journal.root / "state.json").read_text())
        self.assertEqual(state["protocol_version"], 4)
        self.assertEqual(state["revision"], run._revision)
        for key in ("task_id", "revision", "goal", "constraints", "completed", "decisions",
                    "artifacts", "verification", "pending", "next_action", "resume_point"):
            self.assertIn(key, state["task_state"])
        completed = {x["id"]: x for x in state["task_state"]["completed"]}
        self.assertEqual(completed["T1"]["status"], "verified")
        self.assertEqual(completed["T2"]["status"], "incomplete")
        self.assertTrue(any(x["status"] == "blocked" for x in state["task_state"]["pending"]))
        self.assertIn("T2", state["task_state"]["next_action"])

    def test_legacy_protocol_missing_task_state_stays_unknown(self):
        first = self.runner()
        first._ensure_journal()
        state_path = first._journal.root / "state.json"
        legacy = json.loads(state_path.read_text())
        legacy["protocol_version"] = 3
        legacy.pop("task_state", None)
        state_path.write_text(json.dumps(legacy))
        second = self.runner(previous_state=str(first._journal.root))
        second._ensure_journal()
        snapshot = json.loads(Path(second._journal.previous).read_text())
        self.assertEqual(snapshot["task_state"]["status"], "unknown")
        self.assertEqual(snapshot["protocol_version"], 3)

    def test_atomic_write_failure_keeps_previous_state(self):
        run = self.runner()
        run._ensure_journal()
        path = run._journal.root / "state.json"
        before = path.read_text()
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                run._journal.write("state.json", {"revision": 999})
        self.assertEqual(path.read_text(), before)
        self.assertEqual(list(run._journal.root.glob("*.tmp")), [])

    def test_artifact_version_changes_and_reference_is_not_content(self):
        target = self.root / "artifact.txt"
        target.write_text("v1\n")
        run = self.runner()
        plan = self.plan(1)
        run._plan = plan
        run._ensure_journal()
        self.record(run, plan["tasks"][0], files=["artifact.txt"])
        first = run._task_state()["artifacts"][0]
        target.write_text("v2 changed\n")
        self.record(run, dict(plan["tasks"][0], id="T2"), files=["artifact.txt"])
        second = run._task_state()["artifacts"][0]
        self.assertTrue(first["accessible"])
        self.assertNotEqual(first["version"], second["version"])
        self.assertIn("引用不代表", first["note"])
        missing = run._artifact_info("missing.txt")
        self.assertFalse(missing["accessible"])

    def test_unconfirmed_operation_is_marked_checked_before_replay(self):
        run = self.runner()
        first = result(); first.session_id = "s1"
        failed = d.AgentResult(error="timeout after edit", tools=[{"name": "Edit",
                                                                  "input": {"file_path": "a.py"}}])
        with patch.object(d.CliAgent, "run", side_effect=[first, failed]) as cli:
            run._run_agent(d.MASTER, "plan", "规划", display=False)
            run._run_agent(d.MASTER, "task", "任务")
        self.assertEqual(cli.call_count, 2)  # 未确认操作不自动重放
        self.assertEqual(len(run._unconfirmed), 1)
        state = run._task_state()
        unconfirmed = [x for x in state["pending"] if x["status"] == "unconfirmed"]
        self.assertEqual(len(unconfirmed), 1)
        self.assertTrue(unconfirmed[0]["needs_check"])
        self.assertIn("核对未确认操作", state["next_action"])
        run._last_role = d.MASTER
        switch = run._record_switch(d.WORKER, "恢复")
        self.assertTrue(switch["requires_check"])
        self.assertEqual(switch["cost"], None)
        self.assertEqual(switch["reread_cost"], "unknown")

    def test_current_request_wins_over_previous_checkpoint(self):
        first = d.DualRunThread(cwd=str(self.root), request="旧目标")
        first._ensure_journal()
        second = d.DualRunThread(cwd=str(self.root), request="最新目标",
                                 previous_state=str(first._journal.root))
        second._ensure_journal()
        state = second._task_state()
        self.assertEqual(state["goal"], "最新目标")
        self.assertTrue(state["supersedes_previous"])
        self.assertIn("最新用户请求优先", second._bootstrap())

    def test_handoff_is_full_then_incremental_per_role(self):
        run = self.runner()
        plan = self.plan(2)
        run._plan = plan
        run._ensure_journal()
        first_text, first = run._handoff_card(d.WORKER)
        self.assertEqual(first["mode"], "full")
        self.assertIn("原始目标不可丢失", first_text)
        self.assertLessEqual(first["tokens_estimate"], run.handoff_token_budget)
        self.record(run, plan["tasks"][0])
        # 模拟 worker 已确认到旧 revision，后续只发送增量。
        run._ack[d.WORKER] = first["sent_revision"]
        delta_text, delta = run._handoff_card(d.WORKER)
        self.assertEqual(delta["mode"], "delta")
        self.assertNotIn("原始目标不可丢失", delta_text)
        card = json.loads(delta_text)
        self.assertIn("delta", card)
        self.assertIn("T1", json.dumps(card["delta"], ensure_ascii=False))
        # 另一个角色尚未确认，仍收到完整必要状态。
        _, master = run._handoff_card(d.MASTER)
        self.assertEqual(master["mode"], "full")

    def test_budget_trim_keeps_constraints_and_reports_overflow_with_valid_json(self):
        constraints = ["约束" + str(i) + "x" * 200 for i in range(30)]
        run = self.runner(handoff_token_budget=300)
        plan = self.plan(3, constraints=constraints)
        run._plan = plan
        run._ensure_journal()
        for task in plan["tasks"]:
            self.record(run, task, evidence=["证据" * 2000])
        text, meta = run._handoff_card(d.WORKER)
        card = json.loads(text)  # 不被截断
        self.assertEqual(card["constraints"], constraints)
        self.assertTrue(card["budget_exceeded"])
        self.assertTrue(card["segment_refs"])
        self.assertIn("next_action", card)
        self.assertIn("pending", card)
        self.assertGreaterEqual(meta["tokens_estimate"], 300)

    def test_handoff_generation_makes_no_model_call(self):
        run = self.runner()
        run._plan = self.plan(2)
        run._ensure_journal()
        with patch.object(run, "_run_agent") as model_call:
            run._state_block(d.WORKER)
            run._handoff_card(d.MASTER)
        model_call.assert_not_called()
        self.assertEqual(run._summary_calls, 0)

    def test_stable_prefix_is_byte_identical_and_has_no_dynamic_data(self):
        run = self.runner()
        first = run._stable_prefix(d.WORKER)
        run._plan = self.plan(2)
        run._ensure_journal()
        self.record(run, run._plan["tasks"][0])
        second = run._stable_prefix(d.WORKER)
        self.assertEqual(first, second)
        for dynamic in (str(self.root), "原始目标不可丢失", "T1", "run-"):
            self.assertNotIn(dynamic, first)
        seen = []
        with patch.object(d.CliAgent, "run", new=lambda agent, spec, prompt, *a, **k: seen.append(prompt) or result()):
            run._run_agent(d.WORKER, "dynamic part", "任务")
        self.assertTrue(seen[0].startswith(first))

    def test_long_tool_output_is_filtered_deduped_and_locatable(self):
        lines = ["start"] + ["same line"] * 20 + ["ERROR: disk full"] + ["noise " + str(i) for i in range(200)]
        condensed = d.condense_output("\n".join(lines), max_chars=800, max_lines=40,
                                      source_ref="call-007.json")
        self.assertLess(len(condensed), len("\n".join(lines)))
        self.assertIn("ERROR: disk full", condensed)
        self.assertIn("重复", condensed)
        self.assertIn("call-007.json", condensed)
        self.assertIn("省略", condensed)

    def test_unconfirmed_operation_blocks_top_level_success(self):
        run = self.runner()
        plan = self.plan(1)
        run._plan = plan
        run._ensure_journal()
        self.record(run, plan["tasks"][0])
        run._unconfirmed.append({"role": d.WORKER, "label": "任务", "task_id": "T1",
                                 "call_ref": "call-001.json", "reason": "timeout",
                                 "tool_count": 1, "requires_check": True})
        done = []
        run.completed.connect(done.append)
        run._finish(plan, "", cancelled=False)
        self.assertFalse(done[0]["ok"])
        self.assertEqual(done[0]["unconfirmed_operations"][0]["requires_check"], True)

    def test_handoff_gap_falls_back_to_full_state(self):
        run = self.runner()
        run._plan = self.plan(1)
        run._ensure_journal()
        run._ack[d.WORKER] = 1
        run._history = [{"revision": 5, "kind": "record", "summary": "x", "task_id": "T1"}]
        self.record(run, run._plan["tasks"][0])
        text, meta = run._handoff_card(d.WORKER)
        card = json.loads(text)
        self.assertIn(card.get("handoff_mode"), ("full", "full_gap_fallback"))
        self.assertEqual(meta["mode"], card["handoff_mode"])


class UsageObservation(Fixture):
    def test_reasoning_tokens_and_coverage_are_tracked(self):
        metrics = d.usage_metrics("codex", dict(input_tokens=100, cached_input_tokens=70,
                                               output_tokens=10, reasoning_output_tokens=4))
        self.assertEqual(metrics["reasoning_tokens"], 4)
        run = self.runner()
        run._account(d.MASTER, d.AgentResult(
            usage=dict(input_tokens=100, cache_read_input_tokens=70,
                       cache_creation_input_tokens=0, output_tokens=10),
            cost_usd=0.01))
        run._account(d.MASTER, d.AgentResult(error="timeout"))
        stats = run._stats[d.MASTER]
        self.assertEqual(stats["usage_coverage"], 0.5)
        self.assertEqual(stats["cost_coverage"], 0.5)
        self.assertEqual(stats["cache_coverage"], 0.5)

    def test_duplicate_tools_are_counted_but_side_effects_never_skipped(self):
        tracker = d.DuplicateTracker()
        edit = [{"name": "Edit", "input": {"file_path": "a.py", "old": "x"}}]
        events = tracker.observe(edit, {"a.py": "v1"})
        events = tracker.observe(edit, {"a.py": "v1"})
        self.assertTrue(events[0]["duplicate"])
        self.assertFalse(events[0]["auto_skipped"])
        self.assertEqual(events[0]["kind"], "side_effect")
        read = [{"name": "Read", "input": {"file_path": "a.py"}}]
        tracker.observe(read, {"a.py": "v1"})
        changed = tracker.observe(read, {"a.py": "v2"})
        self.assertTrue(changed[0]["necessary_recheck"])
        self.assertEqual(tracker.duplicates, 2)

    def test_pricing_requires_verified_source_and_version(self):
        metrics = d.usage_metrics("claude", dict(input_tokens=1_000_000, cache_read_input_tokens=0,
                                                cache_creation_input_tokens=0, output_tokens=0))
        self.assertIsNone(d.price_usage(metrics, "m", {"models": {"m": {"input_per_million": 3}}}))
        config = {"source": "test-price-list", "version": "2026-11-24",
                  "models": {"m": {"input_per_million": 3, "cache_read_per_million": 0.3,
                                   "cache_write_per_million": 0, "output_per_million": 15}}}
        self.assertEqual(d.price_usage(metrics, "m", config), 3.0)

    def test_per_call_log_has_model_provider_handoff_and_usage(self):
        run = self.runner(master=d.RoleSpec("claude", "main"),
                          worker=d.RoleSpec("codex", "worker"))
        run._plan = d.normalize_plan({"tasks": [{"title": "task", "ops": ["execute"]}]},
                                     "目标", "L2")
        run._ensure_journal()
        usage = dict(input_tokens=100, cached_input_tokens=70,
                     cache_write_input_tokens=0, output_tokens=5)
        def invoke(agent, spec, prompt, *args, session_id=""):
            r = result(); r.session_id = "s1"; r.usage = usage; r.cost_usd = None
            return r
        with patch.object(d.CliAgent, "run", new=invoke):
            run._run_agent(d.WORKER, "work", "任务")
        call = json.loads(run._last_call_path and Path(run._last_call_path).read_text())
        self.assertEqual(call["provider"], "codex")
        self.assertEqual(call["model"], "worker")
        self.assertEqual(call["kind"], "任务")
        self.assertIsNotNone(call["handoff"])
        self.assertEqual(call["metrics"]["cache_hit_rate"], .7)
        self.assertEqual(call["token_method"], "provider-usage")
        self.assertEqual(call["cost_source"], None)  # 未知费用保持未知
        self.assertIn("tool_events", call)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", json.dumps(call))

    def test_switch_log_records_boundary_reason_and_unknown_costs(self):
        run = self.runner()
        self.assertIsNone(run._record_switch(d.MASTER, "首次分配"))
        entry = run._record_switch(d.WORKER, "规划完成，结果已落盘")
        self.assertEqual((entry["from"], entry["to"]), (d.MASTER, d.WORKER))
        self.assertIn("规划完成", entry["boundary"])
        self.assertIsNone(entry["cost"])
        self.assertEqual(entry["reread_cost"], "unknown")
        self.assertIn("known_cache_state", entry)
        self.assertIsNone(entry["known_cache_state"][d.MASTER]["cache_hit_rate"])
        self.assertFalse(entry["requires_check"])
        run._current = object()
        self.assertFalse(run._can_switch())
        run._current = None


class ComparisonTool(unittest.TestCase):
    def test_transcript_dedup_and_metadata_normalization(self):
        path = Path(__file__).resolve().parents[1] / 'tools/dual_usage_report.py'
        spec = importlib.util.spec_from_file_location('usage_report_test', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as root:
            transcript = Path(root) / 'transcript.jsonl'
            transcript.write_text('\n'.join(json.dumps(dict(type='assistant', message=dict(id='same', model='m',
                usage=dict(input_tokens=10, cache_read_input_tokens=90, cache_creation_input_tokens=0, output_tokens=n))))
                for n in (1, 5)))
            records, info = module.load_records(transcript)
            group = module.summarize(records)[0]
            self.assertEqual(group['observations'], 1)
            self.assertEqual(group['input_tokens'], 100)
            self.assertEqual(group['output_tokens'], 5)
            self.assertEqual(group['cache_hit_rate'], .9)
            self.assertIsNone(group['cost_usd'])


if __name__ == "__main__":
    unittest.main()
