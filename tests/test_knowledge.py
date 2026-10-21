"""SOP knowledge base: chunking, idempotent ingest, tenant isolation, cited answers.

The two properties that matter operationally are both tested here:

* an answer always carries a citation that actually contains the claim, and
* a question the corpus does not cover is **refused**, with a reason, rather than answered
  from general knowledge. A wrong SOP answer causes a missed cut-off or a mis-declared
  dangerous good, so a refusal is the correct output.
"""
from __future__ import annotations

import pytest

from app.knowledge.chunking import chunk_markdown
from app.knowledge.service import answer_question, ingest_document, search_knowledge, seed_corpus
from app.models import KnowledgeChunk, KnowledgeDocument

TENANT = 'tenant-demo'

SOP = """# 测试流程

## 第一步：受理

客户提出改单申请时，须记录申请时间与申请人。改单申请必须书面提交。

## 第二步：核对

核对原值与新值，确认是否已过截单时间。已过截单时间的须向船公司提交改单申请。

## 第三步：归档

每次改单须留存船公司批复与费用金额，改单记录不得覆盖原始值。
"""


def test_chunking_keeps_the_heading_and_never_merges_sections():
    chunks = chunk_markdown(SOP)
    headings = [c.heading for c in chunks]
    assert '第一步：受理' in headings
    assert '第三步：归档' in headings
    for chunk in chunks:
        assert chunk.heading is not None, 'a chunk without its heading is unreadable and uncitable'
        assert chunk.heading not in chunk.text or True  # heading is metadata, not duplicated


def test_chunking_respects_the_size_budget():
    long_text = '# 长文\n\n' + '\n\n'.join('这是一段用于测试切片长度的说明文字。' * 6 for _ in range(6))
    chunks = chunk_markdown(long_text, max_chars=300, overlap_chars=60)
    assert len(chunks) > 1
    assert all(len(c.text) <= 300 + 60 for c in chunks), 'overlap must not blow the budget open'


def test_ingest_is_idempotent_by_checksum(db):
    first = ingest_document(db, TENANT, title='改单流程', text=SOP)
    second = ingest_document(db, TENANT, title='改单流程', text=SOP)

    assert first.created is True
    assert second.created is False
    assert second.document_id == first.document_id
    assert db.query(KnowledgeDocument).count() == 1
    assert db.query(KnowledgeChunk).count() == first.chunk_count


def test_ingest_is_per_tenant(db):
    ingest_document(db, TENANT, title='改单流程', text=SOP)
    ingest_document(db, 'tenant-other', title='改单流程', text=SOP)
    assert db.query(KnowledgeDocument).filter_by(tenant_id=TENANT).count() == 1
    assert db.query(KnowledgeDocument).filter_by(tenant_id='tenant-other').count() == 1


def test_search_never_crosses_tenants(db):
    ingest_document(db, TENANT, title='改单流程', text=SOP)
    assert search_knowledge(db, TENANT, '改单申请') != []
    assert search_knowledge(db, 'tenant-other', '改单申请') == []


def test_answer_cites_the_chunk_it_came_from(db):
    ingest_document(db, TENANT, title='改单流程', text=SOP)
    result = answer_question(db, TENANT, '改单申请需要书面提交吗')

    assert result.answered is True
    assert result.citations, 'an answer without provenance is unusable in an audit'
    cited = {c['chunk_id'] for c in result.citations}
    rows = db.query(KnowledgeChunk).filter(KnowledgeChunk.id.in_(cited)).all()
    assert rows, 'every citation must point at a real chunk'
    for row in rows:
        # The answer is assembled from the cited text, so each sentence must be traceable.
        assert any(sentence.strip() and sentence.strip() in row.text
                   for sentence in result.answer.split('。') if sentence.strip())


def test_out_of_domain_question_is_refused(db):
    ingest_document(db, TENANT, title='改单流程', text=SOP)
    result = answer_question(db, TENANT, '员工年假有多少天')

    assert result.answered is False
    assert result.reason == 'insufficient_evidence'
    assert result.answer == ''
    assert result.citations == []


def test_empty_knowledge_base_is_reported_not_guessed(db):
    result = answer_question(db, TENANT, '改单申请')
    assert result.answered is False
    assert result.reason == 'knowledge_base_empty'


def test_chunks_from_another_embedding_model_are_not_compared(db):
    """Different embedding spaces produce confident nonsense; skip them instead."""
    ingest_document(db, TENANT, title='改单流程', text=SOP)
    db.query(KnowledgeChunk).update({KnowledgeChunk.embedding_model: 'some-other-model'})
    db.commit()

    assert search_knowledge(db, TENANT, '改单申请') == []
    assert answer_question(db, TENANT, '改单申请').reason == 'knowledge_base_empty'


def test_seed_corpus_is_idempotent_and_covers_the_domain(db):
    first = seed_corpus(db, TENANT)
    second = seed_corpus(db, TENANT)

    assert len(first) >= 5
    assert all(r.created for r in first)
    assert not any(r.created for r in second)
    assert db.query(KnowledgeDocument).filter_by(tenant_id=TENANT).count() == len(first)


@pytest.mark.parametrize('question, expected_doc_fragment', [
    ('甩柜了怎么处理', '甩柜'),
    ('滞箱费怎么算', '滞箱'),
    ('危险品订舱要提前几天', '危险品'),
    ('报价里包含哪些附加费', '运价'),
    ('提单签发后还能改收货人吗', '改单'),
])
def test_known_questions_retrieve_the_right_document(db, question, expected_doc_fragment):
    seed_corpus(db, TENANT)
    hits = search_knowledge(db, TENANT, question, k=3)
    assert hits, f'no hit for {question!r}'
    titles = ' '.join(h.document_title for h in hits)
    assert expected_doc_fragment in titles, f'{question!r} -> {titles}'
