"""Migration and owner-authored checkpoint boundaries never hide later directions."""
import json
import unittest

import briefing as b
from requirements import canonical_bytes
from test_briefing import PROJECT, TASK, rows, comment, checkpoint


class OwnerBaselineTests(unittest.TestCase):
    def save_as(self, data, cid, actor, hour, *, legacy=False):
        writes=[]
        b.save_checkpoint(data,PROJECT,TASK,checkpoint(data),actor,
                          lambda args:(writes.append(args),json.dumps({'id':cid}))[1],
                          provenance_writes=not legacy)
        data[0]['comments'].append(comment(cid,writes[0][3],
                                          '2026-09-15T%02d:00:00Z'%hour,actor))

    def add(self,data,cid,actor,hour):
        data[0]['comments'].append(comment(cid,'Instruction '+cid,
                                          '2026-09-15T%02d:00:00Z'%hour,actor))

    def assert_directions(self,data,ids):
        before=canonical_bytes(data)
        expected={TASK+'-c'+x for x in ids}
        for _ in range(2):
            brief=b.brief(data,PROJECT,TASK)
            page=b.direction_page(data,PROJECT,TASK)
            self.assertEqual({x['id'] for x in page['items']},expected)
            self.assertEqual(brief['directions']['total'],len(expected))
            self.assertEqual(b.checkpoint_queue_fields(data,data[0])['unresolved_directions'],len(expected))
        self.assertEqual(canonical_bytes(data),before,'Reads must not clear or rewrite directions')

    def test_only_previous_owners_own_checkpoint_sets_the_cutoff(self):
        data=rows();self.add(data,'alice-before','alice/session',12)
        self.save_as(data,'alice-cp','alice/session',12)
        self.add(data,'alice-later','alice/session',13)
        self.add(data,'reviewer','reviewer/session',14)
        # This checkpoint observes Alice as assignee but is NOT Alice's checkpoint.
        self.save_as(data,'operator-cp','operator/session',15)
        data[0]['assignee']='bob/session'
        self.save_as(data,'bob-cp','bob/session',16)
        self.add(data,'bob-own','bob/session',17)
        self.assert_directions(data,['alice-later','reviewer'])

    def test_without_a_previous_owner_checkpoint_no_cutoff_is_invented(self):
        data=rows();self.add(data,'alice-before','alice/session',11)
        self.save_as(data,'operator-cp','operator/session',12)
        data[0]['assignee']='bob/session'
        self.save_as(data,'bob-cp','bob/session',13)
        self.assert_directions(data,['first','alice-before'])

    def test_owner_comments_tied_with_checkpoint_time_remain_possible_directions(self):
        data=rows();self.add(data,'alice-before','alice/session',12)
        self.save_as(data,'alice-cp','alice/session',12)
        # Native second precision cannot establish order within this timestamp.
        self.add(data,'alice-tied','alice/session',12)
        data[0]['assignee']='bob/session'
        self.save_as(data,'bob-cp','bob/session',13)
        self.assert_directions(data,['alice-tied'])

    def test_each_previous_owner_uses_their_last_own_checkpoint(self):
        data=rows();self.save_as(data,'alice-first','alice/session',11)
        self.add(data,'alice-before-last','alice/session',12)
        data[0]['assignee']='bob/session'
        self.add(data,'bob-before','bob/session',13)
        self.save_as(data,'bob-cp','bob/session',14)
        self.add(data,'bob-later','bob/session',15)
        data[0]['assignee']='alice/session'
        self.save_as(data,'alice-last','alice/session',16)
        self.add(data,'alice-later','alice/session',17)
        self.add(data,'reviewer','reviewer/session',18)
        data[0]['assignee']='carol/session'
        self.save_as(data,'carol-cp','carol/session',19)
        self.assert_directions(data,['bob-later','alice-later','reviewer'])

    def test_unassigned_task_uses_checkpoint_author_even_when_owner_field_differs(self):
        data=rows();self.save_as(data,'alice-cp','alice/session',11)
        self.add(data,'alice-later','alice/session',12)
        self.save_as(data,'operator-cp','operator/session',13)
        data[0]['assignee']=None
        self.add(data,'operator-own','operator/session',14)
        self.add(data,'reviewer','reviewer/session',15)
        self.assert_directions(data,['alice-later','reviewer'])

    def test_newest_legacy_baseline_is_used_and_unknown_is_reported_once(self):
        data=rows();self.save_as(data,'legacy-first','alice/session',11,legacy=True)
        self.add(data,'historical','reviewer/session',12)
        self.save_as(data,'legacy-last','alice/session',13,legacy=True)
        self.add(data,'later','reviewer/session',14)
        self.save_as(data,'enabled','alice/session',15)
        self.assert_directions(data,['later'])
        result=b.brief(data,PROJECT,TASK)
        self.assertEqual(sum('Direction baseline:' in x for x in result['warnings']),1)


if __name__=='__main__':
    unittest.main()
