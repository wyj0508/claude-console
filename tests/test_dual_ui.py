"""Headless UI checks. External refreshes and config writes are mocked."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5.QtWidgets import QApplication
import main as m
import dual_ai as d

REFRESH_MODEL_CATALOG = m.MainWindow._refresh_model_catalog

CLAUDE_MODELS = [
    {'id': 'c1', 'name': 'Claude Test', 'efforts': ['low', 'medium', 'high']},
    {'id': 'c2', 'name': 'Claude Default', 'efforts': []},
]
CODEX_MODELS = [
    {'id': 'x1', 'name': 'Codex Test', 'efforts': ['minimal', 'high', 'ultra']},
    {'id': 'x2', 'name': 'Codex Default', 'efforts': []},
]


class DualUIFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(m.QSS)

    def setUp(self):
        self.patches = [
            patch.object(m, 'load_config', return_value=copy.deepcopy(m.DEFAULT_CONFIG)),
            patch.object(m, 'save_config'),
            patch.object(m.QMessageBox, 'question', return_value=m.QMessageBox.Yes),
            patch.object(m.shared_agents, 'ensure_shared_layout'),
            patch.object(m.QTimer, 'singleShot'),
            patch.object(m, 'provider_env', return_value={}),
            patch.object(d, 'load_codex_models', return_value=copy.deepcopy(CODEX_MODELS)),
            patch.object(d.BalanceWorker, 'start'),
        ]
        for name in ('_probe_claude', '_refresh_skills', '_populate_projects',
                     '_refresh_favorites', '_refresh_model_catalog'):
            self.patches.append(patch.object(m.MainWindow, name))
        for p in self.patches:
            p.start()
        self.window = m.MainWindow()
        self.window.current_project = tempfile.gettempdir()

    def load_test_models(self):
        w = self.window
        w._on_catalog_loaded(copy.deepcopy(CLAUDE_MODELS), '')
        w._dual_master = d.RoleSpec('claude', 'c1', effort='low')
        w._dual_worker = d.RoleSpec('codex', 'x1', effort='ultra')
        w._populate_dual_combos()

    def restart(self, config):
        self.window.close()
        m.load_config.return_value = copy.deepcopy(config)
        self.window = m.MainWindow()
        self.window.current_project = tempfile.gettempdir()
        return self.window

    def tearDown(self):
        self.window.close()
        for p in reversed(self.patches):
            p.stop()


class DualUI(DualUIFixture):
    def test_real_thread_subprocess_and_ui_completion(self):
        from test_dual_protocol import FAKE_CLI
        self.load_test_models()
        w = self.window
        with tempfile.TemporaryDirectory() as root:
            cli = Path(root) / 'fake-agent'
            cli.write_text(FAKE_CLI)
            cli.chmod(0o755)
            w.current_project = root
            w.dual_check.setChecked(True)
            w.dual_tier_combo.setCurrentIndex(w.dual_tier_combo.findData('L2'))
            w.chat_input.setPlainText('实现并验证')
            with patch.object(d, 'RUN_STATE_ROOT', Path(root) / 'states'), \
                 patch.object(d, 'find_codex_binary', return_value=str(cli)), \
                 patch.object(m, 'find_claude', return_value=str(cli)):
                w.btn_send.click()
                state = w._chat_state()
                deadline = time.monotonic() + 10
                saw_running_progress = False
                while state.get('dual_thread') is not None and time.monotonic() < deadline:
                    thread = state.get('dual_thread')
                    if thread is not None and thread.isRunning():
                        snapshot, channel = w._progress_worker_snapshot()
                        if snapshot is not None and snapshot.get('busy'):
                            saw_running_progress = True
                            self.assertIn('协作', channel)
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertTrue(saw_running_progress)
                self.assertIsNone(state.get('dual_thread'))
                self.assertEqual(state['status'], '协作完成')
                self.assertTrue(w.btn_send.isEnabled())
                saved = Path(state['dual_state_dir'])
                self.assertTrue((saved / 'result.json').is_file())
                self.assertIn(str(saved), state['blocks'][-1]['text'])
                import json
                calls = [json.loads(x) for x in (Path(root) / 'calls.jsonl').read_text().splitlines()]
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[-1]['resume'], 'session-1')

    def test_checkpoint_carried_to_next_turn_and_cleared_for_new_chat(self):
        w = self.window
        state = w._chat_state()
        state['blocks'] = [{'kind': 'user', 'text': '早期约束' + 'x' * 8000}]
        w._on_dual_completed(state, {'state_dir': '/saved/run', 'protocol_version': 2, 'ok': True})
        with patch.object(d, 'find_codex_binary', return_value='/fake/codex'), \
             patch.object(m, 'find_claude', return_value='/fake/claude'), \
             patch.object(d, 'DualRunThread') as thread:
            w._send_dual_message('本轮请求', '本轮请求')
            kwargs = thread.call_args.kwargs
            self.assertEqual(kwargs['previous_state'], '/saved/run')
            self.assertIn('x' * 8000, kwargs['context'])
            self.assertNotIn('本轮请求', kwargs['context'])
            state['dual_thread'] = None
            state['busy'] = False
            w._chat_busy = False
            w._new_chat()
            self.assertNotIn('dual_state_dir', state)

    def test_endpoint_preflight_ignores_unused_backend(self):
        w = self.window
        for tier, codex_bin, claude_bin in (('L4', '/fake/codex', ''),
                                            ('L0', '', '/fake/claude')):
            with self.subTest(tier=tier):
                w.dual_tier_combo.setCurrentIndex(w.dual_tier_combo.findData(tier))
                master = d.RoleSpec('claude', 'master')
                worker = d.RoleSpec('codex', 'worker')
                with patch.object(w, '_dual_spec_from_combo', side_effect=lambda role: master if role == 'master' else worker), \
                     patch.object(d, 'find_codex_binary', return_value=codex_bin), \
                     patch.object(m, 'find_claude', return_value=claude_bin), \
                     patch.object(d, 'DualRunThread') as thread, \
                     patch.object(m.QMessageBox, 'warning') as warning:
                    w._chat_state()['dual_thread'] = None
                    w._send_dual_message('test', 'test')
                    warning.assert_not_called()
                    thread.return_value.start.assert_called_once()
                    self.assertEqual(thread.call_args.kwargs['tier'], tier)
                    w._chat_state()['dual_thread'] = None
                    w._chat_busy = False

    def test_preflight_failure_preserves_input(self):
        w = self.window
        w.chat_input.setPlainText('不要丢失输入')
        w.dual_tier_combo.setCurrentIndex(w.dual_tier_combo.findData('L2'))
        with patch.object(d, 'find_codex_binary', return_value=''), \
             patch.object(m, 'find_claude', return_value=''), \
             patch.object(m.QMessageBox, 'warning'):
            w._send_dual_message('不要丢失输入', '不要丢失输入')
        self.assertEqual(w.chat_input.toPlainText(), '不要丢失输入')
        self.assertEqual(w._chat_state()['blocks'], [])

    def test_old_balance_result_does_not_replace_current_model(self):
        w = self.window
        with patch.object(w, '_dual_spec_from_combo', return_value=d.RoleSpec('codex', 'new')):
            w.dual_master_balance.setText('current')
            w._on_balance_ready('master', {'text': 'old', 'ok': True}, d.RoleSpec('claude', 'old'))
            self.assertEqual(w.dual_master_balance.text(), 'current')
            self.assertIn(('claude', 'old'), w._balance_cache)

    def test_new_chat_waits_for_cancelled_thread(self):
        w = self.window
        thread = MagicMock()
        thread.isRunning.return_value = True
        state = w._chat_state()
        state['blocks'] = [{'kind': 'user', 'text': '保留直到停止'}]
        state['dual_thread'] = thread
        w._new_chat()
        thread.cancel.assert_called_once()
        self.assertTrue(state['blocks'])
        state['dual_thread'] = None

    def test_progress_reports_running_dual_thread(self):
        w = self.window
        thread = MagicMock()
        thread.isRunning.return_value = True
        thread.status_snapshot.return_value = {
            'busy': False,  # 运行中的线程必须以 isRunning 为准
            'dual': True, 'permission_mode': 'acceptEdits',
            'elapsed': 42.0, 'since_activity': 3.0,
            'activity_label': '执行工具 Bash', 'tool_count': 2,
            'last_tool': 'Bash', 'last_error': '',
            'tier': 'L2', 'role_label': '执行 AI', 'task_label': 'T1',
            'review_enabled': False,
        }
        state = w._chat_state()
        state['dual_thread'] = thread
        snapshot, channel = w._progress_worker_snapshot()
        self.assertTrue(snapshot['busy'])
        self.assertIn('协作', channel)
        markup = w._progress_markup(snapshot, channel)
        self.assertIn('进度查询', markup)
        self.assertIn('档次 L2', markup)
        self.assertIn('复查 关', markup)
        self.assertIn('执行 AI', markup)
        self.assertIn('执行工具 Bash', markup)
        self.assertNotIn('当前没有运行中的任务', markup)

        # 当前项目空闲时，后台项目的协作线程也要能报告。
        thread2 = MagicMock()
        thread2.isRunning.return_value = True
        thread2.status_snapshot.return_value = {
            'busy': True, 'dual': True, 'permission_mode': 'default',
            'elapsed': 5.0, 'since_activity': 1.0,
            'activity_label': '主 AI 正在规划…', 'tool_count': 0,
            'last_tool': '', 'last_error': '', 'tier': 'L2',
            'role_label': '主 AI', 'task_label': '',
        }
        state['dual_thread'] = None
        other = w._chat_state('/tmp/another-project')
        other['dual_thread'] = thread2
        snapshot, channel = w._progress_worker_snapshot()
        self.assertTrue(snapshot['busy'])
        self.assertIn('another-project', channel)
        other['dual_thread'] = None

    def test_progress_shows_idle_after_finished_dual_thread(self):
        w = self.window
        thread = MagicMock()
        thread.isRunning.return_value = False
        w._chat_state()['dual_thread'] = thread
        snapshot, _channel = w._progress_worker_snapshot()
        if snapshot is None:
            return
        self.assertFalse(snapshot.get('busy'))

    def test_balance_refresh_allowed_while_dual_busy_and_deduped(self):
        w = self.window
        w._set_dual_busy(True)
        self.assertTrue(w.dual_balance_btn.isEnabled())
        start = len(w._balance_workers)
        w._refresh_balance('single', force=False)
        self.assertEqual(len(w._balance_workers), start + 1)
        # 自动刷新不重复堆积同一模型的查询。
        w._refresh_balance('single', force=False)
        self.assertEqual(len(w._balance_workers), start + 1)
        key = (w._balance_spec('single').source, w._balance_spec('single').model)
        # 查询超时后标签要恢复可读，而不是永远停在“查询中…”。
        w._balance_pending[key] = time.monotonic() - w.BALANCE_TIMEOUT - 1
        w._check_balance_timeouts()
        self.assertNotIn('查询中', w.single_balance.text())
        self.assertIn('超时', w.single_balance.text())
        # 结果到达后仍能正常显示并清除等待标记。
        w._on_balance_ready('single', {'ok': True, 'text': '余额 ¥1.00'},
                            w._balance_spec('single'))
        self.assertIn('¥1.00', w.single_balance.text())
        self.assertNotIn(key, w._balance_pending)

    def test_dual_stream_delta_is_batched_before_render(self):
        w = self.window
        state = w._chat_state()
        wrapper, bubble = w._add_bubble('worker', '', plain=True)
        state['dual_live_wrapper'] = wrapper
        state['dual_live_bubble'] = bubble
        state['dual_last_role'] = 'worker'
        w._on_dual_delta(state, 'worker', '第一段')
        w._on_dual_delta(state, 'worker', '第二段')
        # 高频 delta 先累积，不逐条重排气泡。
        self.assertEqual(bubble.raw_text, '')
        w._flush_dual_stream()
        self.assertEqual(bubble.raw_text, '第一段第二段')
        state['dual_live'] = ''
        w._discard_dual_live(state)


class DualUIAcceptance(DualUIFixture):
    def test_old_config_migrates_roles_and_preserves_single_effort(self):
        config = copy.deepcopy(m.DEFAULT_CONFIG)
        for key in list(config):
            if key.startswith('dual_'):
                del config[key]
        config.update(model='c1', effort='medium')
        w = self.restart(config)
        self.assertEqual(w.config['effort'], 'medium')
        w._on_catalog_loaded(copy.deepcopy(CLAUDE_MODELS), '')
        self.assertEqual(w._current_effort(), 'medium')
        self.assertEqual(w._dual_master.effort, 'medium')
        single = (w._model, w._effort, w.config['model'], w.config['effort'])
        w.dual_check.setChecked(True)
        w.dual_master_effort_combo.setCurrentIndex(0)
        w.dual_worker_effort_combo.setCurrentIndex(0)
        w._swap_dual_roles()
        w.dual_check.setChecked(False)
        self.assertEqual((w._model, w._effort, w.config['model'], w.config['effort']), single)

    def test_legacy_share_config_migrates_to_tier(self):
        config = copy.deepcopy(m.DEFAULT_CONFIG)
        config.pop('dual_tier', None)
        config.update(dual_mode=True, dual_share=31, dual_strategy='ratio')
        w = self.restart(config)
        self.assertEqual(w._dual_tier, 'L2')
        self.assertEqual(w.dual_tier_combo.currentData(), 'L2')
        self.assertTrue(w.single_model_panel.isHidden())

    def test_pending_custom_role_survives_restart_and_catalog(self):
        config = copy.deepcopy(m.DEFAULT_CONFIG)
        config.update(dual_mode=True, dual_tier='L2',
                      dual_master={'source': 'claude', 'model': 'c1', 'effort': 'medium'},
                      dual_worker={'source': 'codex', 'model': 'x1', 'effort': 'ultra'})
        w = self.restart(config)
        self.assertEqual(w.config['dual_master']['effort'], 'medium')
        w._on_catalog_loaded(copy.deepcopy(CLAUDE_MODELS), '')
        self.assertEqual(w._dual_spec_from_combo('master').effort, 'medium')
        self.assertEqual(w._dual_spec_from_combo('worker').effort, 'ultra')
        self.assertEqual(w.dual_tier_combo.currentData(), 'L2')
        self.assertTrue(w.single_model_panel.isHidden())

    def test_repeated_toggle_refresh_swap_efforts_and_tier(self):
        self.load_test_models()
        w = self.window
        single = (w._model, w._effort)
        tiers = ('L0', 'L2', 'L4')
        for index in range(12):
            w.dual_check.setChecked(True)
            for role in ('master', 'worker'):
                combo = getattr(w, 'dual_' + role + '_effort_combo')
                combo.setCurrentIndex(index % combo.count())
            before = [w._dual_spec_from_combo(role) for role in ('master', 'worker')]
            w.dual_swap_btn.click()
            after = [w._dual_spec_from_combo(role) for role in ('master', 'worker')]
            self.assertEqual([(x.source, x.model, x.effort) for x in after],
                             [(x.source, x.model, x.effort) for x in reversed(before)])
            wanted = tiers[index % len(tiers)]
            w.dual_tier_combo.setCurrentIndex(w.dual_tier_combo.findData(wanted))
            self.assertEqual(w.config['dual_tier'], wanted)
            self.assertEqual(w._dual_tier, wanted)
            w._on_catalog_loaded(copy.deepcopy(CLAUDE_MODELS), '')
            self.assertEqual([w._dual_spec_from_combo(role).effort for role in ('master', 'worker')],
                             [x.effort for x in after])
            w.dual_check.setChecked(False)
            self.app.processEvents()
            self.assertEqual((w._model, w._effort), single)
            self.assertFalse(w.single_model_panel.isHidden())
            self.assertTrue(w.dual_panel.isHidden())

    def test_failed_provider_refresh_still_refreshes_codex_models(self):
        self.load_test_models()
        w = self.window
        updated = copy.deepcopy(CODEX_MODELS) + [
            {'id': 'x3', 'name': 'New Codex', 'efforts': ['high']}]
        with patch.object(d, 'load_codex_models', return_value=updated):
            w._on_catalog_loaded([], 'offline')
        models = [w.dual_worker_combo.itemData(i) for i in range(w.dual_worker_combo.count())]
        self.assertTrue(any(isinstance(x, dict) and x.get('model') == 'x3' for x in models))
        self.assertEqual(w._dual_worker.effort, 'ultra')

    def test_refresh_signal_defers_catalog_until_dual_finishes(self):
        self.load_test_models()
        w = self.window
        w.dual_check.setChecked(True)
        # 只替换网络线程；使用真实刷新槽和 loaded/finished 信号路径。
        class Catalog(m.QObject):
            loaded = m.pyqtSignal(list, str)
            finished = m.pyqtSignal()

            def start(self):
                pass

        thread = Catalog(w)
        w.btn_refresh_models.clicked.disconnect()
        w.btn_refresh_models.clicked.connect(
            lambda checked: REFRESH_MODEL_CATALOG(w, checked))
        with patch.object(m, 'ModelCatalogWorker', return_value=thread):
            w.btn_refresh_models.click()
        self.assertIs(w._catalog_thread, thread)
        self.assertFalse(w.btn_refresh_models.isEnabled())
        w._set_dual_busy(True)
        updated = copy.deepcopy(CLAUDE_MODELS)
        updated[0]['efforts'] = ['medium']
        thread.loaded.emit(updated, '')
        self.assertIsNotNone(w._pending_catalog)
        self.assertEqual(w._dual_master.effort, 'low')
        thread.finished.emit()
        self.assertFalse(w.btn_refresh_models.isEnabled())
        w._on_dual_thread_finished(w._chat_state())
        self.assertIsNone(w._pending_catalog)
        self.assertEqual(w._dual_master.effort, 'medium')
        self.assertEqual(w._dual_worker.effort, 'ultra')
        self.assertTrue(w.btn_refresh_models.isEnabled())

    def test_unsupported_role_effort_disables_and_recovers(self):
        self.load_test_models()
        w = self.window
        w.dual_check.setChecked(True)
        for role, source, model in (('master', 'claude', 'c2'), ('worker', 'codex', 'x2')):
            models = getattr(w, 'dual_' + role + '_combo')
            effort = getattr(w, 'dual_' + role + '_effort_combo')
            previous = models.currentIndex()
            for i in range(models.count()):
                data = models.itemData(i)
                if isinstance(data, dict) and (data['source'], data['model']) == (source, model):
                    models.setCurrentIndex(i)
                    break
            self.assertEqual(effort.currentData(), '')
            self.assertFalse(effort.isEnabled())
            models.setCurrentIndex(previous)
            self.assertTrue(effort.isEnabled())

    def test_dual_send_without_sdk_and_stop_restore_controls(self):
        self.load_test_models()
        w = self.window
        with patch.object(m, 'SDK_AVAILABLE', False), \
             patch.object(d, 'find_codex_binary', return_value='/mock/codex'), \
             patch.object(m, 'find_claude', return_value='/mock/claude'), \
             patch.object(d, 'DualRunThread') as thread:
            w._update_chat_buttons()
            self.assertFalse(w.btn_send.isEnabled())
            w.dual_check.setChecked(True)
            self.assertTrue(w.btn_send.isEnabled())
            w.chat_input.setPlainText('test')
            w.btn_send.click()
            thread.return_value.start.assert_called_once()
            kwargs = thread.call_args.kwargs
            self.assertEqual((kwargs['master'].effort, kwargs['worker'].effort), ('low', 'ultra'))
            self.assertTrue(kwargs['review_enabled'])
            self.assertNotIn('effort', kwargs)
            self.assertEqual(w.chat_input.toPlainText(), '')
            for control in (w.dual_check, w.perm_combo, w.dual_master_combo,
                            w.dual_worker_effort_combo, w.dual_swap_btn, w.btn_refresh_models):
                self.assertFalse(control.isEnabled())
            self.assertTrue(w.btn_stop.isEnabled())
            w.btn_stop.click()
            thread.return_value.cancel.assert_called_once()
            w._on_dual_completed(w._chat_state(), {'cancelled': True})
            w._on_dual_thread_finished(w._chat_state())
            self.assertFalse(w._chat_busy)
            self.assertFalse(w.btn_stop.isEnabled())
            self.assertTrue(w.btn_send.isEnabled())
            w.dual_check.setChecked(False)
            self.assertFalse(w.btn_send.isEnabled())

    def test_single_send_stop_uses_original_settings(self):
        self.load_test_models()
        w = self.window
        worker = MagicMock()
        w._chat_state()['worker'] = worker
        with patch.object(m, 'SDK_AVAILABLE', True):
            w._update_chat_buttons()
            w.chat_input.setPlainText('single request')
            w.btn_send.click()
            worker.configure.assert_called_once_with(
                w.current_project, w._current_permission_mode(), resume=None,
                model=w._current_model(), effort=w._current_effort())
            worker.send.assert_called_once_with('single request')
            w._on_chat_busy(w._chat_state(), True)
            w.btn_stop.click()
            worker.interrupt.assert_called_once()
            w._on_chat_busy(w._chat_state(), False)
            self.assertTrue(w.btn_send.isEnabled())

    def test_tier_is_saved_immediately(self):
        w = self.window
        index = w.dual_tier_combo.findData('L4')
        w.dual_tier_combo.setCurrentIndex(index)
        self.assertEqual(w.config['dual_tier'], 'L4')
        self.assertEqual(w._dual_tier, 'L4')
        self.assertEqual(m.save_config.call_args.args[0]['dual_tier'], 'L4')

    def test_review_checkbox_defaults_persists_and_keeps_selection(self):
        w = self.window
        w.dual_check.setChecked(True)
        self.assertTrue(w.review_check.isChecked())
        self.assertTrue(w.config['dual_review_enabled'])
        w.btn_model_config.click()
        w.review_check.setChecked(False)
        self.assertFalse(w.config['dual_review_enabled'])
        # 展开 / 收起、切档次、切模式都不重置复查选择。
        w.btn_model_config.click()
        w.btn_model_config.click()
        w.dual_tier_combo.setCurrentIndex(w.dual_tier_combo.findData('L3'))
        w.dual_check.setChecked(False)
        w.dual_check.setChecked(True)
        self.assertFalse(w.review_check.isChecked())
        self.assertFalse(m.save_config.call_args.args[0]['dual_review_enabled'])
        # 忙碌时禁用，但折叠按钮仍可查看配置。
        w._set_dual_busy(True)
        self.assertFalse(w.review_check.isEnabled())
        self.assertTrue(w.btn_model_config.isEnabled())
        w._set_dual_busy(False)
        self.assertTrue(w.review_check.isEnabled())
        # 重启后保留关闭状态。
        config = copy.deepcopy(w.config)
        self.assertFalse(config['dual_review_enabled'])
        w2 = self.restart(config)
        self.assertFalse(w2.review_check.isChecked())
        # 旧配置没有该键时默认开启。
        old = copy.deepcopy(m.DEFAULT_CONFIG)
        old.pop('dual_review_enabled')
        old['dual_mode'] = True
        w3 = self.restart(old)
        self.assertTrue(w3.review_check.isChecked())

    def test_review_snapshot_is_passed_to_dual_run(self):
        self.load_test_models()
        w = self.window
        w.dual_check.setChecked(True)
        w.review_check.setChecked(False)
        with patch.object(d, 'find_codex_binary', return_value='/fake/codex'), \
             patch.object(m, 'find_claude', return_value='/fake/claude'), \
             patch.object(d, 'DualRunThread') as thread:
            w._send_dual_message('简单任务', '简单任务')
            self.assertFalse(thread.call_args.kwargs['review_enabled'])
            # 运行中勾选框被禁用，本轮使用发送时快照。
            self.assertFalse(w.review_check.isEnabled())
        w._chat_state()['dual_thread'] = None
        w._chat_busy = False

    def test_balance_signals_cache_failure_and_stale_result(self):
        w = self.window
        w._refresh_balances()
        worker = w._balance_workers[-1]
        worker.ready.emit('single', {'ok': True, 'text': '余额 ¥8.92'})
        count = len(w._balance_workers)
        w._balance_timer.timeout.emit()
        self.assertEqual(len(w._balance_workers), count)
        w.dual_balance_btn.click()
        self.assertEqual(len(w._balance_workers), count + 1)
        old_spec = w._balance_spec('single')
        w.model_combo.setCurrentIndex((w.model_combo.currentIndex() + 1) % w.model_combo.count())
        w._on_balance_ready('single', {'ok': False, 'text': '额度不可用', 'detail': 'offline'}, w._balance_spec('single'))
        w._on_balance_ready('single', {'ok': True, 'text': '旧余额'}, old_spec)
        self.assertEqual(w.single_balance.text(), '额度不可用')
        self.assertEqual(w.single_balance.toolTip(), 'offline')
        self.assertIn('#b05a5a', w.single_balance.styleSheet())
        w.dual_check.setChecked(True)
        w.dual_balance_btn.click()
        for role in ('master', 'worker'):
            query = [x for x in w._balance_workers if x.role == role][-1]
            query.ready.emit(role, {'ok': True, 'text': 'Plus · 5h 6% · 周 1%'})
        self.assertIn('5h', w.dual_master_balance.text())
        self.assertIn('5h6%', w.dual_worker_balance.text())

    def test_layout_and_horizontal_access_at_three_sizes(self):
        self.load_test_models()
        w = self.window
        w.show()
        # 默认收起：底栏按钮未选中、配置区不可见、顶部工具栏不再放协作开关。
        self.assertFalse(w.model_config_scroll.isVisible())
        self.assertFalse(w.btn_model_config.isChecked())
        self.assertIn('▾', w.btn_model_config.text())
        self.assertEqual(w.chat_toolbar.layout().indexOf(w.dual_check), -1)
        for width, height in ((960, 660), (1180, 800), (1920, 1080)):
            w.resize(width, height)
            for on in (False, True):
                with self.subTest(width=width, dual=on):
                    w.dual_check.setChecked(on)
                    # 切换模式不会自动展开配置。
                    self.assertFalse(w.model_config_scroll.isVisible())
                    w.btn_model_config.click()
                    for _ in range(3):
                        self.app.processEvents()
                    self.assertTrue(w.model_config_scroll.isVisible())
                    self.assertTrue(w.btn_model_config.isChecked())
                    self.assertIn('▴', w.btn_model_config.text())
                    self.assertEqual(w.single_model_panel.isVisible(), not on)
                    self.assertEqual(w.dual_panel.isVisible(), on)
                    self.assertEqual(w.single_balance.isVisible(), not on)
                    self.assertEqual(w.dual_master_balance.isVisible(), on)
                    self.assertEqual(w.dual_worker_balance.isVisible(), on)
                    if on:
                        self.assertLessEqual(abs(w.dual_master_combo.geometry().center().y()
                                                 - w.dual_tier_combo.geometry().center().y()), 1)
                    row = w.model_config_panel.layout()
                    widgets = [row.itemAt(i).widget() for i in range(row.count())
                               if row.itemAt(i).widget() and row.itemAt(i).widget().isVisible()]
                    for a, b in zip(widgets, widgets[1:]):
                        self.assertLess(a.geometry().right(), b.x())
                    targets = [w.model_combo, w.effort_combo, w.btn_refresh_models]
                    if on:
                        targets += [w.dual_master_combo, w.dual_worker_combo,
                                    w.dual_master_effort_combo, w.dual_worker_effort_combo,
                                    w.dual_tier_combo, w.dual_swap_btn,
                                    w.review_check]
                    for target in targets:
                        w.model_config_scroll.ensureWidgetVisible(target, 0, 0)
                        self.app.processEvents()
                        pos = target.mapTo(w.model_config_scroll.viewport(), m.QPoint(0, 0))
                        self.assertGreaterEqual(pos.x(), 0)
                        self.assertLessEqual(pos.x() + target.width(),
                                             w.model_config_scroll.viewport().width())
                        self.assertGreaterEqual(pos.y(), 0)
                        self.assertLessEqual(pos.y() + target.height(),
                                             w.model_config_scroll.viewport().height())
                    # 底栏顺序：协作开关 → 模型配置 → 额度 → 停止 → 发送，互不重叠。
                    bottom = w.chat_tab.layout().itemAt(
                        w.chat_tab.layout().count() - 1).layout()
                    items = [bottom.itemAt(i).widget() for i in range(bottom.count())]
                    order = [w.dual_check, w.btn_model_config, w.dual_balance_panel,
                             w.btn_stop, w.btn_send]
                    indexes = [items.index(x) for x in order]
                    self.assertEqual(indexes, sorted(indexes))
                    for a, b in zip(order, order[1:]):
                        self.assertLessEqual(a.geometry().right(), b.x())
                    self.assertLess(w.dual_balance_panel.geometry().right(), w.btn_stop.x())
                    self.assertLess(w.btn_stop.geometry().right(), w.btn_send.x())
                    self.assertGreater(w.dual_check.y(), w.chat_input.geometry().bottom())
                    # 再点一次收起。
                    w.btn_model_config.click()
                    for _ in range(2):
                        self.app.processEvents()
                    self.assertFalse(w.model_config_scroll.isVisible())
                    self.assertFalse(w.btn_model_config.isChecked())
                    self.assertIn('▾', w.btn_model_config.text())

    def test_model_config_toggle_keeps_selection_and_never_auto_expands(self):
        self.load_test_models()
        w = self.window
        w.show()
        w.dual_check.setChecked(True)
        w.btn_model_config.click()
        self.assertTrue(w.model_config_scroll.isVisible())
        tier_index = w.dual_tier_combo.findData('L1')
        w.dual_tier_combo.setCurrentIndex(tier_index)
        worker_models = w.dual_worker_combo
        for i in range(worker_models.count()):
            data = worker_models.itemData(i)
            if isinstance(data, dict) and data.get('model') == 'x2':
                worker_models.setCurrentIndex(i)
                break
        before = {
            role: (spec.source, spec.model, spec.effort)
            for role, spec in ((r, w._dual_spec_from_combo(r))
                               for r in ('master', 'worker'))}
        before_tier = w.dual_tier_combo.currentData()
        single_model = w.model_combo.currentData()
        # 收起后切换模式、刷新目录都不应自动展开，也不应改变选择。
        w.btn_model_config.click()
        self.assertFalse(w.model_config_scroll.isVisible())
        w.dual_check.setChecked(False)
        w._on_catalog_loaded(copy.deepcopy(CLAUDE_MODELS), '')
        self.assertFalse(w.model_config_scroll.isVisible())
        w.dual_check.setChecked(True)
        self.assertFalse(w.model_config_scroll.isVisible())
        after = {
            role: (spec.source, spec.model, spec.effort)
            for role, spec in ((r, w._dual_spec_from_combo(r))
                               for r in ('master', 'worker'))}
        self.assertEqual(after, before)
        self.assertEqual(w.dual_tier_combo.currentData(), before_tier)
        self.assertEqual(w.model_combo.currentData(), single_model)
        # 再展开仍是同一批控件，选择不变；最后再点一次可隐藏。
        w.btn_model_config.click()
        self.assertTrue(w.model_config_scroll.isVisible())
        self.assertEqual({
            role: (spec.source, spec.model, spec.effort)
            for role, spec in ((r, w._dual_spec_from_combo(r))
                               for r in ('master', 'worker'))}, before)
        w.btn_model_config.click()
        self.assertFalse(w.model_config_scroll.isVisible())

    def test_model_config_busy_rules_and_toggle_remains_usable(self):
        self.load_test_models()
        w = self.window
        w.show()
        w.dual_check.setChecked(True)
        w.btn_model_config.click()
        w._set_dual_busy(True)
        self.assertTrue(w.model_config_scroll.isVisible())
        for control in (w.dual_check, w.dual_master_combo, w.dual_worker_combo,
                        w.dual_master_effort_combo, w.dual_worker_effort_combo,
                        w.dual_swap_btn, w.dual_tier_combo, w.review_check,
                        w.btn_refresh_models):
            self.assertFalse(control.isEnabled())
        # 展开 / 收起只影响显隐，忙碌时仍可查看配置。
        w.btn_model_config.click()
        self.assertFalse(w.model_config_scroll.isVisible())
        w.btn_model_config.click()
        self.assertTrue(w.model_config_scroll.isVisible())
        w._set_dual_busy(False)
        for control in (w.dual_check, w.dual_master_combo, w.dual_worker_combo,
                        w.dual_swap_btn, w.dual_tier_combo, w.review_check,
                        w.btn_refresh_models):
            self.assertTrue(control.isEnabled())


class ChatScrollPositioning(DualUIFixture):
    """切换项目后定位到对话最底部：使用真实 singleShot，不依赖被 mock 的定时器。"""

    LONG = '\n'.join(
        f'第{i}行 内容内容内容内容内容内容内容内容内容内容' for i in range(100))

    def setUp(self):
        super().setUp()
        self._timer_patch = next(
            p for p in self.patches
            if getattr(p, 'attribute', '') == 'singleShot')

    def _settle(self, seconds=0.25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)

    @staticmethod
    def _blocks(rounds=3, latest='最新一轮问题：起始位置'):
        blocks = []
        for index in range(rounds):
            text = latest if index == rounds - 1 else f'第{index + 1}轮问题'
            blocks.append({'kind': 'user', 'text': text})
            blocks.append({'kind': 'assistant', 'text': ChatScrollPositioning.LONG})
        return blocks

    def _target_geometry(self):
        w = self.window
        target = w._chat_last_user_wrapper
        y = target.mapTo(w.chat_body_widget, m.QPoint(0, 0)).y()
        return w.chat_scroll.verticalScrollBar(), target, y

    def test_switch_lands_at_conversation_bottom(self):
        w = self.window
        w.show()
        self._timer_patch.stop()
        try:
            for width, height in ((960, 660), (1180, 800), (1920, 1080)):
                with self.subTest(width=width, height=height):
                    w.resize(width, height)
                    path = tempfile.mkdtemp()
                    w.current_project = path
                    w._chat_state(path)['blocks'] = self._blocks()
                    w._reset_chat_for_project()
                    self._settle()
                    bar, target, _y = self._target_geometry()
                    bubble = target.findChild(m.ChatBubble)
                    self.assertEqual(bubble.role, 'user')
                    self.assertEqual(bubble.raw_text, '最新一轮问题：起始位置')
                    # 切换项目后直接显示对话最底部。
                    self.assertGreater(bar.maximum(), 0)
                    self.assertEqual(bar.value(), bar.maximum())
                    # 正常聊天仍自动滚到底。
                    w._add_bubble('assistant', self.LONG)
                    self._settle()
                    self.assertEqual(bar.value(), bar.maximum())
        finally:
            self._settle()
            self._timer_patch.start()

    def test_empty_and_ai_only_projects_are_safe(self):
        w = self.window
        w.resize(960, 660)
        w.show()
        self._timer_patch.stop()
        try:
            # 只有 AI 消息：同样直接定位到最底部。
            path = tempfile.mkdtemp()
            w.current_project = path
            w._chat_state(path)['blocks'] = [
                {'kind': 'assistant', 'text': self.LONG},
                {'kind': 'note', 'text': '仅 AI 消息'},
                {'kind': 'assistant', 'text': self.LONG},
            ]
            w._reset_chat_for_project()
            self._settle()
            bar = w.chat_scroll.verticalScrollBar()
            self.assertIsNone(w._chat_last_user_wrapper)
            self.assertGreater(bar.maximum(), 0)
            self.assertEqual(bar.value(), bar.maximum())
            # 空项目：显示空态，不滚动、不报错。
            empty = tempfile.mkdtemp()
            w.current_project = empty
            w._reset_chat_for_project()
            self._settle()
            self.assertEqual(bar.value(), 0)
        finally:
            self._settle()
            self._timer_patch.start()

    def test_project_changed_signal_path_positions_latest_round(self):
        w = self.window
        w.show()
        self._timer_patch.stop()
        try:
            path = tempfile.mkdtemp()
            w._chat_state(path)['blocks'] = self._blocks(latest='信号路径最后一问')
            item = m.QListWidgetItem()
            item.setData(m.Qt.UserRole, {'name': 'B', 'path': path})
            with patch.object(w, '_refresh_sessions'), \
                 patch.object(w, '_remember_active_tab'), \
                 patch.object(w, '_capture_study_state'), \
                 patch.object(w, '_restore_active_tab'), \
                 patch.object(w, '_switch_study_page_to'), \
                 patch.object(w, '_switch_dev_page_to'):
                w._on_project_changed(item, None)
            self._settle()
            self.assertEqual(w.current_project, path)
            bar, target, _y = self._target_geometry()
            self.assertEqual(target.findChild(m.ChatBubble).raw_text,
                             '信号路径最后一问')
            self.assertEqual(bar.value(), bar.maximum())
        finally:
            self._settle()
            self._timer_patch.start()

    def test_hidden_chat_tab_applies_anchor_on_return(self):
        w = self.window
        w.show()
        self._timer_patch.stop()
        try:
            if w.tabs.indexOf(w.study_page) < 0:
                self.skipTest('研读页在独立窗口，无法用于隐藏对话页')
            w.tabs.setCurrentWidget(w.study_page)
            self._settle()
            path = tempfile.mkdtemp()
            w.current_project = path
            w._chat_state(path)['blocks'] = self._blocks(latest='隐藏页切换后的提问')
            w._reset_chat_for_project()
            self._settle()
            # 对话页不可见时先暂存“滚到底部”，不乱滚。
            self.assertTrue(w._pending_chat_bottom)
            w.tabs.setCurrentWidget(w.chat_tab)
            self._settle()
            self.assertFalse(w._pending_chat_bottom)
            bar, target, _y = self._target_geometry()
            self.assertEqual(target.findChild(m.ChatBubble).raw_text,
                             '隐藏页切换后的提问')
            self.assertEqual(bar.value(), bar.maximum())
        finally:
            self._settle()
            self._timer_patch.start()

    def test_fast_switch_and_background_output_keep_current_target(self):
        w = self.window
        w.resize(960, 660)
        w.show()
        self._timer_patch.stop()
        try:
            first = tempfile.mkdtemp()
            second = tempfile.mkdtemp()
            w._chat_state(first)['blocks'] = self._blocks(
                rounds=5, latest='项目 A 最后一问')
            w._chat_state(second)['blocks'] = self._blocks(
                rounds=2, latest='项目 B 最后一问')
            # 连续切换，第一次的滚动回调必须失效。
            w.current_project = first
            w._reset_chat_for_project()
            w.current_project = second
            w._reset_chat_for_project()
            self._settle()
            bar, target, _y = self._target_geometry()
            bubble = target.findChild(m.ChatBubble)
            self.assertEqual(bubble.raw_text, '项目 B 最后一问')
            self.assertEqual(w.current_project, second)
            self.assertEqual(bar.value(), bar.maximum())
            # 后台项目输出不改变当前项目的定位：仍停在当前项目底部。
            w._on_dual_note(w._chat_state(first), '项目 A 后台输出')
            self._settle()
            self.assertEqual(bar.value(), bar.maximum())
            self.assertEqual(w.current_project, second)
        finally:
            self._settle()
            self._timer_patch.start()


class ConfigMigration(unittest.TestCase):
    def test_real_config_loader_migrates_legacy_share_and_saves_tier(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            with patch.object(m, 'CONFIG_PATH', path), patch.object(m, 'CONFIG_DIR', Path(directory)):
                for share, tier in ((0, 'L4'), (20, 'L3'), (31, 'L2'), (90, 'L1'), (100, 'L0')):
                    path.write_text(json.dumps({'dual_share': share}))
                    self.assertEqual(m.load_config()['dual_tier'], tier)
                    self.assertEqual(json.loads(path.read_text())['dual_tier'], tier)
                    self.assertEqual(m.load_config()['dual_tier'], tier)
                path.write_text(json.dumps({'dual_tier': 'L1', 'dual_share': 0}))
                self.assertEqual(m.load_config()['dual_tier'], 'L1')
                # 复查开关：旧配置缺省开启，显式关闭则保留。
                path.write_text(json.dumps({}))
                self.assertTrue(m.load_config()['dual_review_enabled'])
                path.write_text(json.dumps({'dual_review_enabled': False}))
                self.assertFalse(m.load_config()['dual_review_enabled'])


if __name__ == '__main__':
    unittest.main()
