"""Run once before serving an upgraded database; safe to run repeatedly."""
from . import db, knowledge_versions

if __name__ == "__main__":
    db.create_database()
    print(f"知识版本迁移完成：{knowledge_versions.migrate()} 条已有知识登记为历史 v1；原内容未改动。")
