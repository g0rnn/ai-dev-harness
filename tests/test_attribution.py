"""Task/PR attribution boundaries, independent of branch/worktree and transcript text."""
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


a = module('task_attribution_test', 'core/metrics/attribution.py')
c = module('task_attribution_collect', 'core/metrics/collect.py')
s = module('task_attribution_summary', 'core/metrics/summary.py')


def ts(minute):
    return f'2026-10-20T10:{minute:02d}:00+00:00'


def connection(task, since=0, until=None, session='s', pr=None):
    return dict(task=task, session=session, since=ts(since), until=ts(until) if until is not None else None, pr=pr)


def event(minute, tokens=None, session='s', parent=None, kind=None):
    row = dict(session=session, ts=ts(minute), branch='wrong-source-branch', tool='codex', event=kind or ('start' if tokens is None else 'stop'))
    if tokens is not None:
        row['usage'] = {'fixture': {k: tokens if k == 'input' else 0 for k in c.USAGE_FIELDS}}
    if parent:
        row['parent'] = parent
        row['event'] = 'usage'
    return row


def amount(summary):
    return sum(x['input'] for x in summary['usage'].values())


class ScopedAttributionTest(unittest.TestCase):
    def test_same_chat_switch_does_not_move_previous_cumulative_tokens_to_next_pr(self):
        rows = [event(0), event(1,100), event(2,160), event(3,200), event(4,260)]
        bindings = [connection('A',until=2,pr='143'),connection('B',since=2,pr='144')]
        first = a.scoped(c,rows,bindings,pr='143')
        second = a.scoped(c,rows,bindings,pr='144')
        self.assertEqual((amount(first),amount(second)),(100,100))
        self.assertEqual((first['turns'],second['turns']),(1,3))
        self.assertIn('s',second['usage_errors'])  # boundary increment 60 remains unassigned
        rendered=s.agent_text(c,second,None,None,c.DEFAULT_PRICING)
        self.assertIn('완전한 귀속 수치 미확정',rendered)
        self.assertNotIn('- 토큰:',rendered)
        self.assertNotIn('- 추정 비용',rendered)
        self.assertLessEqual(amount(first)+amount(second),260)

    def test_task_and_pr_match_and_duplicate_records_do_not_double_count(self):
        rows=[event(0),event(1,50),event(2,80)]
        bindings=[connection('TASK',pr='143')]
        self.assertEqual(amount(a.scoped(c,rows*2,bindings,task='TASK')),80)
        copies=[dict(row,branch='another-worktree-branch') for row in rows]
        self.assertEqual(a.scoped(c,rows+copies,bindings,task='TASK')['turns'],2)
        self.assertEqual(amount(a.scoped(c,rows,bindings,pr='#143')),80)
        rendered=s.agent_text(c,a.scoped(c,rows,bindings,pr='143'),None,None,c.DEFAULT_PRICING)
        self.assertIn('귀속 범위: `pr:143`',rendered)

    def test_unbound_history_is_not_charged_to_first_pr(self):
        rows=[event(0),event(1,900),event(3,950)]
        result=a.scoped(c,rows,[connection('A',since=2)],task='A')
        self.assertEqual(amount(result),0)
        self.assertIn('s',result['usage_errors'])
        self.assertIn('토큰: 기록 없음',c.agent_markdown(result,None,None))

    def test_overlapping_worktree_bindings_remain_unassigned(self):
        result=a.scoped(c,[event(0),event(1,100)],
                        [connection('A',pr='143'),connection('B',pr='144')],pr='143')
        self.assertEqual(amount(result),0)
        self.assertIn('s',result['usage_errors'])

    def test_child_inherits_parent_but_explicit_child_binding_takes_priority(self):
        rows=[event(0),event(1,100),event(0,session='child',parent='s'),event(1,20,session='child',parent='s')]
        parent=[connection('A',pr='143')]
        result=a.scoped(c,rows,parent,pr='143')
        self.assertEqual((amount(result),result['turns'],result['sessions']),(120,1,1))
        child=parent+[connection('B',session='child',pr='144')]
        self.assertEqual(amount(a.scoped(c,rows,child,pr='143')),100)
        self.assertEqual(amount(a.scoped(c,rows,child,pr='144')),20)

    def test_active_time_does_not_cross_unbound_start(self):
        result=a.scoped(c,[event(0),event(4,100),event(5,110)],[connection('A',since=3)],task='A')
        self.assertEqual(result['active_sec'],60)

    def test_scope_validation(self):
        for kwargs in [dict(pr='0'),dict(task='A',pr='1'),dict(pr='https://elsewhere/pull/1'),dict(task='missing')]:
            with self.subTest(kwargs=kwargs),self.assertRaises(a.AttributionError):
                a.scoped(c,[],[connection('A')],**kwargs)

    def test_partial_source_does_not_publish_complete_pr_cost(self):
        rows=[event(0),dict(event(1,100),usage_error='partial')]
        result=a.scoped(c,rows,[connection('#140',pr='143')],pr='143')
        self.assertEqual(amount(result),100)
        self.assertNotIn('- 추정 비용',s.agent_text(c,result,None,None,c.DEFAULT_PRICING))

    def test_missing_bound_session_does_not_make_partial_total_look_complete(self):
        result=a.scoped(c,[event(0),event(1,100)],
                        [connection('A',pr='143'),connection('A',session='missing',pr='143')],pr='143')
        self.assertEqual(amount(result),100)
        self.assertIn('missing',result['usage_errors'])
        self.assertNotIn('- 토큰:',s.agent_text(c,result,None,None,c.DEFAULT_PRICING))

    def test_timezone_equivalent_foreign_copies_are_deduplicated(self):
        # This fixture exercises the adapter's canonical interval de-duplication.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            common=types.SimpleNamespace(git=lambda *_:str(root),git_state_path=lambda r,n:Path(r)/n)
            for name,stamp in [('one',ts(0)),('two','2026-10-20T10:00:00Z')]:
                path=root/'worktrees'/name/'harness/agentops/bindings.json';path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'s':{'task':'A','since':stamp,'pr':'143'}}))
            self.assertEqual(len(a.bindings(common,root,[str(root/'one'),str(root/'two')])),1)


class BindingStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.common=types.SimpleNamespace(git=lambda *_:str(self.root),
            _file_lock=lambda _:contextlib.nullcontext(),
            git_state_path=lambda root,name:Path(root)/name)

    def change(self,action,**kwargs):
        return a.change(self.common,self.root,action,**kwargs)

    def test_switch_reconnect_link_and_close_preserve_history(self):
        self.change('bind',task='A',session='s',since=ts(0))
        before=a.state_path(self.common,self.root).read_bytes()
        with self.assertRaises(a.AttributionError):
            self.change('bind',task='A',session='s',since=ts(1))
        self.assertEqual(a.state_path(self.common,self.root).read_bytes(),before)
        self.change('link',task='A',pr='143')
        self.change('bind',task='B',session='s',pr='144',since=ts(2))
        data=self.change('unbind',session='s',since=ts(4))
        self.assertEqual(len(data['bindings']),2)
        self.assertEqual(data['bindings'][0]['until'],ts(2))
        self.assertEqual(data['bindings'][1]['until'],ts(4))
        intervals=a.bindings(self.common,self.root,[])
        self.assertEqual([b['pr'] for b in intervals],['143','144'])

    def test_conflict_and_backfill_do_not_mutate_state(self):
        self.change('bind',task='A',session='s',pr='143',since=ts(2))
        path=a.state_path(self.common,self.root);before=path.read_bytes()
        for action,kwargs in [('link',dict(task='A',pr='144')),('bind',dict(task='B',session='s',since=ts(1))),('bind',dict(task='A',session='s',pr='144'))]:
            with self.subTest(action=action,kwargs=kwargs),self.assertRaises(a.AttributionError):
                self.change(action,**kwargs)
            self.assertEqual(path.read_bytes(),before)

    def test_foreign_141_is_read_only_and_overlaps_not_silently_resolved(self):
        work=self.root/'worktrees'/'worktree';p=work/'harness/agentops/bindings.json';p.parent.mkdir(parents=True)
        foreign={'s':{'task':'B','since':ts(2),'pr':'144','history':[{'task':'A','since':ts(0),'until':ts(2),'pr':'143'}]}}
        p.write_text(json.dumps(foreign),encoding='utf8');before=p.read_bytes()
        intervals=a.bindings(self.common,self.root,[str(work),str(work)])
        self.assertEqual(len(intervals),2)
        self.assertEqual(p.read_bytes(),before)
        self.change('link',task='A',pr='143')
        with self.assertRaises(a.AttributionError):self.change('link',task='B',pr='145')
        self.assertEqual(p.read_bytes(),before)
        self.change('bind',task='C',session='s',since=ts(0),pr='145')
        result=a.scoped(c,[event(0),event(1,100)],a.bindings(self.common,self.root,[str(work)]),pr='143')
        self.assertEqual(amount(result),0)

    def test_corrupt_state_is_not_replaced_or_ignored(self):
        p=a.state_path(self.common,self.root);p.parent.mkdir(parents=True);p.write_text('{}',encoding='utf8')
        with self.assertRaises(a.AttributionError):self.change('bind',task='A',session='s')
        self.assertEqual(p.read_text(),'{}')

    def test_missing_or_credential_like_session_refused(self):
        for session in [None,'../bad session','ghp_'+'x'*30]:
            with self.subTest(session=session),self.assertRaises(a.AttributionError):
                self.change('bind',task='A',session=session)


if __name__=='__main__':unittest.main()
