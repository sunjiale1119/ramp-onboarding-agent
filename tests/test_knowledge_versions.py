"""Lifecycle and retrieval regressions; isolated SQLite, no real users or API calls."""
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ramp import db, knowledge, knowledge_versions as kv


class VersionsTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        db.Knowledge.__table__.create(self.engine)
        db.Escalation.__table__.create(self.engine)
        self.Factory = sessionmaker(self.engine, expire_on_commit=False)
        self.patch = patch.object(db, "get_session", self.Factory)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.engine.dispose)
        kv.migrate(self.engine)
        self.s = self.Factory()
        self.addCleanup(self.s.close)
        knowledge._index = None
        self.today = date.today()

    def body(self, **overrides):
        return dict(domain="hr", question="住宿报销上限是多少？", answer="上限为 500 元。",
                    source_level="L1", source_name="差旅管理办法 · 第3条", topic="住宿报销上限",
                    scope="*", effective_from=self.today.isoformat(), change_note="更新标准", **overrides)

    def draft(self, **changes):
        b = self.body()
        b.update(changes)
        return kv.save_draft(self.s, b, "editor")

    def published(self, draft, **changes):
        v = kv.get_version(self.s, draft["id"])
        r = kv.review(self.s, v)
        body = dict(id=v.id, revision=v.revision, review_token=r["review_token"], reviewed=True,
                    review_note="已核对原制度，同时间同范围无冲突", acknowledged=[x["id"] for x in r["candidates"]])
        body.update(changes)
        return kv.publish(self.s, body, "publisher", lambda text: [1.0, 0.0])

    def new_version(self, source, **changes):
        return self.draft(id=source["id"], revision=source["revision"], **changes)

    def active(self, day=None, scope=None):
        return kv.active_metadata(self.s, as_of=day, scope=scope)

    def test_draft_not_searchable(self):
        d = self.draft()
        self.assertEqual(self.active(), {})
        p = self.published(d)
        self.assertIn(p["knowledge_id"], self.active())

    def test_future_switch_and_historical_query(self):
        old = self.published(self.draft())
        tomorrow = self.today + timedelta(days=1)
        new = self.published(self.new_version(old, answer="上限为 600 元。", effective_from=str(tomorrow)))
        self.assertEqual(set(self.active()), {old["knowledge_id"]})
        self.assertEqual(set(self.active(tomorrow)), {new["knowledge_id"]})
        self.assertEqual(set(self.active(self.today)), {old["knowledge_id"]})
        self.assertEqual(kv.serialize(self.s, kv.get_version(self.s, old["id"]), tomorrow)["state"], "superseded")

    def test_expiry_does_not_resurrect_old_version(self):
        old = self.published(self.draft())
        tomorrow = self.today + timedelta(days=1)
        new = self.published(self.new_version(old, answer="上限 600 元", effective_from=str(tomorrow), expires_on=str(tomorrow)))
        self.assertIn(new["knowledge_id"], self.active(tomorrow))
        self.assertEqual(self.active(tomorrow + timedelta(days=1)), {})

    def test_expired_and_future_never_in_prompt_material(self):
        p = self.published(self.draft(expires_on=str(self.today)))
        with self.Factory() as session:
            index = knowledge.KnowledgeIndex().load(session, as_of=self.today + timedelta(days=1))
        self.assertEqual(index.size, 0)

    def test_topic_conflict_blocks_even_if_acknowledged(self):
        self.published(self.draft())
        other = self.draft(answer="上限 999 元")
        self.assertTrue(kv.review(self.s, kv.get_version(self.s, other["id"]))["blockers"])
        with self.assertRaises(kv.ConflictError):
            self.published(other)
        self.s.rollback()
        self.assertEqual(kv.get_version(self.s, other["id"]).status, "draft")

    def test_similar_clause_requires_explicit_ack(self):
        self.published(self.draft())
        other = self.draft(topic="住宿报销凭证要求", question="住宿报销凭证要求是什么？", answer="需发票")
        review = kv.review(self.s, kv.get_version(self.s, other["id"]))
        self.assertTrue(review["candidates"])
        self.assertFalse(review["blockers"])
        with self.assertRaises(kv.ConflictError):
            self.published(other, acknowledged=[])

    def test_review_token_invalidated_by_other_publish(self):
        d = self.draft()
        r = kv.review(self.s, kv.get_version(self.s, d["id"]))
        self.published(self.draft(topic="设备领用", question="电脑如何领取？"))
        with self.assertRaises(kv.ConflictError):
            self.published(d, review_token=r["review_token"])

    def test_concurrent_drafts_must_rebase(self):
        old = self.published(self.draft())
        a, b = self.new_version(old, answer="600"), self.new_version(old, answer="700")
        self.published(a)
        with self.assertRaises(kv.ConflictError):
            self.published(b)

    def test_stale_editor_cannot_overwrite(self):
        d = self.draft()
        self.new_version(d, answer="600")
        with self.assertRaises(kv.ConflictError):
            self.new_version(d, answer="700")

    def test_repeated_publish_never_duplicates(self):
        d = self.draft()
        p = self.published(d)
        with self.assertRaises(kv.ConflictError):
            self.published(d, revision=d["revision"])
        self.assertEqual(self.s.query(kv.Version).filter_by(status="published").count(), 1)

    def test_publish_failure_preserves_old_generation(self):
        old = self.published(self.draft())
        d = self.new_version(old, answer="600")
        r = kv.review(self.s, kv.get_version(self.s, d["id"]))
        generation = kv.catalog_generation()
        def broken(_):
            raise RuntimeError("embedding unavailable")
        with self.assertRaises(RuntimeError):
            kv.publish(self.s, dict(id=d["id"], revision=d["revision"], review_token=r["review_token"],
                       reviewed=True, review_note="审核通过", acknowledged=[]), "publisher", broken)
        self.s.rollback()
        self.assertEqual(kv.catalog_generation(), generation)
        self.assertEqual(set(self.active()), {old["knowledge_id"]})
        self.assertEqual(kv.get_version(self.s, d["id"]).status, "draft")

    def test_restore_old_content_requires_new_draft_and_review(self):
        old = self.published(self.draft())
        newer = self.published(self.new_version(old, answer="600"))
        restore = self.new_version(old)
        self.assertEqual(restore["base_id"], newer["id"])
        self.assertEqual(restore["answer"], old["answer"])
        self.assertEqual(set(self.active()), {newer["knowledge_id"]})
        restored = self.published(restore)
        self.assertEqual(set(self.active()), {restored["knowledge_id"]})
        self.assertEqual(self.s.query(kv.Version).count(), 3)

    def test_live_version_cannot_be_deleted(self):
        p = self.published(self.draft())
        with self.assertRaises(kv.ValidationError):
            kv.discard(self.s, p, "admin")

    def test_scheduled_version_can_be_withdrawn(self):
        old = self.published(self.draft())
        future = self.today + timedelta(days=2)
        new = self.published(self.new_version(old, effective_from=str(future)))
        kv.discard(self.s, new, "admin")
        self.assertEqual(set(self.active(future)), {old["knowledge_id"]})

    def test_draft_discard_retains_audit(self):
        d = self.draft()
        kv.discard(self.s, d, "admin")
        self.assertEqual(self.s.query(kv.Audit).filter_by(version_id=d["id"]).count(), 2)
        self.assertIsNotNone(self.s.get(db.Knowledge, d["knowledge_id"]))

    def test_scopes_are_filtered_before_retrieval(self):
        sales = self.published(self.draft(scope="销售部"))
        engineering = self.published(self.draft(scope="研发部", answer="600"))
        self.assertEqual(self.active(), {})
        self.assertEqual(set(self.active(scope="销售部")), {sales["knowledge_id"]})
        self.assertEqual(set(self.active(scope="研发部")), {engineering["knowledge_id"]})
        global_draft = self.draft()
        with self.assertRaises(kv.ConflictError):
            self.published(global_draft)

    def test_version_identity_cannot_be_silently_changed(self):
        p = self.published(self.draft())
        with self.assertRaises(kv.ValidationError):
            self.new_version(p, scope="其他部门")

    def test_invalid_dates_and_backdating(self):
        with self.assertRaises(kv.ValidationError):
            self.draft(effective_from="not-a-date")
        self.s.rollback()
        with self.assertRaises(kv.ValidationError):
            self.draft(expires_on=str(self.today - timedelta(days=1)))
        self.s.rollback()
        d = self.draft(effective_from=str(self.today - timedelta(days=1)))
        with self.assertRaises(kv.ConflictError):
            self.published(d)

    def test_migration_idempotent_preserves_legacy_dates(self):
        k = db.Knowledge(domain="hr", question="旧规则", answer="旧内容", source_level="L1",
                         source_name="旧文件", effective_from=self.today-timedelta(days=30),
                         expires_on=self.today-timedelta(days=1), embedding=[1,0])
        self.s.add(k)
        self.s.commit()
        self.assertEqual(kv.migrate(self.engine), 1)
        self.assertEqual(kv.migrate(self.engine), 0)
        self.assertEqual(self.active(), {})
        self.assertIn(k.id, self.active(self.today-timedelta(days=2)))
        self.assertEqual(self.s.get(db.Knowledge, k.id).answer, "旧内容")

    def test_index_refresh_and_citation_version(self):
        old = self.published(self.draft())
        first = knowledge.index()
        new = self.published(self.new_version(old, answer="600"))
        second = knowledge.index()
        self.assertIsNot(first, second)
        with patch("ramp.embeddings.encode", return_value=np.array([[1.,0.]])):
            hits = second.search("住宿报销上限是多少？").hits
        self.assertEqual([h.knowledge_id for h in hits], [new["knowledge_id"]])
        self.assertIn("v2", hits[0].citation)
        self.assertIn("publisher", hits[0].citation)

    def test_historical_retrieval_not_penalized_by_current_expiry(self):
        historical_day = self.today - timedelta(days=2)
        k = db.Knowledge(domain="hr", question="住宿报销上限是多少？", answer="500元",
                         source_level="L1", source_name="旧差旅制度",
                         effective_from=self.today-timedelta(days=30),
                         expires_on=self.today-timedelta(days=1), embedding=[1., 0.])
        self.s.add(k)
        self.s.commit()
        kv.migrate(self.engine)
        idx = knowledge.KnowledgeIndex().load(self.s, as_of=historical_day)
        with patch("ramp.embeddings.encode", return_value=np.array([[1., 0.]])):
            hit = idx.search(k.question).hits[0]
        self.assertFalse(hit.is_stale)
        self.assertNotIn("已过期", hit.citation)
        self.assertAlmostEqual(hit.raw_score, hit.score)
        v = self.s.query(kv.Version).filter_by(knowledge_id=k.id).one()
        self.assertEqual(kv.serialize(self.s, v, historical_day)["state"], "active")
        self.assertEqual(knowledge.KnowledgeIndex().load(self.s).size, 0)

    def test_mentor_sink_is_draft(self):
        k = knowledge.add_knowledge(self.s, domain="hr", question="住宿上限", answer="500", confirmed_by="mentor")
        self.assertEqual(self.active(), {})
        v = self.s.query(kv.Version).filter_by(knowledge_id=k.id).one()
        self.assertEqual(v.status, "draft")

    def test_mentor_review_status_tracks_publication_and_discard(self):
        for publish in (True, False):
            d = self.draft(topic=f"人工审核测试{publish}")
            e = db.Escalation(session_id="isolated", employee_id="test", domain="hr",
                              question="测试", status="review_pending", knowledge_id=d["knowledge_id"])
            self.s.add(e)
            self.s.commit()
            if publish:
                self.published(d)
            else:
                kv.discard(self.s, d, "admin")
            self.s.refresh(e)
            self.assertEqual(e.status, "sunk" if publish else "answered")

    def test_api_requires_admin_and_publish_confirmation(self):
        from ramp.api import app, current
        from ramp.auth import Principal
        app.dependency_overrides[current] = lambda: Principal("newbie", "新人", "newbie")
        self.addCleanup(app.dependency_overrides.clear)
        client = TestClient(app)  # no lifespan; the test DB is already migrated
        self.assertEqual(client.get("/api/admin/knowledge").status_code, 403)
        self.assertEqual(client.post("/api/admin/knowledge/publish", json={}).status_code, 403)
        app.dependency_overrides[current] = lambda: Principal("admin", "管理员", "admin")
        response = client.post("/api/admin/knowledge/save", json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        d = response.json()["item"]
        review = client.get(f"/api/admin/knowledge/review/{d['id']}").json()
        denied = client.post("/api/admin/knowledge/publish", json={
            "id":d["id"], "revision":d["revision"], "review_token":review["review_token"]})
        self.assertEqual(denied.status_code, 400)
        with patch("ramp.embeddings.encode_one", return_value=[1.,0.]):
            published = client.post("/api/admin/knowledge/publish", json={
                "id":d["id"], "revision":d["revision"], "review_token":review["review_token"],
                "reviewed":True, "review_note":"已核对", "acknowledged":[], "confirmed_by":"伪造署名"})
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["item"]["reviewed_by"], "admin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
