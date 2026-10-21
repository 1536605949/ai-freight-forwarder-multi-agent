/* Execute the demo pages' own JavaScript in jsdom against a stubbed API.
 *
 * Why this exists
 * ---------------
 * tests/test_demo_pages.py is static analysis: it proves the page mentions the right node
 * names and branches on `answered`. It cannot prove the page *works* -- the same lesson the
 * backend learned when 259 green tests coexisted with a 500 on the first real request.
 * This script boots the actual HTML, stubs `fetch`, drives the actual buttons, and reads
 * the resulting DOM.
 *
 * Usage:  node scripts/page_smoke.js graph|knowledge
 * Output: one JSON object on stdout. Exit code 0 only when every check passed.
 *
 * Not part of CI: the workflow installs Python only. `tests/test_page_runtime.py` runs this
 * when node + jsdom are available and skips with a reason when they are not, so the check is
 * real locally and honest about its absence elsewhere.
 */
'use strict';

const path = require('path');
const fs = require('fs');

const requested = process.argv.slice(2);
const pages = requested.length ? requested : ['graph', 'knowledge'];
for (const p of pages) {
  if (!['graph', 'knowledge'].includes(p)) {
    console.error(`unknown page: ${p}`);
    process.exit(2);
  }
}

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  console.log(JSON.stringify({ ok: false, skipped: 'jsdom_not_installed', detail: String(e.message) }));
  process.exit(3);
}

const ROOT = path.resolve(__dirname, '..');
const readPage = (name) => fs.readFileSync(path.join(ROOT, 'demo_client', `${name}.html`), 'utf8');

/* ------------------------------------------------------------------ fixtures */

const RUN_HALTED = {
  run_id: 'grun_demo', thread_id: 'thr_demo', inquiry_id: 'inq_demo',
  status: 'waiting_approval', stop_reason: 'human_review_required',
  resume_node: 'quote_send', synthetic_quotes_allowed: true,
  steps: [
    { node: 'understand', status: 'ok', detail: 'inquiry complete', elapsed_ms: 3, outputs: {} },
    { node: 'schedules', status: 'ok', detail: '3 sailings', elapsed_ms: 71, outputs: { schedule_count: 3 } },
    { node: 'rates', status: 'ok', detail: '2 rates', elapsed_ms: 66, outputs: { rate_count: 2 } },
    { node: 'rfq', status: 'ok', detail: 'sent to 5 suppliers', elapsed_ms: 124, outputs: { rfq_id: 'rfq_demo' } },
    { node: 'supplier_quotes', status: 'ok', detail: '2 quotes', elapsed_ms: 18, outputs: {} },
    { node: 'optimize', status: 'ok', detail: 'sell 2450.0', elapsed_ms: 9, outputs: { quote_id: 'cq_demo' } },
    { node: 'hitl_review', status: 'waiting_approval', detail: 'human_review_required',
      elapsed_ms: 2, outputs: { quote_id: 'cq_demo', sell_amount: 2450.0, resume_node: 'quote_send' } }
  ]
};

const RUN_DONE = {
  run_id: 'grun_demo2', thread_id: 'thr_demo2', inquiry_id: 'inq_demo',
  status: 'completed', stop_reason: null, resume_node: null,
  synthetic_quotes_allowed: true,
  steps: RUN_HALTED.steps.concat([
    { node: 'quote_send', status: 'ok', detail: 'quote sent', elapsed_ms: 21, outputs: { quote_id: 'cq_demo' } },
    { node: 'booking', status: 'ok', detail: 'booking confirmed', elapsed_ms: 34, outputs: { booking_id: 'bkg_demo' } },
    { node: 'vessel_tracking', status: 'ok', detail: 'position refreshed', elapsed_ms: 57, outputs: {} }
  ])
};

const ANSWER_OK = {
  query: '甩柜了怎么处理', answered: true,
  answer: '发现甩柜后应第一时间确认下一班船期，并书面通知客户改配方案。',
  reason: null, top_score: 0.83, embedding_model: 'hash-256',
  citations: [{ document_title: '甩柜与舱位管理', heading: '处理流程', score: 0.83,
                text: '发现甩柜后应第一时间确认下一班船期，并书面通知客户改配方案。' }]
};

const ANSWER_REFUSED = {
  query: '公司食堂几点开饭', answered: false, answer: '',
  reason: 'insufficient_evidence', top_score: 0.0, embedding_model: 'hash-256',
  citations: []
};

const TOOL_RESULT = {
  question: '查一下这张询价单的报价状态', answer: '报价单 cq_demo 状态为 waiting_approval。',
  stopped_reason: 'policy_done',
  citations: [{ document_title: '报价与审批', heading: '放行', score: 0.71, text: '报价需人工放行。' }],
  steps: [
    { tool: 'get_quote_status', arguments: { quote_id: 'cq_demo' }, ok: true,
      result: { status: 'waiting_approval' }, error: null },
    { tool: 'write_quote', arguments: {}, ok: false, result: null,
      error: 'tool_not_permitted' }
  ]
};

