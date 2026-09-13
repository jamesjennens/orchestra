import copy
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from export_requirements import adapt, read_export, reference, revision_comment, selection_from_manifest
from requirement_impact import analyze
from requirements import content_hash
from publish_brd import publish


def baseline():
    return json.loads((Path(__file__).resolve().parents[1]/'docs/requirements-baseline.json').read_text(encoding='utf-8'))


def seal(m):
    for r in m['narrative'] + m['requirements']:
        r['sha256'] = content_hash(r)
    m['sha256'] = content_hash(m)
    return m


def exported(m):
    return [{'id': r['id'], 'title': r['title'], 'description': 'mutable current body',
             'comments': [{'id': 'ordinary', 'text': 'Original discussion'},
                          {'id': 'revision-1', 'text': revision_comment(r), 'author': 'fixture', 'created_at': '2026-09-13T00:00:00Z'}]}
            for r in m['narrative'] + m['requirements']]


def changed(m):
    m=copy.deepcopy(m);m['baseline']='draft-0.2'
    m['requirements'][0]['revision'] += 1
    m['requirements'][0]['description'] += '\nExplicit revised intent.\n'
    return seal(m)


def graph(m):
    return {'schema_version':1, 'nodes':[
        {'id':'design','kind':'design','requirements':[reference(m['requirements'][0])], 'depends_on':[], 'evidence':['old-design']},
        {'id':'task','kind':'task','requirements':[], 'depends_on':['design'], 'evidence':['old-test-pass']},
        {'id':'other','kind':'task','requirements':[reference(m['requirements'][1])], 'depends_on':[], 'evidence':[]}]}


class ExportTests(unittest.TestCase):
    def test_real_snapshot_roundtrip_and_history_preserved(self):
        m=baseline();rows=exported(m)
        result=adapt(rows,selection_from_manifest(m))
        self.assertEqual(result['manifest'],m)
        self.assertEqual(result['provenance']['source_issues'][0]['comments'][0]['text'],'Original discussion')
        self.assertEqual(len(result['provenance']['sources']),13)

    def test_requires_explicit_revision_comments(self):
        m=baseline();rows=exported(m);rows[0]['comments']=[]
        with self.assertRaisesRegex(ValueError,'missing'):adapt(rows,selection_from_manifest(m))

    def test_conflicting_revision_rejected_even_when_original_selected(self):
        m=baseline();rows=exported(m);r=copy.deepcopy(m['narrative'][0]);r['description']='changed';r['sha256']=content_hash(r)
        rows[0]['comments'].append({'id':'conflict','text':revision_comment(r)})
        with self.assertRaisesRegex(ValueError,'conflicting'):adapt(rows,selection_from_manifest(m))

    def test_identity_and_hash_mismatch_and_duplicate_rows(self):
        m=baseline()
        for mutate in (lambda rows: rows.append(rows[0]), lambda rows: rows[0].update(id='wrong'),
                       lambda rows: rows[0]['comments'][1].update(text=rows[0]['comments'][1]['text'].replace('purpose','tampered'))):
            rows=exported(m);mutate(rows)
            with self.assertRaises(ValueError):adapt(rows,selection_from_manifest(m))

    def test_exact_selection_and_context_are_resolved(self):
        m=baseline();selection=selection_from_manifest(m, ['unknown'])
        with self.assertRaises(ValueError):adapt(exported(m),selection)
        selection=selection_from_manifest(m);selection['narrative'][0]['revision']=99
        with self.assertRaises(ValueError):adapt(exported(m),selection)

    def test_prior_revision_cannot_be_rewritten(self):
        old=baseline();new=changed(old);new['requirements'][0]['revision']=1;seal(new)
        with self.assertRaisesRegex(ValueError,'same revision'):adapt(exported(new),selection_from_manifest(new),previous=old)

    def test_jsonl_duplicate_fields_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'export.jsonl';f.write_text('{"id":"a","id":"b"}\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'duplicate'):read_export(f)

    def test_cli_export_and_publish(self):
        m=baseline();root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);source=tmp/'issues.jsonl';selection=tmp/'selection.json'
            source.write_text('\n'.join(json.dumps(r) for r in exported(m)),encoding='utf-8')
            selection.write_text(json.dumps(selection_from_manifest(m)),encoding='utf-8')
            result=subprocess.run([sys.executable,str(root/'export_requirements.py'),'--export',str(source),'--selection',str(selection),'--publish',str(tmp/'published')],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['manifest'],m)
            self.assertTrue((tmp/'published'/m['baseline']/'BRD.md').exists())

    def test_unselected_historical_rewrite_is_rejected(self):
        old=baseline();new=changed(old);rows=exported(new)
        rewritten=copy.deepcopy(old['requirements'][0]);rewritten['description']='rewritten past';rewritten['sha256']=content_hash(rewritten)
        next(row for row in rows if row['id']==rewritten['id'])['comments'].append({'id':'bad-past','text':revision_comment(rewritten)})
        with self.assertRaisesRegex(ValueError,'historical'):adapt(rows,selection_from_manifest(new),previous=old)


