-- ops: the agents' own state. wh: the warehouse stand-in (production: Snowflake).
-- billing: the billing stand-in (production: Stripe). HubSpot itself lives behind its API only.

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS wh;
CREATE SCHEMA IF NOT EXISTS billing;

CREATE TABLE IF NOT EXISTS ops.clock (
    id int PRIMARY KEY CHECK (id = 1),
    today date NOT NULL
);

-- Every write an agent makes claims a row here first. The key is built from the business event
-- (company, action, trigger), never from a webhook event id, so two paths reacting to one change collide.
CREATE TABLE IF NOT EXISTS ops.ledger (
    id bigserial PRIMARY KEY,
    action_key text NOT NULL UNIQUE,
    action_type text NOT NULL,
    agent text NOT NULL,
    company_id bigint NOT NULL,
    company_name text NOT NULL,
    owner_id bigint,
    trigger text NOT NULL,
    business_date date NOT NULL,
    autonomy text NOT NULL CHECK (autonomy IN ('act', 'approve')),
    approval_id bigint,
    status text NOT NULL CHECK (status IN ('claimed', 'done', 'failed')),
    source text NOT NULL,
    payload jsonb NOT NULL,
    hubspot_type text,
    hubspot_id bigint,
    error text,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    done_at timestamptz
);
CREATE INDEX IF NOT EXISTS ledger_company ON ops.ledger (company_id, business_date);
CREATE INDEX IF NOT EXISTS ledger_date ON ops.ledger (business_date);

-- Duplicate claims are counted, so the dashboard can show what the ledger stopped.
CREATE TABLE IF NOT EXISTS ops.ledger_collisions (
    id bigserial PRIMARY KEY,
    action_key text NOT NULL,
    business_date date NOT NULL,
    source text NOT NULL,
    at timestamptz NOT NULL DEFAULT now()
);

-- The Slack approval messages. Production posted these with approve, edit and reject buttons.
CREATE TABLE IF NOT EXISTS ops.approvals (
    id bigserial PRIMARY KEY,
    action_key text NOT NULL UNIQUE,
    action_type text NOT NULL,
    agent text NOT NULL,
    company_id bigint NOT NULL,
    company_name text NOT NULL,
    owner_id bigint,
    reason text NOT NULL,
    requested_on date NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'approved', 'edited', 'rejected', 'executed', 'cancelled', 'failed')),
    decision text CHECK (decision IN ('approve', 'edit', 'reject')),
    edited_payload jsonb,
    decided_by text,
    decided_on date,
    review_minutes int,
    outcome text
);
CREATE INDEX IF NOT EXISTS approvals_status ON ops.approvals (status);

