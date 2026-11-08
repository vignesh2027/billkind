/* Billkind Impact studio: AG Studio dashboards over the signed-in user's own data.

   - Data: /api/insights/data builds the sources on the server, already scoped to this user.
   - Live: the existing server-sent events stream triggers a refetch; changed rows go in through
     AG Studio's data engine as transactions, so only the affected widgets re-query.
   - Assistant: a Billkind agent in AG Studio's Agent Framework (client tool runner). Each round goes through
     Billkind's server (/api/insights/agent), so the model key never reaches the page; the model acts through a
     custom Studio tool, place_widget.
   - Two custom widgets: "Money path" and "Paid ledger".
*/
(() => {
  const root = document.getElementById('studio');
  if (!root) return;
  const scope = root.dataset.scope === 'admin' ? 'admin' : 'sponsor';
  const csrf = document.querySelector('meta[name=csrf]').content;
  const status = document.getElementById('studio-status');
  const say = (msg) => { if (status) status.textContent = msg; };
  const S = window.agStudio;
  if (!S) { say('The dashboard library could not load. Check your connection and reload.'); return; }

  const key = root.dataset.key;
  const lm = S.AgStudioLicenseManager || S.LicenseManager;
  if (key && lm && lm.setLicenseKey) lm.setLicenseKey(key);

  // ---------- Theme: Billkind tokens ----------
  const theme = S.studioTheme.withParams({
    accentColor: '#2450D0',
    foregroundColor: '#0A1A38',
    backgroundColor: '#FFFFFF',
    borderColor: '#D6E0EE',
    fontFamily: 'Manrope, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
  });

  // ---------- Custom widgets ----------
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  class MoneyPathWidget {
    init(params) {
      this.eGui = document.createElement('div');
      this.eGui.className = 'bk-money-path';
      return this.update(params);
    }
    refresh(params) { return this.update(params); }
    async update({ widgetApi, dataMapping }) {
      const f = ['given', 'payments', 'billers'].map((k) => dataMapping[k] && dataMapping[k][0]);
      if (f.some((x) => !x)) { widgetApi.setDisplayState('incompleteDataMapping'); return; }
      widgetApi.setDisplayState('loading', { prominent: !this.loaded });
      const res = await widgetApi.getData({ fields: f }, { queryId: 'money-path' });
      if (res.replaced) return;
      const row = (res.results.rows || [])[0] || {};
      const [given, payments, billers] = f.map((x) => row[x.key]);
      this.loaded = true;
      if (!payments) { widgetApi.setDisplayState('noData'); return; }
      const g = widgetApi.formatFieldValue(f[0], given);
      this.eGui.innerHTML = `
        <div class="bk-node"><span class="bk-k">You</span><strong>${esc(g)}</strong><span class="bk-s">given after refunds</span></div>
        <div class="bk-link" aria-hidden="true"></div>
        <div class="bk-node"><span class="bk-k">PayPal</span><strong>${esc(payments)}</strong><span class="bk-s">payment${payments === 1 ? '' : 's'} captured</span></div>
        <div class="bk-link" aria-hidden="true"></div>
        <div class="bk-node bk-biller"><span class="bk-k">Billers</span><strong>${esc(billers || 0)}</strong><span class="bk-s">paid directly</span></div>
        <div class="bk-never">Never to the person</div>`;
      widgetApi.setDisplayState('displayed');
    }
    getGui() { return this.eGui; }
  }

  class PaidLedgerWidget {
    init(params) {
      this.eGui = document.createElement('div');
      this.eGui.className = 'bk-ledger';
      return this.update(params);
    }
    refresh(params) { return this.update(params); }
    async update({ widgetApi, dataMapping }) {
      const cols = dataMapping.cols || [];
      if (!cols.length) { widgetApi.setDisplayState('incompleteDataMapping'); return; }
      widgetApi.setDisplayState('loading', { prominent: !this.loaded });
      const res = await widgetApi.getData({ fields: cols, limit: { count: 40 } }, { queryId: 'ledger' });
      if (res.replaced) return;
      this.loaded = true;
      const rows = res.results.rows || [];
      if (!rows.length) { widgetApi.setDisplayState('noData'); return; }
      const byName = {};
      cols.forEach((c) => { byName[(c.id || '').split('.').pop()] = c.key; });
      const v = (r, name) => r[byName[name]];
      rows.sort((a, b) => String(v(b, 'paid_on') || '').localeCompare(String(v(a, 'paid_on') || '')));
      this.eGui.innerHTML = rows.map((r) => {
        const paid = v(r, 'status') === 'Paid to biller';
        return `<div class="bk-row${paid ? ' is-paid' : ''}">
          <div class="bk-main"><strong>${esc(v(r, 'category'))}</strong> <span>${esc([v(r, 'city'), v(r, 'country')].filter(Boolean).join(', '))}</span>
            <div class="bk-sub">Paid to ${esc(v(r, 'biller'))} <span class="bk-id">${esc(v(r, 'bill_id'))}</span></div></div>
          ${paid ? '<span class="bk-stamp">PAID</span>' : `<span class="bk-state">${esc(v(r, 'status'))}</span>`}
        </div>`;
      }).join('');
      widgetApi.setDisplayState('displayed');
    }
    getGui() { return this.eGui; }
  }

  const numeric = (aiDescription) => ({ type: 'field', supportedRoles: ['numeric'], requires: { cardinality: 'one' }, required: true, aiDescription });
  const widgets = (widgetConfig) => S.createWidgets({
    additionalTypes: [
      {
        id: 'money-path', label: 'Money path', comp: MoneyPathWidget,
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="4" cy="12" r="2.5"/><circle cx="12" cy="12" r="2.5"/><circle cx="20" cy="12" r="2.5"/><path d="M6.5 12h3M14.5 12h3"/></svg>',
        dataMapping: { given: numeric('Total given after refunds'), payments: numeric('Count of payments'), billers: numeric('Count of distinct billers paid') },
        form: (params) => params.createDefaults({ dataMappingItems: [{ key: 'given', label: 'Given' }, { key: 'payments', label: 'Payments' }, { key: 'billers', label: 'Billers paid' }] }),
        defaultSize: { width: 900, height: 200 }, minSize: { width: 420, height: 150 },
      },
      {
        id: 'paid-ledger', label: 'Paid ledger', comp: PaidLedgerWidget,
        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 3h12v18l-3-2-3 2-3-2-3 2V3z"/><path d="M9 8h6M9 12h6"/></svg>',
        dataMapping: { cols: { type: 'fieldset', supportedRoles: ['category', 'numeric', 'temporal'], requires: { cardinality: 'many' }, required: true, aiDescription: 'Bill fields to list: bill_id, category, city, country, biller, status, paid_on' } },
        form: (params) => params.createDefaults({ dataMappingItems: [{ key: 'cols', label: 'Bill fields' }] }),
        defaultSize: { width: 600, height: 400 }, minSize: { width: 320, height: 200 },
      },
    ],
    menu: [...((widgetConfig && widgetConfig.menu) || []), { label: 'Billkind', widgetIds: ['money-path', 'paid-ledger'] }],
  });

  // ---------- Built-in reports ----------
  const sum = (id) => [{ id, aggregation: 'sum' }];
  const countd = (id) => [{ id, aggregation: 'countd' }];
  const kpi = (value, text) => ({ type: 'value', dataMapping: { value }, format: { style: { label: { enabled: true, text } } } });
  const chart = (type, cat, val, title, desc = true) => ({
    type, dataMapping: { categoryKey: [{ id: cat }], valueKey: val },
    sort: desc ? [{ field: val[0], direction: 'desc' }] : undefined,
    format: { title: { enabled: true, text: title } },
  });
  const grid = (cols, title) => ({ type: 'grid', dataMapping: { cols: cols.map((c) => (typeof c === 'string' ? { id: c } : c)) }, format: { title: { enabled: true, text: title } } });
  const L = (x, y, w, h) => ({ xTrack: x, yTrack: y, xSpan: w, ySpan: h });
  const ledgerCols = ['bills.bill_id', 'bills.category', 'bills.city', 'bills.country', 'bills.biller', 'bills.status', 'bills.paid_on'].map((id) => ({ id }));

  const SPONSOR_STATE = {
    selectedPageId: 'impact',
    panels: { filters: { collapsed: true }, ai: { collapsed: false }, edit: { collapsed: true }, data: { collapsed: true } },
    pages: [
      {
        id: 'impact', name: 'What my money did',
        widgets: {
          given: kpi(sum('payments.net_usd'), 'Given after refunds'),
          bills: kpi(countd('bills.bill_id'), 'Bills supported'),
          countries: kpi(countd('bills.country'), 'Countries'),
          billers: kpi(countd('bills.biller'), 'Billers paid'),
          path: { type: 'money-path', dataMapping: { given: sum('payments.net_usd'), payments: countd('payments.payment_id'), billers: countd('bills.biller') } },
          country: chart('bar-chart-grouped', 'bills.country', sum('payments.net_usd'), 'Given by country'),
          kind: chart('donut-chart', 'bills.category', sum('payments.net_usd'), 'Given by kind of bill', false),
          how: chart('bar-chart-grouped', 'payments.source', sum('payments.net_usd'), 'How I gave'),
          ledger: { type: 'paid-ledger', dataMapping: { cols: ledgerCols }, format: { title: { enabled: true, text: 'Paid ledger' } } },
          detail: grid(['payments.paid_on', 'bills.category', 'bills.country', 'bills.biller', { id: 'payments.net_usd' }, 'payments.payment_status', 'bills.status', 'payments.paypal_order_id', 'payments.paypal_capture_id'], 'Every payment, with its PayPal IDs'),
        },
        widgetLayout: {
          given: L(0, 0, 6, 6), bills: L(6, 0, 6, 6), countries: L(12, 0, 6, 6), billers: L(18, 0, 6, 6),
          path: L(0, 6, 24, 8),
          country: L(0, 14, 12, 16), kind: L(12, 14, 12, 16),
          how: L(0, 30, 9, 16), ledger: L(9, 30, 15, 16),
          detail: L(0, 46, 24, 16),
        },
      },
      {
        id: 'agent', name: 'Where my agent gave',
        widgets: {
          paid: kpi(sum('agent_decisions.paid_usd'), 'Paid by my agent'),
          runs: kpi(countd('agent_decisions.decision_id'), 'Decisions logged'),
          decisions: chart('bar-chart-grouped', 'agent_decisions.decision', countd('agent_decisions.decision_id'), 'Decisions by outcome'),
          rules: chart('bar-chart-grouped', 'agent_decisions.rule', countd('agent_decisions.decision_id'), 'Rules that applied'),
          log: grid(['agent_decisions.run_on', 'agent_decisions.decision', { id: 'agent_decisions.paid_usd' }, 'bills.category', 'bills.country', 'agent_decisions.rule', 'agent_decisions.reason'], 'Decision log'),
        },
        widgetLayout: { paid: L(0, 0, 12, 6), runs: L(12, 0, 12, 6), decisions: L(0, 6, 12, 16), rules: L(12, 6, 12, 16), log: L(0, 22, 24, 18) },
      },
      { id: 'mine', name: 'My own view', widgets: {}, widgetLayout: {} },
    ],
  };

  const ADMIN_STATE = {
    selectedPageId: 'ops',
    panels: { filters: { collapsed: true }, ai: { collapsed: false }, edit: { collapsed: true }, data: { collapsed: true } },
    pages: [
      {
        id: 'ops', name: 'Safety and payouts',
        widgets: {
          bills: kpi(countd('bills.bill_id'), 'Bills submitted'),
          risk: kpi([{ id: 'bills.risk_score', aggregation: 'avg' }], 'Average risk score'),
          payouts: kpi(countd('payouts.payout_item'), 'Payouts to billers'),
          hooks: kpi(countd('webhooks.event_id'), 'PayPal webhooks'),
          status: chart('bar-chart-grouped', 'bills.status', countd('bills.bill_id'), 'Bills by status'),
          signals: chart('bar-chart-grouped', 'fraud_signals.signal', countd('fraud_signals.bill_id'), 'Risk signals raised'),
          outcome: chart('donut-chart', 'payouts.status', countd('payouts.payout_item'), 'Payout outcomes', false),
          verified: chart('donut-chart', 'webhooks.method', countd('webhooks.event_id'), 'Webhook verification', false),
          payoutGrid: grid(['payouts.sent_on', 'payouts.bill_id', 'bills.biller', 'payouts.currency', { id: 'payouts.amount_usd' }, 'payouts.status', 'payouts.minutes_to_final'], 'Payouts to billers'),
        },
        widgetLayout: {
          bills: L(0, 0, 6, 6), risk: L(6, 0, 6, 6), payouts: L(12, 0, 6, 6), hooks: L(18, 0, 6, 6),
          status: L(0, 6, 12, 16), signals: L(12, 6, 12, 16),
          outcome: L(0, 22, 12, 15), verified: L(12, 22, 12, 15),
          payoutGrid: L(0, 37, 24, 16),
        },
      },
      {
        id: 'money', name: 'Money movement',
        widgets: {
          path: { type: 'money-path', dataMapping: { given: sum('payments.net_usd'), payments: countd('payments.payment_id'), billers: countd('bills.biller') } },
          cur: chart('bar-chart-grouped', 'payments.currency', sum('payments.net_usd'), 'Captured by currency (USD equivalent)'),
          source: chart('bar-chart-grouped', 'payments.source', sum('payments.net_usd'), 'How sponsors gave'),
          refunds: chart('bar-chart-grouped', 'payments.payment_status', sum('payments.refunded_usd'), 'Refunded by payment status'),
          ledger: { type: 'paid-ledger', dataMapping: { cols: ledgerCols }, format: { title: { enabled: true, text: 'Bills in motion' } } },
        },
        widgetLayout: { path: L(0, 0, 24, 8), cur: L(0, 8, 8, 16), source: L(8, 8, 8, 16), refunds: L(16, 8, 8, 16), ledger: L(0, 24, 24, 18) },
      },
      { id: 'mine', name: 'Investigate', widgets: {}, widgetLayout: {} },
    ],
  };

  const initialState = scope === 'admin' ? ADMIN_STATE : SPONSOR_STATE;
  const storeKey = `bk-studio-${scope}-v4`;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(storeKey) || 'null'); } catch (e) { saved = null; }

  // ---------- Assistant: a Billkind agent inside AG Studio's Agent Framework ----------
  // A client tool runner: each round goes to Billkind's server (/api/insights/agent), which asks an open model
  // on Groq for the next step. The model answers with a call to `place_widget`, a custom Studio tool defined
  // below that builds the widget configuration and adds it to the dashboard. The model key never reaches the page.
  let current = null;
  const allFields = () => (current ? current.sources.flatMap((src) => (src.fields || []).map((f) => ({ id: `${src.id}.${f.id}`, format: f.format }))) : []);
  const numericFields = () => allFields().filter((f) => ['currencyFormat', 'decimalFormat', 'integerFormat'].includes(f.format)).map((f) => f.id);
  const idFields = () => allFields().filter((f) => /\.(bill_id|payment_id|decision_id|payout_item|event_id)$/.test(f.id)).map((f) => f.id);
  const CHART = { bar: 'bar-chart-grouped', column: 'column-chart-grouped', donut: 'donut-chart' };

  function placeWidget(args) {
    const state = api.getState();
    const pageId = state.pages.some((pg) => pg.id === 'mine') ? 'mine' : state.selectedPageId;
    const page = state.pages.find((pg) => pg.id === pageId);
    const id = `ask-${Date.now().toString(36)}`;
    const agg = args.aggregation || (idFields().includes(args.value) ? 'countd' : 'sum');
    const title = { title: { enabled: true, text: args.title } };
    let widget, size;
    if (args.kind === 'value') {
      widget = { type: 'value', dataMapping: { value: [{ id: args.value, aggregation: agg }] }, format: { style: { label: { enabled: true, text: args.title } } } };
      size = [8, 6];
    } else if (args.kind === 'grid') {
      const cols = (args.columns && args.columns.length ? args.columns : [args.category, args.value].filter(Boolean)).map((c) => ({ id: c }));
      widget = { type: 'grid', dataMapping: { cols }, format: title };
      size = [24, 16];
    } else {
      const value = [{ id: args.value, aggregation: agg }];
      widget = { type: CHART[args.kind] || 'bar-chart-grouped', dataMapping: { categoryKey: [{ id: args.category }], valueKey: value }, format: title };
      if (args.kind !== 'donut') widget.sort = [{ field: value[0], direction: 'desc' }];
      size = [12, 16];
    }
    const layout = page.widgetLayout || {};
    const bottom = Object.values(layout).reduce((m, l) => Math.max(m, (l.yTrack || 0) + (l.ySpan || 0)), 0);
    const rowItems = Object.values(layout).filter((l) => (l.yTrack || 0) + (l.ySpan || 0) === bottom);
    const usedX = rowItems.reduce((m, l) => Math.max(m, (l.xTrack || 0) + (l.xSpan || 0)), 0);
    const fits = rowItems.length && usedX + size[0] <= 24 && size[0] < 24;
    const pos = fits ? { xTrack: usedX, yTrack: rowItems[0].yTrack, xSpan: size[0], ySpan: size[1] } : { xTrack: 0, yTrack: bottom, xSpan: size[0], ySpan: size[1] };
    const pages = state.pages.map((pg) => (pg.id !== pageId ? pg : { ...pg, widgets: { ...(pg.widgets || {}), [id]: widget }, widgetLayout: { ...layout, [id]: pos } }));
    api.setState({ ...state, pages, selectedPageId: pageId });
    return { id, pageId };
  }

  function billkindAgent(studioApi) {
    const tool = studioApi.defineAiTool({
      name: 'place_widget',
      description: 'Add a widget built from this dashboard\'s data to the user\'s own page (My own view, or Investigate for operations).',
      params: (s) => {
        const fieldIds = allFields().map((f) => f.id);
        const nums = numericFields().concat(idFields());
        const fieldEnum = fieldIds.length ? s.enum(fieldIds) : s.string();
        return s.object({
          kind: s.enum(['bar', 'column', 'donut', 'value', 'grid']),
          title: s.string({ description: 'Short title in plain words.' }),
          category: (fieldIds.length ? s.enum(fieldIds) : s.string()).optional(),
          value: (nums.length ? s.enum(nums) : s.string()).optional(),
          aggregation: s.enum(['sum', 'avg', 'count', 'countd']).optional(),
          columns: s.array(fieldEnum).optional(),
        });
      },
      execute: async (args, ctx) => {
        if (args.kind !== 'grid' && !args.value) return ctx.error('A value field is needed for this kind of widget.');
        if (['bar', 'column', 'donut'].includes(args.kind) && !args.category) return ctx.error('A category field is needed for a chart.');
        try {
          const placed = placeWidget(args);
          const pageName = (api.getState().pages.find((pg) => pg.id === placed.pageId) || {}).name || 'current';
          return ctx.success(`Added "${args.title}" to the ${pageName} page.`, placed);
        } catch (err) {
          return ctx.error(`Could not add the widget: ${err && err.message ? err.message : err}`);
        }
      },
    });
    return S.clientToolRunner({
      id: 'billkind-analyst',
      name: 'Billkind analyst',
      description: 'Builds views of your giving from plain-words requests.',
      schema: (s) => s.undefined(),
      tools: () => [tool],
      run: async function* (input, ctx) {
        const tools = ctx.tools().map((t) => {
          const schema = t.schema && t.schema();
          return schema ? { name: t.name, description: t.description, parameters: schema.parameters } : null;
        }).filter(Boolean);
        let data;
        try {
          const r = await fetch(`/api/insights/agent?scope=${scope}`, {
            method: 'POST', signal: ctx.signal,
            headers: { 'Content-Type': 'application/json', 'X-CSRF': csrf },
            body: JSON.stringify({ messages: input.messages, tools }),
          });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          data = await r.json();
        } catch (err) {
          yield { type: 'RUN_ERROR', message: 'The analyst could not be reached. Please try again in a moment.' };
          return;
        }
        for (const ev of data.events || []) yield ev;
      },
    });
  }

  const promptStarters = scope === 'admin'
    ? [
        { label: 'Top risk signals', prompt: 'Show which risk signals are raised most often.' },
        { label: 'Payout speed', prompt: 'Show the average minutes to final status for payouts, by currency.' },
        { label: 'Average risk by kind', prompt: 'Show the average risk score by kind of bill.' },
      ]
    : [
        { label: 'Given by biller', prompt: 'Show how much I gave to each biller as a bar chart.' },
        { label: 'Agent decisions table', prompt: 'Add a table of my agent decisions with the rule that applied and the reason.' },
        { label: 'Days to paid', prompt: 'Show the average days from listing to paid, by kind of bill.' },
      ];

  const props = {
    localeText: { aiMessageInputPlaceholder: 'Ask for a view, for example: given by month' },
    mode: 'edit',
    initialState: saved || initialState,
    widgets,
    theme,
    ai: ({ api: studioApi }) => S.createAiHarness(studioApi, () => ({ agents: [billkindAgent(studioApi)], primary: 'billkind-analyst', promptStarters })),
    onStateUpdated: (event) => { try { localStorage.setItem(storeKey, JSON.stringify(event.state)); } catch (e) { /* storage may be blocked */ } },
  };
  const create = S.createStudioWithAi || S.createStudio;
  const api = create(root, props);

  // ---------- Data and live updates ----------
  const sourceRows = (data, id) => ((data.sources.find((s) => s.id === id) || {}).data || []);
  async function load() {
    const r = await fetch(`/api/insights/data?scope=${scope}`, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }
  function describe(data) {
    const pays = sourceRows(data, 'payments').length;
    if (scope === 'admin') return `${sourceRows(data, 'bills').length} bills, ${pays} payments, ${sourceRows(data, 'payouts').length} payouts. Updates live.`;
    return pays ? `${pays} payment${pays === 1 ? '' : 's'} to ${sourceRows(data, 'bills').length} bill${sourceRows(data, 'bills').length === 1 ? '' : 's'}. Updates live as PayPal confirms payouts.` : 'No payments yet. Pay a bill and it appears here live.';
  }
  async function refresh() {
    let next;
    try { next = await load(); } catch (e) { say('Could not refresh the data. It will retry on the next update.'); return; }
    const engine = api.getDataEngine && api.getDataEngine();
    const tx = [];
    if (current && engine && engine.applyTransaction) {
      for (const src of next.sources) {
        if (!src.primaryKeyField) continue;
        const k = src.primaryKeyField;
        const old = new Map(sourceRows(current, src.id).map((r) => [r[k], JSON.stringify(r)]));
        const add = [], update = [];
        for (const row of src.data) {
          if (!old.has(row[k])) add.push(row); else if (old.get(row[k]) !== JSON.stringify(row)) update.push(row);
        }
        if (add.length || update.length) tx.push({ tableId: src.id, add, update });
      }
    }
    const changedKeyless = current && next.sources.some((s) => !s.primaryKeyField && JSON.stringify(s.data) !== JSON.stringify(sourceRows(current, s.id)));
    try {
      if (tx.length && !changedKeyless) await engine.applyTransaction(tx);
      else if (!current || changedKeyless || tx.length) api.setProperty('data', next);
    } catch (e) {
      api.setProperty('data', next);
    }
    current = next;
    say(describe(next));
  }
  refresh().then(() => {
    const es = new EventSource('/events/stream');
    let t = null;
    const kick = () => { clearTimeout(t); t = setTimeout(refresh, 600); };
    ['funded', 'fully_funded', 'payout_sent', 'paid', 'refund', 'thanks', 'message', 'listed'].forEach((k) => es.addEventListener(k, kick));
  });

  // ---------- Toolbar ----------
  const modeBtn = document.getElementById('studio-mode');
  modeBtn.addEventListener('click', () => {
    const editing = api.getProperty('mode') === 'edit';
    api.setProperty('mode', editing ? 'view' : 'edit');
    modeBtn.setAttribute('aria-pressed', String(editing));
    modeBtn.lastChild.textContent = editing ? ' Back to editing and the assistant' : ' Presentation view';
  });
  document.getElementById('studio-reset').addEventListener('click', () => {
    try { localStorage.removeItem(storeKey); } catch (e) { /* ignore */ }
    api.setState(initialState);
  });
})();
