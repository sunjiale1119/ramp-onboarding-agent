"""Isolated business records and manual fulfillment tests. No enterprise or LLM calls."""
import hashlib
import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from ramp import api, auth, db, enterprise as ent, external, ticketing
from tests import test_service_controls as fixtures


class EnterpriseTest(unittest.TestCase):
    setUp = fixtures.ControlsTest.setUp
    actor = fixtures.ControlsTest.actor
    ticket = fixtures.ControlsTest.ticket

    def body(self, **changes):
        value = dict(employee_id='alice', field='leave_balance', value={'total':5,'used':1},
                     as_of=str(date.today()), valid_until=str(date.today()+timedelta(days=7)),
                     source_ref='HR-test-reference', expected_revision=0)
        value.update(changes)
        return value

    def save(self, **changes):
        return self.client.post('/api/enterprise/records', json=self.body(**changes))

    def test_record_owner_and_self_access_denied(self):
        for actor in ('alice','mentor1','ops'):
            self.actor(actor)
            self.assertEqual(self.save().status_code,403)
            self.assertEqual(self.client.get('/api/enterprise/records/alice').status_code,403)
        self.actor('hr')
        self.assertEqual(self.save(field='entitlements',value={'granted':['vpn']}).status_code,403)
        self.assertEqual(self.save().status_code,200)

    def test_snapshot_replay_and_conflict(self):
        self.actor('admin')
        first=self.save(); self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()['value']['remaining'],4)
        self.assertEqual(first.json(),self.save().json())
        self.assertEqual(self.save(value={'total':6,'used':1}).status_code,409)
        self.assertEqual(self.save(value={'total':6,'used':1},expected_revision=1).status_code,200)
        self.assertEqual(self.s.query(ent.BusinessRecord).count(),2)

    def test_expired_and_revoked_do_not_resurrect(self):
        self.actor('admin'); self.save()
        result=self.save(revoked=True, value={}, expected_revision=1)
        self.assertEqual(result.status_code,200,result.text)
        with self.assertRaises(external.NotConnected):ent.read(self.s,'alice','leave_balance')
        self.assertFalse(external.status(self.s)[0]['ready'])
        self.assertEqual(self.s.query(ent.BusinessRecord).count(),2)

    def test_expired_snapshot_unavailable(self):
        self.actor('admin');self.save()
        row=self.s.query(ent.BusinessRecord).one();row.valid_until=date.today()-timedelta(days=1);self.s.commit()
        with self.assertRaises(external.NotConnected):ent.read(self.s,'alice','leave_balance')

    def test_no_record_does_not_infer_from_onboard_date(self):
        with patch.dict(os.environ,{'RAMP_EXTERNAL_MODE':'builtin'}):
            for field in ('probation','social_insurance','leave_balance'):
                with self.assertRaises(external.NotConnected):external.hr_field(self.s,'alice',field)

    def test_validation(self):
        self.actor('admin')
        for changes in (
            {'employee_id':'missing'}, {'field':'salary'}, {'source_ref':' '},
            {'as_of':str(date.today()+timedelta(days=1))},
            {'valid_until':str(date.today()+timedelta(days=91))},
            {'value':{'total':1,'used':2}}, {'value':{'total':True,'used':0}},
            {'value':{'total':1,'used':0,'role':'admin'}},
            {'field':'entitlements','value':{'granted':['unknown']}},
            {'field':'social_insurance','value':{'status':'guessed'}},
            {'field':'probation','value':{'probation_end':'bad-date'}},
        ):
            with self.subTest(changes=changes):self.assertEqual(self.save(**changes).status_code,400)
        self.assertEqual(self.save(role='admin').status_code,422)

    def test_catalog_revision_and_resource_references(self):
        self.actor('admin')
        revision=self.client.get('/api/enterprise/catalog').json()['revision']
        data={'key':'doc_catalog','value':{'id':'身份核验'},'expected_revision':revision}
        self.assertEqual(self.client.post('/api/enterprise/catalog',json=data).status_code,200)
        self.assertEqual(self.client.post('/api/enterprise/catalog',json=data).status_code,409)
        revision=self.client.get('/api/enterprise/catalog').json()['revision']
        self.assertEqual(self.client.post('/api/enterprise/catalog',json={'key':'entitlement_catalog','value':{},'expected_revision':revision}).status_code,409)

    def test_ingest_https_token_scope_and_existing_identity(self):
        digest=hashlib.sha256(b'test-only-inbound-token').hexdigest()
        with patch.dict(os.environ,{'RAMP_DATA_INGEST_TOKEN_SHA256':digest,'RAMP_DATA_INGEST_FIELDS':'leave_balance'}):
            secure=TestClient(api.app,base_url='https://testserver',raise_server_exceptions=False)
            self.addCleanup(secure.close)
            headers={'Authorization':'Bearer test-only-inbound-token'}
            self.assertEqual(self.client.post('/api/enterprise/ingest',json=self.body(),headers=headers).status_code,400)
            self.assertEqual(secure.post('/api/enterprise/ingest',json=self.body()).status_code,401)
            self.assertEqual(secure.post('/api/enterprise/ingest',json=self.body(field='entitlements',value={'granted':[]}),headers=headers).status_code,403)
            self.assertEqual(secure.post('/api/enterprise/ingest',json=self.body(employee_id='missing'),headers=headers).status_code,400)
            r=secure.post('/api/enterprise/ingest',json=self.body(),headers=headers)
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['source_kind'],'integration')
            self.assertEqual(secure.post('/api/enterprise/ingest',json=self.body(),headers=headers).json(),r.json())
            q=secure.get('/api/enterprise/ingest/revision?employee_id=alice&field=leave_balance',headers=headers)
            self.assertEqual(q.json(),{'revision':1})
            self.actor('admin')
            self.assertEqual(self.save(expected_revision=1).status_code,409)

    def action(self, ticket_id, action, revision, **fields):
        return self.client.post('/api/enterprise/delivery',json=dict(ticket_id=ticket_id,action=action,
            expected_revision=revision,note='isolated acceptance check',**fields))

    def approve(self,ticket_id):
        self.actor('mentor1')
        response=self.client.post('/api/tickets/decision',json={'ticket_id':ticket_id,'action':'approve','note':'test approval'})
        self.assertEqual(response.status_code,200,response.text)

    def test_full_delivery_acceptance_and_duplicate_prevention(self):
        tid=self.ticket()['ticket_id'];self.approve(tid)
        with self.assertRaises(ValueError):self.ticket('another-action')
        r=self.action(tid,'assign',0,executor='ops');self.assertEqual(r.status_code,200,r.text)
        self.actor('ops')
        self.assertTrue(any(t['ticket_id']==tid for t in self.client.get('/api/tickets').json()))
        self.assertEqual(self.action(tid,'complete',1).status_code,400)
        r=self.action(tid,'complete',1,reference='IT-actual-reference');self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.action(tid,'complete',1,reference='IT-actual-reference').status_code,409)
        self.actor('alice')
        t=self.client.get('/api/tickets').json()[0]
        self.assertTrue(t['can_accept']);self.assertEqual(t['delivery_reference'],'IT-actual-reference')
        self.assertEqual(self.action(tid,'accept',2).json()['status'],'closed')
        self.assertEqual(self.s.query(ent.BusinessRecord).count(),0)

    def test_delivery_failure_reassign_reopen(self):
        tid=self.ticket()['ticket_id'];self.approve(tid)
        self.action(tid,'assign',0,executor='ops');self.actor('ops')
        self.assertEqual(self.action(tid,'fail',1).json()['status'],'delivery_failed')
        self.actor('admin');self.assertEqual(self.action(tid,'assign',2,executor='ops').status_code,200)
        self.actor('ops');self.action(tid,'complete',3,reference='IT-ref')
        self.actor('alice');self.assertEqual(self.action(tid,'reopen',4).json()['status'],'fulfilling')

    def test_delivery_permissions(self):
        tid=self.ticket()['ticket_id'];self.approve(tid)
        self.actor('bob');self.assertEqual(self.action(tid,'assign',0,executor='ops').status_code,403)
        self.actor('admin');self.assertEqual(self.action(tid,'assign',0,executor='alice').status_code,400)
        self.assertEqual(self.action(tid,'assign',0,executor='ops').status_code,200)
        self.assertEqual(self.action(tid,'complete',1,reference='x').status_code,403)
        self.actor('ops');self.action(tid,'complete',1,reference='x')
        self.actor('bob');self.assertEqual(self.action(tid,'accept',2).status_code,403)

    def test_missing_approver_fails_before_submission(self):
        self.s.get(db.ExtConfig,'resource_approvers').value={};self.s.commit()
        with self.assertRaises(ValueError):self.ticket()
        self.assertEqual(self.s.query(db.Ticket).count(),0)


if __name__=='__main__':unittest.main()
