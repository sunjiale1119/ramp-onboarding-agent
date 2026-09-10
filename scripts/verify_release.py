"""Read-only deployment checks, apart from opening/revoking the operator's login session.

Run inside the application container; never prints credentials or user records.
Uses ADMIN_USERNAME/ADMIN_PASSWORD, or the documented demo defaults.
"""
import os
import httpx
from sqlalchemy import text
from ramp import config, db
from ramp.checkpointer import MySQLSaver


def main():
    expected = os.environ['RAMP_RELEASE']
    assert config.MYSQL_USER == 'ramp_app', 'Application must not use database root'
    with db.get_session() as s:
        grants = [str(row[0]) for row in s.execute(text('SHOW GRANTS')).all()]
        assert not any('ALL PRIVILEGES' in g or 'GRANT OPTION' in g or ' DROP' in g for g in grants)
        assert any('`ramp`.*' in g for g in grants), 'Missing database-scoped grants'
        counts = {name: s.execute(text(f'SELECT COUNT(*) FROM `{name}`')).scalar()
                  for name in ('users', 'sessions', 'messages', 'tickets')}
    MySQLSaver().get_tuple({'configurable': {'thread_id': 'release-read-only-probe'}})
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=20) as c:
        for path in ('/health/live', '/health/ready', '/login'):
            assert c.get(path).status_code == 200, 'Unhealthy: ' + path
        for path in ('/api/tickets', '/api/health', '/api/admin/users'):
            assert c.get(path).status_code == 401, 'Unauthenticated access: ' + path
        r = c.post('/api/login', json={'username': os.getenv('ADMIN_USERNAME', 'admin'),
                                     'password': os.getenv('ADMIN_PASSWORD', 'ramp2026')})
        assert r.status_code == 200, 'Operator login unavailable; verify current credentials privately'
        try:
            data = c.get('/api/ops/readiness').json()
            assert data['release'] == expected and data['ready'], 'Wrong release or incomplete migration'
            assert c.get('/workspace').status_code == 200
            assert c.get('/api/admin/external').json()['demo_load_allowed'] is False
            assert c.get('/api/admin/users').status_code == 200
        finally:
            c.post('/api/logout', json={})
        assert c.get('/api/tickets').status_code == 401
    print('PASS: release, readiness, database-scoped account, checkpoint access, authentication, logout, demo loading disabled')
    print('Existing record counts (no content):', counts)
    print('Release:', expected)


if __name__ == '__main__':
    main()
