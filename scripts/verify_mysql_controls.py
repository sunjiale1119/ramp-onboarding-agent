"""Real MySQL/MariaDB locks, receipts and checkpoint restart in a disposable DB.

Uses current configured DB credentials but NEVER changes the configured database.
Requires CREATE/DROP DATABASE privilege; intended for the deployment operator.
"""
import concurrent.futures
import uuid
from datetime import date
from unittest.mock import patch
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command
from ramp import auth, config, db, enterprise, reliability as rel, ticketing
from ramp.checkpointer import MySQLSaver
from ramp.state import RampState, new_state
from ramp.subagents import _make_confirm, _make_execute
from ramp.tools import registry


def main():
    test_name='ramp_acceptance_'+uuid.uuid4().hex[:16]
    operator=create_engine(config.mysql_url(with_db=False))
    with operator.begin() as conn:
        conn.execute(text(f'CREATE DATABASE `{test_name}` CHARACTER SET utf8mb4'))
    eng=create_engine(db.engine().url.set(database=test_name),pool_pre_ping=True)
    factory=sessionmaker(eng,expire_on_commit=False)
    try:
        with patch.object(db,'engine',lambda:eng),patch.object(db,'get_session',factory),patch.object(auth,'get_session',factory),patch.object(config,'MYSQL_DB',test_name):
            db.Base.metadata.create_all(eng)
            rel.migrate(eng)
            with factory() as s:
                s.add(auth.User(username='acceptance',display_name='隔离验收',role='newbie',active=True,salt='unused',pwd_hash='unused'))
                s.add(db.ExtConfig(key='entitlement_catalog',value={'vpn':'VPN'}))
                s.add(auth.User(username='reviewer',display_name='隔离审批',role='mentor',active=True,salt='unused',pwd_hash='unused'))
                s.add(auth.User(username='executor',display_name='隔离执行',role='ops',active=True,salt='unused',pwd_hash='unused'))
                s.add(db.ExtConfig(key='resource_approvers',value={'vpn':{'username':'reviewer','sla_days':1}}))
                s.add(db.Session(id='restart-check',employee_id='acceptance'))
                s.commit()
            def try_lock():
                try:
                    with rel.named_lock('exclusive-test'):return False
                except HTTPException as e:return e.status_code==409
            with rel.named_lock('exclusive-test'),concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(try_lock).result(timeout=10), 'concurrent lock not enforced'
            with rel.named_lock('exclusive-test'):pass
            record = enterprise.RecordIn(employee_id='acceptance',field='leave_balance',value={'total':5,'used':1},
                as_of=date.today(),valid_until=date.today(),source_ref='isolated-mysql-check',expected_revision=0)
            first_record=enterprise.save_record(record,'operator')
            assert first_record['revision']==1 and enterprise.save_record(record,'operator')['revision']==1
            pending=registry.execute('it_create_ticket',{'resource':'vpn','reason':'验收','duration_days':30},domain='it',context={'employee_id':'acceptance'}).pending_action
            assert pending
            def graph():
                g=StateGraph(RampState);g.add_node('confirm',_make_confirm('it'));g.add_node('execute',_make_execute('it'))
                g.add_edge(START,'confirm');g.add_edge('confirm','execute');g.add_edge('execute',END)
                return g.compile(checkpointer=MySQLSaver())
            state=new_state('restart-check','acceptance','申请VPN');state['pending_action']=pending
            cfg={'configurable':{'thread_id':'restart-check'}}
            first=graph().invoke(state,cfg);assert first.get('__interrupt__')
            # New graph + new saver reads the durable checkpoint from MySQL.
            result=graph().invoke(Command(resume={'confirmed':True}),cfg)
            ticket=result['action_result']['ticket_id']
            with factory() as s:
                repeat=ticketing.create(s,'acceptance','vpn','验收',30,pending['action_id'])
                assert repeat['ticket_id']==ticket and s.query(db.Ticket).count()==1
            print('PASS: MySQL mutual exclusion, lock release, persisted interrupt, fresh-saver resume, single-ticket replay')
            reviewer=auth.Principal('reviewer','隔离审批','mentor')
            ticketing.decide(reviewer, ticket, 'approve', 'isolated check')
            enterprise.deliver(reviewer,enterprise.DeliveryIn(ticket_id=ticket,action='assign',executor='executor',note='check',expected_revision=0))
            enterprise.deliver(auth.Principal('executor','隔离执行','ops'),enterprise.DeliveryIn(ticket_id=ticket,action='complete',reference='isolated-proof',note='check',expected_revision=1))
            closed=enterprise.deliver(auth.Principal('acceptance','隔离验收','newbie'),enterprise.DeliveryIn(ticket_id=ticket,action='accept',note='check',expected_revision=2))
            assert closed['status']=='closed'
            print('PASS: MySQL business snapshot replay and approve/assign/deliver/accept transaction chain')
    finally:
        eng.dispose()
        # Only this program-generated, exact disposable schema can be dropped.
        assert test_name.startswith('ramp_acceptance_') and len(test_name)==32
        with operator.begin() as conn:conn.execute(text(f'DROP DATABASE `{test_name}`'))
        operator.dispose()
        print('Removed the disposable acceptance database; application database unchanged.')


if __name__=='__main__':main()
