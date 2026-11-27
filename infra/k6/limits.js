// The load of the limits layer (docs/limits.md): one test case per origin and endpoint, run by
// k6 through the limits proxy. Every request is logged as one JSON record (console.log, written to
// a file by --log-output), the client's view the limits job judges from:
//   {case, kind, route, origin, object, page, method, path, interaction, status, start, end}
// with start and end in epoch milliseconds.
//
// Cases (the origin of each is a UUID made from the SHA-256 of "limits-case-<case>", so the proxy's
// draw depends only on its seed; make limits restarts the transmitter and the proxy, so every run
// starts from zero counters):
// - traffic-<i>, one per Accounts endpoint: from the next minute boundary, TPM + 50 requests in a
//   row (the request after the TPM must get 429, none before it);
// - operational-<i>-<r>, three per Accounts endpoint: the monthly minimum + 3 successes of a first
//   page, each followed by page 2 (where the endpoint paginates) and by a request that fails, so a
//   transmitter that counts pages or errors refuses before the minimum;
// - consents-<i>-<r>, two per Consents endpoint: 25 requests (no limit applies there).
import http from 'k6/http';
import exec from 'k6/execution';
import { sha256, randomBytes } from 'k6/crypto';
import { sleep } from 'k6';

const BASE = __ENV.BASE || 'http://proxy-limits.ofb.svc';
const ACC = '/open-banking/accounts/v2';
const CON = '/open-banking/consents/v3';
const CLIENT_TOKEN = 'k6-client-credentials';

// route: [TPM, monthly minimum, paginates] (specs/operational/requirements.json).
export const ACCOUNTS_ROUTES = {
  'accounts.list': [1000, 8, true],
  'accounts.detail': [1000, 8, false],
  'accounts.balances': [2500, 420, false],
  'accounts.transactions': [1000, 8, true],
  'accounts.transactions-current': [2500, 240, true],
  'accounts.overdraft-limits': [2500, 420, false],
};
const ROUTES = Object.keys(ACCOUNTS_ROUTES);
const CONSENTS_ROUTES = ['consents.create', 'consents.read', 'consents.delete', 'consents.extend', 'consents.extensions'];
const OPERATIONAL_PER_ROUTE = 3;
const CONSENTS_PER_ROUTE = 2;
const CONSENTS_REQUESTS = 25;
const TRAFFIC_EXTRA = 50;

export const options = {
  scenarios: {
    traffic: { executor: 'per-vu-iterations', exec: 'traffic', vus: ROUTES.length, iterations: 1, maxDuration: '3m' },
    operational: { executor: 'per-vu-iterations', exec: 'operational', vus: ROUTES.length * OPERATIONAL_PER_ROUTE,
                   iterations: 1, maxDuration: '3m' },
    consents: { executor: 'per-vu-iterations', exec: 'consents', vus: CONSENTS_ROUTES.length * CONSENTS_PER_ROUTE,
                iterations: 1, maxDuration: '3m' },
  },
  // Refusals are expected; the job, not k6, judges them.
  thresholds: {},
  summaryTrendStats: ['avg', 'p(95)', 'max'],
};

function uuidv4() {
  const b = new Uint8Array(randomBytes(16));
  b[6] = (b[6] & 15) | 64;
  b[8] = (b[8] & 63) | 128;
  return asUuid([...b].map((x) => x.toString(16).padStart(2, '0')).join(''));
}

function asUuid(h) {
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
}

export function origin(caseId) {
  return asUuid(sha256(`limits-case-${caseId}`, 'hex'));
}

export function setup() {
  return http.get(`${BASE}/mock/seed`).json();
}

function send(c, method, path, page, body, extra) {
  const interaction = uuidv4();
  const headers = Object.assign({ 'x-fapi-interaction-id': interaction, 'x-mock-client-org-id': c.origin,
                                  authorization: `Bearer ${c.token}` }, extra || {});
  if (body) headers['content-type'] = 'application/json';
  const start = Date.now();
  const res = http.request(method, BASE + path, body ? JSON.stringify(body) : null, { headers, tags: { case: c.id } });
  const end = Date.now();
  console.log(JSON.stringify({ case: c.id, kind: c.kind, route: c.route, origin: c.origin, object: c.object, page,
                               method, path, interaction, status: res.status, start, end }));
  return res.status;
}

