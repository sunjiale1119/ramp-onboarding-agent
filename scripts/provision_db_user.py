"""Run once inside the old root-configured container. Prints no credentials.

Usage: python scripts/provision_db_user.py /secure-mounted-deploy/.env
The deployment directory must be mounted explicitly; file remains mode 0600.
"""
from pathlib import Path
import os
import secrets
from sqlalchemy import text
from dotenv import dotenv_values
from ramp import db


def provision(path):
    target = Path(path)
    if target.name != '.env' or not target.is_file():
        raise ValueError('Expected an existing deployment .env')
    values=dotenv_values(target)
    if values.get('RAMP_DB_USER') == 'ramp_app' and values.get('RAMP_DB_PASSWORD'):
        print('Application credential already provisioned; unchanged.')
        return
    password=secrets.token_hex(32)
    with db.engine().begin() as conn:
        conn.execute(text("CREATE USER IF NOT EXISTS 'ramp_app'@'%' IDENTIFIED BY :password"),{'password':password})
        conn.execute(text("ALTER USER 'ramp_app'@'%' IDENTIFIED BY :password"),{'password':password})
        conn.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES ON ramp.* TO 'ramp_app'@'%'") )
    original=target.read_text(encoding='utf-8-sig')
    lines=[line for line in original.splitlines() if not line.startswith(('RAMP_DB_USER=','RAMP_DB_PASSWORD='))]
    target.write_text('\n'.join(lines)+f'\nRAMP_DB_USER=ramp_app\nRAMP_DB_PASSWORD={password}\n',encoding='utf-8')
    os.chmod(target,0o600)
    print('Provisioned database-scoped application account; secret stored only in deployment .env.')


if __name__=='__main__':
    import sys
    provision(sys.argv[1])