const METRICS = {
  agents: {
    runs: 4,
    latency: { samples: 4, p50_ms: 120, p95_ms: 480, max_ms: 480 },
    tokens: { prompt: 0, completion: 0, reported_runs: 0, unreported_runs: 4 },
    cost_usd: { total: 0.0, per_reported_run: null, reported_runs: 0 },
    by_status: { ok: 4 }, by_agent: []
  },
  graph: {
    runs: 2,
    by_status: { completed: 1, waiting_approval: 1 },
    stop_reasons: { human_review_required: 1 },
    step_status: {}, timed_runs: 2,
    nodes: [
      { node: 'rates', samples: 2, p50_ms: 60, p95_ms: 66, max_ms: 66 },
      { node: 'rfq', samples: 2, p50_ms: 110, p95_ms: 124, max_ms: 124 }
    ]
  }
};

const INQUIRIES = {
  count: 1,
  items: [{ id: 'inq_demo', origin: 'Shanghai', destination: 'Los Angeles', status: 'ready' }]
};

/* ------------------------------------------------------------------ stubs */

function makeFetch() {
  const calls = [];
  let answerCount = 0;

  const respond = (body, ok = true, status = 200) => ({
    ok, status, json: async () => body
  });

  const fetchStub = async (url, opts) => {
    const method = (opts && opts.method) || 'GET';
    calls.push(`${method} ${url}`);

    if (url.startsWith('/api/v1/inquiries')) return respond(INQUIRIES);
    if (url.startsWith('/api/v1/graph/status/')) return respond({ detail: 'no run' }, false, 404);
    if (url === '/api/v1/graph/run') return respond(RUN_HALTED);
    if (url === '/api/v1/graph/resume') return respond(RUN_DONE);
    if (url.startsWith('/api/v1/quotes/') && url.endsWith('/review')) return respond({ status: 'approved' });
    if (url.startsWith('/api/v1/rfqs/') && url.endsWith('/supplier-quotes')) return respond({ quote: {}, idempotent: false });

    if (url === '/api/v1/knowledge/documents') {
      return respond({ count: 2, chunks: 40, items: [
        { id: 'd1', title: '订舱标准作业流程（SOP）', source: 'bundled', version: '1', chunk_count: 22 },
        { id: 'd2', title: '甩柜与舱位管理', source: 'bundled', version: '1', chunk_count: 18 }
      ] });
    }
    if (url === '/api/v1/knowledge/seed') return respond({ documents: 2, created: 0, results: [] });
    if (url === '/api/v1/knowledge/answer') {
      answerCount += 1;
      const asked = JSON.parse(opts.body || '{}').query || '';
      return respond(asked.includes('食堂') ? ANSWER_REFUSED : ANSWER_OK);
    }
    if (url === '/api/v1/knowledge/search') {
      return respond({ query: 'x', count: 1, hits: [{
        chunk_id: 'c1', document_id: 'd1', document_title: '甩柜与舱位管理', heading: '处理流程',
        ordinal: 0, text: '发现甩柜后应第一时间确认下一班船期。', score: 0.83,
        signals: { bm25: 0.91, cosine: 0.62, coverage: 0.88, heading_hit: 1.0 }
      }] });
    }
    if (url === '/api/v1/agent/ask') return respond(TOOL_RESULT);
    if (url.startsWith('/api/v1/metrics/agent')) return respond(METRICS);
    if (url.startsWith('/api/v1/inquiries/')) return respond({ id: 'inq_demo' });

    return respond({ detail: `unstubbed ${method} ${url}` }, false, 500);
  };

  fetchStub.calls = calls;
  fetchStub.answerCount = () => answerCount;
  return fetchStub;
}

/* ------------------------------------------------------------------ harness */

const checks = [];
function check(name, pass, detail) {
  checks.push({ name, pass: !!pass, detail: detail === undefined ? '' : String(detail) });
}

function text(dom, sel) {
  const el = dom.window.document.querySelector(sel);
  return el ? el.textContent.trim() : '';
}
function count(dom, sel) {
  return dom.window.document.querySelectorAll(sel).length;
}

async function settle(times = 6) {
  for (let i = 0; i < times; i += 1) {
    await new Promise((r) => setTimeout(r, 12));
  }
}

