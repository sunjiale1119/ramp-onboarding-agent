"""Authorization and failure injection without real users or paid model calls."""
import os
import unittest
from datetime import date
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ramp import api, auth, db, external, pilot, reliability as rel, ticketing, trace
from ramp.knowledge_versions import migrate as migrate_kb
from ramp.tools import registry


class ControlsTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread":False}, poolclass=StaticPool)
        db.Base.metadata.create_all(self.engine)
        self.Factory = sessionmaker(self.engine, expire_on_commit=False)
        for obj, name, value in ((db,"engine",lambda:self.engine),(db,"get_session",self.Factory),(auth,"get_session",self.Factory)):
            p=patch.object(obj,name,value); p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)
        migrate_kb(self.engine)
        self.s=self.Factory(); self.addCleanup(self.s.close)
        for name,role,mentor in (("alice","newbie","mentor1"),("bob","newbie","mentor2"),
                                  ("mentor1","mentor",None),("mentor2","mentor",None),
                                  ("admin","admin",None),("ops","ops",None),("hr","hr",None)):
            salt,h=auth.hash_password("test-only-password")
            self.s.add(auth.User(username=name,display_name=name,role=role,mentor=mentor,
                active=True,onboard_date=date.today(),salt=salt,pwd_hash=h))
        self.s.add(db.Session(id="alice-session",employee_id="alice"))
        self.s.add(db.Session(id="bob-session",employee_id="bob"))
        self.s.add(db.Escalation(id=1,session_id="bob-session",employee_id="bob",mentor_id="mentor2",domain="hr",question="private"))
        self.s.add(db.ExtConfig(key="entitlement_catalog",value={"vpn":"VPN"}))
        self.s.add(db.ExtConfig(key="resource_approvers",value={"vpn":{"username":"mentor1","sla_days":1}}))
        self.s.commit()
        self.client=TestClient(api.app,raise_server_exceptions=False)
        self.addCleanup(api.app.dependency_overrides.clear)
        self.actor("alice")

    def actor(self,name):
        u=self.s.get(auth.User,name)
        api.app.dependency_overrides[api.current]=lambda:auth.Principal(u.username,u.display_name,u.role)

    def ask_body(self,**changes):
        b=dict(question="test question",employee_id="alice",request_id="test-request-001",session_id=None)
        b.update(changes);return b

    def ticket(self, key="action-1", owner="alice"):
        with self.Factory() as s:
            return ticketing.create(s,owner,"vpn","testing",30,key)

    def test_newbie_cannot_use_another_employee(self):
        with patch("ramp.runtime.ask") as fn:
            self.assertEqual(self.client.post('/api/newbie/ask',json=self.ask_body(employee_id='bob')).status_code,403)
            fn.assert_not_called()

    def test_newbie_cannot_hijack_session(self):
        with patch("ramp.runtime.ask") as fn:
            self.assertEqual(self.client.post('/api/newbie/ask',json=self.ask_body(session_id='bob-session')).status_code,403)
            fn.assert_not_called()

    def test_newbie_cannot_confirm_another_session(self):
        with patch("ramp.runtime.resume") as fn:
            r=self.client.post('/api/newbie/confirm',json=dict(session_id='bob-session',action_id='x',confirmed=True))
            self.assertEqual(r.status_code,403);fn.assert_not_called()

    def test_role_matrix_denies_protected_routes(self):
        for name,url in (("alice","/api/admin/users"),("mentor1","/api/hr/dashboard"),("hr","/api/newbie/alice/memory"),
                         ("ops","/api/admin/users"),("admin","/api/newbie/alice/memory"),("alice","/api/ops/audit")):
            with self.subTest(name=name,url=url):
                self.actor(name);self.assertEqual(self.client.get(url).status_code,403)

    def test_mentor_cannot_access_other_assignment(self):
        self.actor('mentor1')
        for url in ('/api/mentor/mentor2/mentees','/api/mentor/mentor2/escalations','/api/mentor/view/bob'):
            self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.post('/api/mentor/answer',json=dict(escalation_id=1,answer='reply')).status_code,403)

    def test_hr_individual_endpoint_removed(self):
        self.actor('hr');self.assertEqual(self.client.get('/api/hr/view/alice').status_code,404)

    def test_duplicate_ask_runs_model_once(self):
        with patch('ramp.runtime.ask',side_effect=lambda *a,**k:dict(session_id=k['session_id'],answer='test',cost=0)) as fn:
            a=self.client.post('/api/newbie/ask',json=self.ask_body())
            b=self.client.post('/api/newbie/ask',json=self.ask_body())
            self.assertEqual(a.status_code,200,a.text);self.assertEqual(a.json(),b.json());self.assertEqual(fn.call_count,1)

    def test_real_confirmation_nodes_resume_and_replay(self):
        from langgraph.graph import StateGraph, START, END
        from langgraph.checkpoint.memory import InMemorySaver
        from ramp.state import RampState, new_state
        from ramp.subagents import _make_confirm, _make_execute
        saver=InMemorySaver()
        def build():
            g=StateGraph(RampState);g.add_node('confirm',_make_confirm('it'));g.add_node('execute',_make_execute('it'))
            g.add_edge(START,'confirm');g.add_edge('confirm','execute');g.add_edge('execute',END)
            return g.compile(checkpointer=saver)
        p=registry.execute('it_create_ticket',dict(resource='vpn',reason='testing',duration_days=30),domain='it',context={'employee_id':'alice'})
        self.assertTrue(p.ok,p.error)
        state=new_state('alice-session','alice','apply');state['pending_action']=p.pending_action
        g=build();g.invoke(state,{'configurable':{'thread_id':'alice-session'}})
        # Recreate the graph between interruption and confirmation.
        g=build()
        with patch('ramp.graph.compiled',return_value=g):
            body=dict(session_id='alice-session',action_id=p.pending_action['action_id'],confirmed=True)
            a=self.client.post('/api/newbie/confirm',json=body)
            self.assertEqual(a.status_code,200,a.text)
            self.assertEqual(a.json()['action_result']['status'],'pending_approval')
            b=self.client.post('/api/newbie/confirm',json=body)
            self.assertEqual(a.json(),b.json());self.assertEqual(self.s.query(db.Ticket).count(),1)
            body['confirmed']=False
            self.assertEqual(self.client.post('/api/newbie/confirm',json=body).status_code,409)

    def test_request_key_cannot_change_content(self):
        rel.run_once('alice','key','ask',{},lambda sid:dict(answer='ok'))
        with self.assertRaises(HTTPException) as e:rel.run_once('alice','key','ask',{'changed':True},lambda sid:{})
        self.assertEqual(e.exception.status_code,409)

    def test_uncertain_ask_is_not_blindly_retried(self):
        with self.assertRaises(RuntimeError):
            rel.run_once('alice','broken','ask',{},lambda sid:(_ for _ in ()).throw(RuntimeError('fail after execution')))
        fn=lambda sid:self.fail('must not run model again')
        with self.assertRaises(HTTPException):rel.run_once('alice','broken','ask',{},fn)
        self.assertEqual(rel.requests('alice')[0]['status'],'uncertain')

    def test_quota_rejects_before_model(self):
        with patch.dict(os.environ,{'RAMP_DAILY_ASK_LIMIT':'0'}),patch('ramp.runtime.ask') as fn:
            self.assertEqual(self.client.post('/api/newbie/ask',json=self.ask_body()).status_code,429);fn.assert_not_called()

    def test_duplicate_ticket_receipt_survives_new_session(self):
        first=self.ticket();second=self.ticket();self.assertEqual(first,second)
        self.assertEqual(self.s.query(db.Ticket).count(),1)

    def test_crash_after_ticket_commit_replays_receipt(self):
        def crash(sid):self.ticket();raise RuntimeError('crashed after commit')
        with self.assertRaises(RuntimeError):rel.run_once('alice','confirm-action1','confirm',{'session_id':'alice-session'},crash)
        result=rel.run_once('alice','confirm-action1','confirm',{'session_id':'alice-session'},lambda sid:self.ticket())
        self.assertEqual(result['ticket_id'],self.ticket()['ticket_id'])
        self.assertEqual(self.s.query(db.Ticket).count(),1)

    def test_same_resource_pending_duplicate_rejected(self):
        self.ticket()
        with self.assertRaises(ValueError):self.ticket('different-action')

    def test_action_id_cannot_be_reused_by_other_owner(self):
        self.ticket()
        with self.assertRaises(ValueError):self.ticket(owner='bob')

    def test_ticket_validation_duration_resource_reason(self):
        for resource,reason,days in (('missing','why',30),('vpn','',30),('vpn','why',0),('vpn','why',366),('vpn','why',True)):
            with self.subTest(resource=resource,days=days),self.assertRaises(ValueError):ticketing.validate(self.s,'alice',resource,reason,days)

    def test_changed_approval_requires_new_preview(self):
        fields=external.ticket_fields(self.s,'alice','vpn','testing',30)
        fields['审批人']='old person'
        with self.assertRaises(ValueError):ticketing.create(self.s,'alice','vpn','testing',30,'new',fields)

    def test_only_assignee_can_approve(self):
        t=self.ticket()
        for name in ('alice','mentor2','admin'):
            self.actor(name)
            self.assertEqual(self.client.post('/api/tickets/decision',json=dict(ticket_id=t['ticket_id'],action='approve',note='checked')).status_code,403)
        self.actor('mentor1');b=dict(ticket_id=t['ticket_id'],action='approve',note='checked')
        self.assertEqual(self.client.post('/api/tickets/decision',json=b).status_code,200)
        self.assertTrue(self.client.post('/api/tickets/decision',json=b).json()['replayed'])
        b['action']='reject';self.assertEqual(self.client.post('/api/tickets/decision',json=b).status_code,409)

    def test_owner_can_cancel_pending_but_not_approved(self):
        t=self.ticket();p=auth.Principal('alice','Alice','newbie')
        ticketing.decide(p,t['ticket_id'],'cancel','not needed')
        self.assertEqual(ticketing.decide(p,t['ticket_id'],'cancel','retry')['status'],'cancelled')

    def test_ticket_list_row_filter(self):
        t=self.ticket();self.actor('bob');self.assertEqual(self.client.get('/api/tickets').json(),[])
        self.actor('mentor1');self.assertEqual(len(self.client.get('/api/tickets').json()),1)

    def test_tool_rejects_internal_context_injection(self):
        r=registry.execute('it_create_ticket',{'resource':'vpn','reason':'x','_context':{'employee_id':'bob'}},domain='it',context={'employee_id':'alice'})
        self.assertFalse(r.ok);self.assertEqual(self.s.query(db.Ticket).count(),0)

    def test_tool_preview_error_is_caught(self):
        r=registry.execute('it_create_ticket',{'resource':'not-allowed','reason':'x'},domain='it',context={'employee_id':'alice'})
        self.assertFalse(r.ok)

    def test_tool_commit_rechecks_owner_and_domain(self):
        p={'tool':'it_create_ticket','args':{},'owner':'alice','action_id':'test'}
        for ctx in ({'employee_id':'bob','domain':'it'},{'employee_id':'alice','domain':'hr'}):self.assertFalse(registry.commit(p,ctx).ok)

    def test_feedback_visibility_and_resolution(self):
        r=self.client.post('/api/pilot/feedback',json=dict(category='知识问答',rating=2,content='测试反馈'))
        self.assertEqual(r.status_code,200);fid=r.json()['id']
        self.actor('bob');self.assertEqual(self.client.get('/api/pilot/feedback').json(),[])
        self.assertEqual(self.client.post('/api/pilot/feedback/resolve',json=dict(id=fid,status='resolved',resolution='fixed')).status_code,403)
        self.actor('ops');self.assertEqual(len(self.client.get('/api/pilot/feedback').json()),1)
        self.assertEqual(self.client.post('/api/pilot/feedback/resolve',json=dict(id=fid,status='resolved',resolution='fixed')).status_code,200)

    def test_pilot_no_fake_metrics(self):
        d=pilot.dashboard();self.assertEqual(d['participants'],0);self.assertIsNone(d['completion_rate'])

    def test_csrf_and_wrong_content_type_blocked(self):
        self.assertEqual(self.client.post('/api/login',json={},headers={'Origin':'https://evil.test'}).status_code,403)
        self.assertEqual(self.client.post('/api/login',data='username=admin').status_code,415)

    def test_oversized_body_blocked(self):
        self.assertEqual(self.client.post('/api/pilot/feedback',json={'content':'x'*140000}).status_code,413)

    def test_no_paid_probe_for_health(self):
        with (patch('ramp.llm.health',side_effect=AssertionError('must not call model')),
              patch('ramp.knowledge.index') as idx):
            idx.return_value.size=0
            self.assertEqual(self.client.get('/health/ready').status_code,200)

    def test_trace_does_not_leak_tool_args_or_errors(self):
        self.s.add(db.Trace(session_id='alice-session',span='tool',turn=1,detail={'args':{'reason':'private'},'error':'secret','domain':'it'}));self.s.commit()
        d=trace.waterfall('alice-session')[0]['detail'];self.assertEqual(d,{'domain':'it'})

    def test_disabled_and_role_changed_sessions_revoked(self):
        token=auth.login('alice','test-only-password');self.assertIsNotNone(auth.resolve(token))
        auth.update_user('alice',active=False);self.assertIsNone(auth.resolve(token))
        token=auth.login('mentor1','test-only-password');auth.update_user('mentor1',role='ops');self.assertIsNone(auth.resolve(token))

    def test_last_admin_cannot_disable_or_demote(self):
        self.assertFalse(auth.update_user('admin',active=False)[0]);self.assertFalse(auth.update_user('admin',role='newbie')[0])

    def test_real_cookie_auth_and_logout(self):
        api.app.dependency_overrides.clear()
        self.assertEqual(self.client.get('/api/tickets').status_code,401)
        r=self.client.post('/api/login',json={'username':'alice','password':'test-only-password'})
        self.assertEqual(r.status_code,200)
        self.assertIn('httponly',r.headers['set-cookie'].lower())
        self.assertEqual(self.client.get('/api/tickets').status_code,200)
        self.assertEqual(self.client.get('/api/admin/users').status_code,403)
        self.assertEqual(self.client.post('/api/logout',json={}).status_code,200)
        self.assertEqual(self.client.get('/api/tickets').status_code,401)

    def test_login_throttling(self):
        from fastapi import FastAPI
        from ramp.security import BoundaryMiddleware
        isolated=FastAPI();isolated.add_middleware(BoundaryMiddleware)
        @isolated.post('/api/login')
        def dummy():return {'ok':False}
        c=TestClient(isolated)
        for _ in range(30):self.assertEqual(c.post('/api/login',json={}).status_code,200)
        self.assertEqual(c.post('/api/login',json={}).status_code,429)

    def test_admin_rejects_invalid_people_and_config(self):
        self.actor('admin')
        for key,value in (('contacts',{'hr':'nonexistent'}),('resource_approvers',{'vpn':{'username':'nonexistent'}}),
                          ('resource_approvers',{'vpn':{'sla_days':-1}}),('entitlement_catalog',[]),
                          ('role_entitlements',{'newbie':['unknown']})):
            self.assertEqual(self.client.post('/api/admin/external/config',json={'key':key,'value':value}).status_code,400)
        revision=self.client.get('/api/enterprise/catalog').json()['revision']
        self.assertEqual(self.client.post('/api/admin/external/config',json={'key':'contacts','value':{'hr':'mentor1'},'expected_revision':revision}).status_code,200)

    def test_admin_rejects_invalid_business_state(self):
        self.actor('admin')
        for body in ({'fund_base':'abc'},{'fund_base':-1},{'leave_used':-2},{'leave_used':'NaN'},
                     {'social_from':'not-date'},{'social_status':'made-up'},{'granted':['unknown']}):
            self.assertEqual(self.client.post('/api/admin/profile/alice',json=body).status_code,410)
        self.assertEqual(self.client.post('/api/admin/profile/alice',json={'social_status':'pending','granted':['vpn'],'leave_used':1}).status_code,410)

    def test_registration_cannot_self_assign_admin(self):
        response=self.client.post('/api/register',json=dict(username='untrusted',password='test-password',display_name='test',role='admin',active=True))
        self.assertEqual(response.status_code,200)
        u=self.s.get(auth.User,'untrusted');self.assertEqual(u.role,'newbie');self.assertFalse(u.active)


if __name__=='__main__':unittest.main()