// The success of a first page, the page after it and a request that fails, per Accounts route.
function accountsRequests(route, account) {
  const base = `${ACC}/accounts/${account}`;
  return {
    'accounts.list': [`${ACC}/accounts`, `${ACC}/accounts?page=2&page-size=25`, `${ACC}/accounts?accountType=NENHUM`],
    'accounts.detail': [base, null, `${ACC}/accounts/UNKNOWN-ACCOUNT`],
    'accounts.balances': [`${base}/balances`, null, `${ACC}/accounts/UNKNOWN-ACCOUNT/balances`],
    'accounts.transactions': [`${base}/transactions`, `${base}/transactions?page=2&page-size=25`,
                              `${base}/transactions?creditDebitIndicator=NENHUM`],
    'accounts.transactions-current': [`${base}/transactions-current`, `${base}/transactions-current?page=2&page-size=25`,
                                      `${base}/transactions-current?creditDebitIndicator=NENHUM`],
    'accounts.overdraft-limits': [`${base}/overdraft-limits`, null, `${ACC}/accounts/UNKNOWN-ACCOUNT/overdraft-limits`],
  }[route];
}

function accountsCase(seed, kind, id, route, n) {
  const customer = seed.customers[n % seed.customers.length];
  const token = customer.consents[0]; // the authorised one
  const account = customer.accounts[0];
  return { id, kind, route, origin: origin(id), token, account,
           object: route === 'accounts.list' ? token : account };
}

export function traffic(seed) {
  const i = exec.scenario.iterationInTest;
  const route = ROUTES[i];
  const c = accountsCase(seed, 'traffic', `traffic-${i}`, route, i);
  const [first] = accountsRequests(route, c.account);
  // Start right after a minute boundary, so the burst stays in one calendar minute.
  sleep((60000 - (Date.now() % 60000) + 200) / 1000);
  for (let r = 0; r < ACCOUNTS_ROUTES[route][0] + TRAFFIC_EXTRA; r++) send(c, 'GET', first, 1);
}

export function operational(seed) {
  const n = exec.scenario.iterationInTest;
  const route = ROUTES[n % ROUTES.length];
  const r = Math.floor(n / ROUTES.length);
  const c = accountsCase(seed, 'operational', `operational-${n % ROUTES.length}-${r}`, route, n);
  const [first, page2, failing] = accountsRequests(route, c.account);
  for (let s = 0; s < ACCOUNTS_ROUTES[route][1] + 3; s++) {
    send(c, 'GET', first, 1);
    if (page2) send(c, 'GET', page2, 2);
    send(c, 'GET', failing, 1);
  }
}

export function consents(seed) {
  const n = exec.scenario.iterationInTest;
  const route = CONSENTS_ROUTES[n % CONSENTS_ROUTES.length];
  const id = `consents-${n % CONSENTS_ROUTES.length}-${Math.floor(n / CONSENTS_ROUTES.length)}`;
  const customer = seed.customers[n % seed.customers.length];
  const consent = customer.consents[0];
  const c = { id, kind: 'consents', route, origin: origin(id), token: CLIENT_TOKEN, object: null };
  const create = { data: { loggedUser: { document: { identification: customer.cpf, rel: 'CPF' } },
                           permissions: ['ACCOUNTS_READ', 'RESOURCES_READ'] } };
  for (let r = 0; r < CONSENTS_REQUESTS; r++) {
    if (route === 'consents.create') send(c, 'POST', `${CON}/consents`, 1, create);
    else if (route === 'consents.read') send(c, 'GET', `${CON}/consents/${consent}`, 1);
    else if (route === 'consents.extensions') send(c, 'GET', `${CON}/consents/${consent}/extensions`, 1);
    // No seeded consent is revoked or renewed: unknown ids (404) and a token that is not the consent (401).
    else if (route === 'consents.delete') send(c, 'DELETE', `${CON}/consents/urn:k6:unknown-${r}`, 1);
    else send(c, 'POST', `${CON}/consents/${consent}/extends`, 1, create);
  }
}