async function runGraphPage(dom, fetchStub) {
  const w = dom.window;
  await settle();
  check('rail renders 11 nodes', count(dom, '#rail .step') === 11, count(dom, '#rail .step'));
  check('inquiry picker is populated', count(dom, '#inq option') === 1);

  await w.runGraph();
  await settle();

  check('trace renders one row per executed node', count(dom, '#trace .srow') === 7,
        count(dom, '#trace .srow'));
  check('latency bars rendered for timed nodes', count(dom, '#lat .brow') === 7,
        count(dom, '#lat .brow'));
  check('run status card shows waiting_approval', text(dom, '#cntRun') === 'waiting_approval',
        text(dom, '#cntRun'));
  check('gate offers an approval action', text(dom, '#gatebox').includes('批准并续跑'));
  check('gate does not offer a pass-through',
        !text(dom, '#gatebox').includes('直通') && !text(dom, '#gatebox').includes('跳过'));

  const railDone = count(dom, '#rail .step.done');
  check('rail marks completed nodes', railDone >= 6, railDone);

  await w.review('approve', 'cq_demo');
  await settle();

  check('approval resumes to completion', text(dom, '#cntRun') === 'completed', text(dom, '#cntRun'));
  // 10 of 11: `clarify` is the other branch and is not on the happy path.
  check('resumed run renders every executed node', count(dom, '#trace .srow') === 10,
        count(dom, '#trace .srow'));
  check('gate cleared after completion', text(dom, '#gatebox') === '');
  check('review endpoint was called',
        fetchStub.calls.some((c) => c.includes('/review')), fetchStub.calls.join(' | '));
  check('resume endpoint was called',
        fetchStub.calls.some((c) => c === 'POST /api/v1/graph/resume'), fetchStub.calls.join(' | '));
}

async function runKnowledgePage(dom, fetchStub) {
  const w = dom.window;
  await settle();
  check('corpus status is read on boot', text(dom, '#cntCorpus').includes('2 篇'),
        text(dom, '#cntCorpus'));
  check('sample chips rendered', count(dom, '#chips .chip') === 4, count(dom, '#chips .chip'));

  await w.ask('甩柜了怎么处理');
  await settle();
  check('answered question renders a citation', count(dom, '#answer .cite') === 1,
        count(dom, '#answer .cite'));
  check('answer text is present', text(dom, '#answer').includes('下一班船期'));

  await w.showHits();
  await settle();
  check('raw hits render the four signals', text(dom, '#hits').includes('coverage'),
        text(dom, '#hits').slice(0, 60));

  await w.ask('公司食堂几点开饭');
  await settle();
  check('refusal is named as a correct result', text(dom, '#answer').includes('这是正确结果'));
  check('refusal clears stale hits', text(dom, '#hits') === '');
  check('refusal does not render a citation', count(dom, '#answer .cite') === 0);
  check('refusal shows the threshold reasoning', text(dom, '#answer').includes('0.38'));

  w.switchTab('tool');
  w.document.getElementById('tq').value = '查一下这张询价单的报价状态';
  await w.askTool();
  await settle();
  check('tool steps render', count(dom, '#toolOut .srow') === 2, count(dom, '#toolOut .srow'));
  check('refused tool call is marked', text(dom, '#toolOut').includes('被拒绝'));
  check('tool answer rendered', text(dom, '#toolOut').includes('waiting_approval'));

  w.switchTab('metric');
  await settle();
  check('metric tiles rendered', count(dom, '#metricOut .m') === 5, count(dom, '#metricOut .m'));
  check('null cost is explained, not printed as 0',
        text(dom, '#metricOut').includes('不是 0'), text(dom, '#metricOut').slice(0, 80));
  check('node p95 bars rendered', count(dom, '#metricOut .brow') === 2,
        count(dom, '#metricOut .brow'));
}

(async () => {
  /* One node process for every page: requiring jsdom costs ~10s on a cold Windows cache,
     and paying it once instead of once per page halves the wall time of this check. */
  const results = [];

  for (const pageName of pages) {
    checks.length = 0;
    const fetchStub = makeFetch();
    const dom = new JSDOM(readPage(pageName), {
      runScripts: 'dangerously',
      url: `http://localhost:8100/demo/${pageName}.html`,
      beforeParse(window) {
        window.fetch = fetchStub;
        window.alert = (m) => { window.__alert = String(m); };
      }
    });

    await new Promise((r) => dom.window.addEventListener('load', r, { once: true }));
    await settle();

    try {
      if (pageName === 'graph') await runGraphPage(dom, fetchStub);
      else await runKnowledgePage(dom, fetchStub);
    } catch (e) {
      check('no uncaught error while driving the page', false,
            e && e.stack ? e.stack.split('\n')[0] : String(e));
    }

    const failed = checks.filter((c) => !c.pass);
    results.push({ page: pageName, ok: failed.length === 0, checks: checks.slice() });
    dom.window.close();
  }

  console.log(JSON.stringify({ ok: results.every((r) => r.ok), results }, null, 2));
  process.exit(results.every((r) => r.ok) ? 0 : 1);
})();
