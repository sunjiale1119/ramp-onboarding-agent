"""Isolated UI screenshot server. No personal database or model API is used.

Run with the project environment, then capture http://127.0.0.1:8766.
SQLite is used only for this temporary UI preview; deployment uses MySQL/MariaDB.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
for key in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "LANGSMITH_API_KEY"):
    os.environ.pop(key, None)
os.environ["RAMP_EMBEDDING_BACKEND"] = "hashing"
os.environ["RAMP_EXTERNAL_MODE"] = "builtin"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from ramp import auth, config, db, demo, knowledge

preview_dir = Path(tempfile.mkdtemp(prefix="ramp-ui-preview-"))
db._engine = create_engine("sqlite:///" + (preview_dir / "preview.sqlite").as_posix(),
                           connect_args={"check_same_thread": False})
db._Session = sessionmaker(bind=db._engine)
db.Base.metadata.create_all(db._engine)
db.ping = lambda: (True, "SQLite · isolated UI preview")
auth.ADMIN_USERNAME = "admin"
auth.ADMIN_PASSWORD = "ramp2026"
auth.seed_users()
demo.load()
with db.get_session() as session:
    knowledge.seed_from_file(session)
knowledge.reload_index()

if __name__ == "__main__":
    import uvicorn
    from ramp.api import app
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")
