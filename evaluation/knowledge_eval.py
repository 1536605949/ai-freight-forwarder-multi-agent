"""Retrieval quality evaluation for the SOP knowledge base.

Two numbers matter, and they fail in opposite directions:

* **hit@3 / MRR** -- did retrieval put the right document in front of the answer?
* **false-accept rate** -- did the system answer a question the corpus does not cover?

The second is the one that causes damage. A wrong SOP answer leads to a missed cut-off or a
mis-declared dangerous good, so the gate is judged on false accepts first and recall second.

Deliberately reports the calibration set size. A recall of 1.0 over 22 questions is weak
evidence, and printing the denominator is what stops anyone treating it as a guarantee.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import Base  # noqa: E402
from app.knowledge.service import answer_question, search_knowledge, seed_corpus  # noqa: E402

TENANT = 'tenant-eval'

# (question, expected document-title fragment)
POSITIVE_CASES: list[tuple[str, str]] = [
    ('甩柜了怎么处理', '甩柜'),
    ('船公司超配导致货物上不了船怎么办', '甩柜'),
    ('滞箱费怎么算', '滞箱'),
    ('免用期一般有几天', '滞箱'),
    ('滞期费和滞箱费有什么区别', '滞箱'),
    ('提单签发后还能改收货人吗', '改单'),
    ('改单要收费用吗', '改单'),
    ('危险品订舱要提前几天', '危险品'),
    ('危险品瞒报有什么后果', '危险品'),
    ('IMDG 隔离要求', '危险品'),
    ('报价里包含哪些附加费', '运价'),
    ('旺季附加费 PSS 是什么', '运价'),
    ('报价有效期怎么确定', '运价'),
    ('订舱要提前多久提交', '订舱'),
    ('截关日和截港日以什么为准', '订舱'),
    ('提箱后多久要还重', '订舱'),
    ('船期延误超过两天怎么办', '异常'),
    ('货损了怎么索赔', '异常'),
    ('什么情况要升级给主管', '异常'),
    ('免用期到期前系统会提醒吗', '滞箱'),
    ('放舱通知要核对哪些内容', '订舱'),
    ('危险品订舱需要哪些资料', '危险品'),
]

NEGATIVE_CASES: list[str] = [
    '员工年假有多少天',
    '公司食堂几点开饭',
    '怎么申请办公用品',
    '股票期权怎么行权',
    'Python 的 GIL 是什么',
    '怎么报销差旅费',
    '会议室怎么预订',
    '离职流程是什么',
    '年终奖什么时候发',
    '如何配置 VPN',
    '打印机坏了找谁',
    '团建预算是多少',
]


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def run() -> dict:
    db = _session()
    seeded = seed_corpus(db, TENANT)
    if not seeded:
        return {'error': 'corpus_empty'}

    positive_rows = []
    hit1 = hit3 = 0
    reciprocal_rank = 0.0
    for question, expected in POSITIVE_CASES:
        hits = search_knowledge(db, TENANT, question, k=3)
        titles = [h.document_title for h in hits]
        rank = next((i + 1 for i, title in enumerate(titles) if expected in title), None)
        executed = True
        if rank == 1:
            hit1 += 1
        if rank is not None:
            hit3 += 1
            reciprocal_rank += 1.0 / rank
        positive_rows.append({'question': question, 'expected': expected,
                              'retrieved': titles, 'rank': rank, 'executed': executed})

    negative_rows = []
    false_accepts = 0
    for question in NEGATIVE_CASES:
        result = answer_question(db, TENANT, question)
        accepted = bool(result.answered)
        if accepted:
            false_accepts += 1
        negative_rows.append({'question': question, 'answered': accepted,
                              'reason': result.reason, 'top_score': result.top_score,
                              'citations': [c['document_title'] for c in result.citations]})

    n_pos = len(POSITIVE_CASES)
    n_neg = len(NEGATIVE_CASES)
    return {
        'corpus_documents': len(seeded),
        'positive_cases': n_pos,
        'negative_cases': n_neg,
        'hit_at_1': round(hit1 / n_pos, 4),
        'hit_at_3': round(hit3 / n_pos, 4),
        'mrr': round(reciprocal_rank / n_pos, 4),
        'false_accepts': false_accepts,
        'false_accept_rate': round(false_accepts / n_neg, 4),
        'positives': positive_rows,
        'negatives': negative_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Evaluate SOP retrieval quality')
    parser.add_argument('--json', type=Path, default=None, help='write full report here')
    parser.add_argument('--min-hit-at-3', type=float, default=0.85)
    args = parser.parse_args()

    report = run()
    if 'error' in report:
        print(f'FAIL {report["error"]}')
        return 2

    print('SOP retrieval evaluation')
    print(f'  corpus documents      : {report["corpus_documents"]}')
    print(f'  positive cases        : {report["positive_cases"]}')
    print(f'  negative cases        : {report["negative_cases"]}')
    print(f'  hit@1                 : {report["hit_at_1"]:.2%}')
    print(f'  hit@3                 : {report["hit_at_3"]:.2%}')
    print(f'  MRR                   : {report["mrr"]:.4f}')
    print(f'  false accepts         : {report["false_accepts"]} '
          f'({report["false_accept_rate"]:.2%})')

    misses = [r for r in report['positives'] if r['rank'] is None]
    if misses:
        print('\n  missed questions:')
        for row in misses:
            print(f'    - {row["question"]}  (expected {row["expected"]}, got {row["retrieved"]})')

    bad = [r for r in report['negatives'] if r['answered']]
    if bad:
        print('\n  answered but should have refused:')
        for row in bad:
            print(f'    - {row["question"]} -> {row["citations"]}')

    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    failed = report['false_accepts'] > 0 or report['hit_at_3'] < args.min_hit_at_3
    print('\nRESULT:', 'FAIL' if failed else 'PASS')
    print('NOTE: this set is small by design; treat the numbers as a regression guard, '
          'not as a quality guarantee.')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
