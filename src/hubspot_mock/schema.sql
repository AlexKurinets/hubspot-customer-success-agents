-- Storage for the HubSpot stand-in. The agents never read this schema; they only see the HTTP API.

CREATE SCHEMA IF NOT EXISTS hs;
CREATE SEQUENCE IF NOT EXISTS hs.object_id_seq START 30100000001;
CREATE SEQUENCE IF NOT EXISTS hs.event_id_seq START 9100000001;

CREATE TABLE IF NOT EXISTS hs.objects (
    object_type text NOT NULL,
    id bigint NOT NULL,
    properties jsonb NOT NULL,
    history jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (object_type, id)
);
CREATE INDEX IF NOT EXISTS objects_props ON hs.objects USING gin (properties jsonb_path_ops);
CREATE INDEX IF NOT EXISTS objects_createdate ON hs.objects (object_type, (properties->>'createdate'));

CREATE TABLE IF NOT EXISTS hs.associations (
    from_type text NOT NULL,
    from_id bigint NOT NULL,
    to_type text NOT NULL,
    to_id bigint NOT NULL,
    type_id int NOT NULL,
    PRIMARY KEY (from_type, from_id, to_type, to_id, type_id)
);
CREATE INDEX IF NOT EXISTS associations_reverse ON hs.associations (to_type, to_id, from_type);

CREATE TABLE IF NOT EXISTS hs.property_groups (
    object_type text NOT NULL,
    name text NOT NULL,
    label text NOT NULL,
    PRIMARY KEY (object_type, name)
);

CREATE TABLE IF NOT EXISTS hs.properties (
    object_type text NOT NULL,
    name text NOT NULL,
    label text NOT NULL,
    type text NOT NULL,
    field_type text NOT NULL,
    group_name text NOT NULL,
    PRIMARY KEY (object_type, name)
);

CREATE TABLE IF NOT EXISTS hs.owners (
    id bigint PRIMARY KEY,
    email text NOT NULL,
    first_name text NOT NULL,
    last_name text NOT NULL,
    team text NOT NULL
);

CREATE TABLE IF NOT EXISTS hs.webhook_outbox (
    id bigserial PRIMARY KEY,
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    attempts int NOT NULL DEFAULT 0,
    last_error text
);
CREATE INDEX IF NOT EXISTS webhook_pending ON hs.webhook_outbox (id) WHERE delivered_at IS NULL;