class ImpactTests(unittest.TestCase):
    def test_changed_requirement_propagates_and_preserves_evidence(self):
        old=baseline();g=graph(old);before=copy.deepcopy(g)
        result=analyze(old,changed(old),g)
        states={n['id']:n['state'] for n in result['nodes']}
        self.assertEqual(states,{'design':'needs-reassessment','task':'needs-reassessment','other':'unaffected'})
        self.assertEqual(g,before)
        self.assertEqual(result['mode'],'proposed')

    def test_cycle_terminates_and_missing_links_fail(self):
        old=baseline();g=graph(old);g['nodes'][0]['depends_on']=['task']
        self.assertEqual(analyze(old,changed(old),g)['nodes'][0]['state'],'needs-reassessment')
        g['nodes'][0]['depends_on']=['missing']
        with self.assertRaises(ValueError):analyze(old,changed(old),g)

    def test_reaffirmation_requires_exact_new_revision_and_evidence(self):
        old=baseline();new=changed(old);g=graph(old)
        g['nodes'][0]['reaffirmation']={'baseline_sha256':new['sha256'],'requirements':[reference(new['requirements'][0])], 'rationale':'Still valid after review','evidence':'review-2'}
        states={n['id']:n['state'] for n in analyze(old,new,g)['nodes']}
        self.assertEqual(states['design'],'reaffirmed');self.assertEqual(states['task'],'needs-reassessment')
        g['nodes'][1]['reaffirmation']=copy.deepcopy(g['nodes'][0]['reaffirmation'])
        self.assertEqual({n['id']:n['state'] for n in analyze(old,new,g)['nodes']}['task'],'reaffirmed')
        g['nodes'][0]['reaffirmation']['requirements']=[reference(old['requirements'][0])]
        with self.assertRaises(ValueError):analyze(old,new,g)

    def test_downstream_reaffirmation_cannot_hide_upstream_change(self):
        old=baseline();new=changed(old);g=graph(old)
        g['nodes'][1]['reaffirmation']={'baseline_sha256':new['sha256'],'requirements':[reference(new['requirements'][0])], 'rationale':'claim','evidence':'review'}
        states={n['id']:n['state'] for n in analyze(old,new,g)['nodes']}
        self.assertEqual(states['task'],'needs-reassessment')

    def test_removed_requirement_cannot_be_reaffirmed(self):
        old=baseline();new=changed(old);new['requirements'].pop(0);seal(new);g=graph(old)
        self.assertEqual(analyze(old,new,g)['nodes'][0]['state'],'needs-reassessment')
        g['nodes'][0]['reaffirmation']={'baseline_sha256':new['sha256'],'requirements':[], 'rationale':'removed','evidence':'review'}
        with self.assertRaises(ValueError):analyze(old,new,g)

    def test_same_revision_rewrite_and_regression_rejected(self):
        old=baseline();new=changed(old);new['requirements'][0]['revision']=1;seal(new)
        with self.assertRaises(ValueError):analyze(old,new,graph(old))
        with self.assertRaises(ValueError):analyze(changed(old),old,graph(old))

    def test_accepted_change_requires_explicit_decision_and_owner_evidence(self):
        old=baseline();old['state']='accepted'
        for r in old['narrative']+old['requirements']:r['acceptance_state']='accepted'
        seal(old);new=changed(old)
        def approval(m):return {'manifest_sha256':m['sha256'],'decision_id':'owner-fixture','owners':['owner'],'approvers':['owner'],'policy':'any-owner','evidence':'synthetic test only'}
        kwargs={'previous_acceptance':approval(old),'acceptance':approval(new)}
        with self.assertRaises(ValueError):analyze(old,new,graph(old),**kwargs)
        decision={'classification':'scope-change','state':'accepted','decision_id':'decision-2','evidence':'test decision','old_manifest_sha256':old['sha256'],'new_manifest_sha256':new['sha256']}
        self.assertEqual(analyze(old,new,graph(old),change=decision,**kwargs)['mode'],'accepted')
        decision['classification']='defect'
        with self.assertRaises(ValueError):analyze(old,new,graph(old),change=decision,**kwargs)

    def test_export_publish_change_reassess_roundtrip(self):
        old=baseline();new=changed(old)
        with tempfile.TemporaryDirectory() as tmp:
            first=publish(adapt(exported(old),selection_from_manifest(old))['manifest'],tmp)
            before=(first/'BRD.md').read_bytes()
            second=adapt(exported(new),selection_from_manifest(new),previous=old)['manifest']
            publish(second,tmp)
            self.assertEqual(before,(first/'BRD.md').read_bytes())
            self.assertEqual(analyze(old,second,graph(old))['changed'],[old['requirements'][0]['id']])

    def test_multigeneration_evidence_uses_history_without_rewriting_refs(self):
        first=baseline();second=changed(first);third=changed(second);third['baseline']='draft-0.3';seal(third)
        g=graph(first)
        for node in g['nodes'][:2]:
            node['reaffirmation']={'baseline_sha256':second['sha256'],'requirements':[reference(second['requirements'][0])],'rationale':'second review','evidence':'review-v2'}
        before=copy.deepcopy(g)
        result=analyze(second,third,g,history=[{'manifest':first,'acceptance':None}])
        self.assertEqual(result['nodes'][0]['state'],'needs-reassessment')
        self.assertEqual(g,before)
        unrelated=copy.deepcopy(second);unrelated['baseline']='draft-unrelated';unrelated['requirements'][2]['revision']+=1;unrelated['requirements'][2]['description']+=' changed';seal(unrelated)
        result=analyze(second,unrelated,g,history=[{'manifest':first,'acceptance':None}])
        self.assertTrue(all(n['state']=='unaffected' for n in result['nodes']))
        g['nodes'][0]['reaffirmation']['baseline_sha256']=unrelated['sha256']
        states={n['id']:n['state'] for n in analyze(second,unrelated,g,history=[{'manifest':first,'acceptance':None}])['nodes']}
        self.assertEqual(states,{'design':'reaffirmed','other':'unaffected','task':'unaffected'})


if __name__=='__main__':unittest.main()