-- When an action type crossed the graduation rule, in either direction.
CREATE TABLE IF NOT EXISTS ops.graduations (
    id bigserial PRIMARY KEY,
    action_type text NOT NULL,
    business_date date NOT NULL,
    graduated boolean NOT NULL,
    decisions int NOT NULL,
    edited_or_rejected int NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.shadow_log (
    id bigserial PRIMARY KEY,
    business_date date NOT NULL,
    agent text NOT NULL,
    action_type text NOT NULL,
    company_id bigint NOT NULL,
    action_key text NOT NULL UNIQUE,
    would_need_approval boolean NOT NULL,
    payload jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.blocked_actions (
    id bigserial PRIMARY KEY,
    business_date date NOT NULL,
    action_type text NOT NULL,
    company_id bigint NOT NULL,
    action_key text NOT NULL UNIQUE,
    precondition text NOT NULL,
    detail text NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.webhook_events (
    event_id bigint PRIMARY KEY,
    received_at timestamptz NOT NULL DEFAULT now(),
    subscription_type text NOT NULL,
    object_id bigint NOT NULL,
    property_name text,
    property_value text,
    change_source text,
    source_id text,
    status text NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.webhook_rejections (
    id bigserial PRIMARY KEY,
    received_at timestamptz NOT NULL DEFAULT now(),
    reason text NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.webhook_duplicates (
    event_id bigint NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

-- The queue between the webhook receiver and the agents. Production used SQS.
CREATE TABLE IF NOT EXISTS ops.jobs (
    id bigserial PRIMARY KEY,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'dead')),
    attempts int NOT NULL DEFAULT 0,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_queued ON ops.jobs (id) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS ops.dead_letters (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    ref text NOT NULL,
    payload jsonb NOT NULL,
    error text NOT NULL,
    business_date date NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    replayed_at timestamptz
);

CREATE TABLE IF NOT EXISTS ops.runs (
    thread_id text PRIMARY KEY,
    agent text NOT NULL,
    business_date date NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    stats jsonb NOT NULL DEFAULT '{}',
    error text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

-- One row per company per day the score or tier changed.
CREATE TABLE IF NOT EXISTS ops.health_scores (
    company_id bigint NOT NULL,
    business_date date NOT NULL,
    score int NOT NULL,
    tier text NOT NULL,
    features jsonb NOT NULL,
    points jsonb NOT NULL,
    reasons text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (company_id, business_date)
);

CREATE TABLE IF NOT EXISTS ops.qbr_drafts (
    id bigserial PRIMARY KEY,
    company_id bigint NOT NULL,
    meeting_date date NOT NULL,
    business_date date NOT NULL,
    figures jsonb NOT NULL,
    narrative text NOT NULL,
    attempts int NOT NULL,
    check_status text NOT NULL CHECK (check_status IN ('passed', 'regenerated', 'flagged')),
    mismatches jsonb NOT NULL DEFAULT '[]',
    action_key text NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS ops.time_logs (
    id bigserial PRIMARY KEY,
    owner_id bigint NOT NULL,
    company_id bigint,
    activity text NOT NULL,
    minutes int NOT NULL,
    logged_on date NOT NULL,
    source text NOT NULL CHECK (source IN ('history', 'live'))
);

-- Gmail stand-in. Unique on the action key, so a retried send cannot email a customer twice.
CREATE TABLE IF NOT EXISTS ops.outbox (
    id bigserial PRIMARY KEY,
    action_key text NOT NULL UNIQUE,
    company_id bigint NOT NULL,
    from_owner_id bigint,
    to_email text NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    sent_on date NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.client_stats (
    business_date date PRIMARY KEY,
    requests int NOT NULL DEFAULT 0,
    retries int NOT NULL DEFAULT 0,
    rate_limited int NOT NULL DEFAULT 0,
    server_errors int NOT NULL DEFAULT 0,
    throttle_waits int NOT NULL DEFAULT 0
);

-- Warehouse stand-in, keyed by HubSpot company id as the production sync was.
CREATE TABLE IF NOT EXISTS wh.usage_weekly (
    company_id bigint NOT NULL,
    week_start date NOT NULL,
    seats_licensed int NOT NULL,
    weekly_active_users int NOT NULL,
    appointments_used boolean NOT NULL,
    records_used boolean NOT NULL,
    invoicing_used boolean NOT NULL,
    PRIMARY KEY (company_id, week_start)
);

-- Eighteen months of renewals before launch: the features 90 days out, the outcome, and for the
-- renewals that were at risk, how many days before renewal a CSM first touched them.
CREATE TABLE IF NOT EXISTS wh.renewal_history (
    id bigserial PRIMARY KEY,
    renewal_date date NOT NULL,
    seat_utilization double precision NOT NULL,
    module_adoption double precision NOT NULL,
    ticket_ok double precision NOT NULL,
    invoice_current double precision NOT NULL,
    churned boolean NOT NULL,
    at_risk boolean NOT NULL,
    csm_first_touch_days int
);

CREATE TABLE IF NOT EXISTS billing.subscriptions (
    company_id bigint PRIMARY KEY,
    arr numeric(12, 2) NOT NULL,
    seats int NOT NULL,
    current_period_end date NOT NULL,
    updated_on date NOT NULL
);

CREATE TABLE IF NOT EXISTS billing.invoices (
    id bigserial PRIMARY KEY,
    company_id bigint NOT NULL,
    issued_on date NOT NULL,
    due_on date NOT NULL,
    amount numeric(12, 2) NOT NULL,
    status text NOT NULL CHECK (status IN ('paid', 'open', 'failed')),
    paid_on date
);
CREATE INDEX IF NOT EXISTS invoices_company ON billing.invoices (company_id, issued_on);

CREATE TABLE IF NOT EXISTS billing.disputes (
    id bigserial PRIMARY KEY,
    company_id bigint NOT NULL,
    opened_on date NOT NULL,
    closed_on date,
    amount numeric(12, 2) NOT NULL,
    reason text NOT NULL
);
