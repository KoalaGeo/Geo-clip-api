-- Demo data for the docker compose stack: two small tables in EPSG:4326
-- and EPSG:27700 so the CRS handling of the clip process is exercised.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS public.boreholes (
    id serial PRIMARY KEY,
    name text NOT NULL,
    depth_m numeric,
    drilled date,
    geom geometry(Point, 4326)
);
COMMENT ON TABLE public.boreholes IS 'Demo borehole locations';

INSERT INTO public.boreholes (name, depth_m, drilled, geom) VALUES
    ('BH001', 42.5, '2019-04-01', ST_SetSRID(ST_MakePoint(-3.19, 55.95), 4326)),
    ('BH002', 18.0, '2020-06-12', ST_SetSRID(ST_MakePoint(-3.17, 55.96), 4326)),
    ('BH003', 96.2, '2021-09-30', ST_SetSRID(ST_MakePoint(-2.90, 56.10), 4326));

CREATE INDEX IF NOT EXISTS boreholes_geom_idx
    ON public.boreholes USING gist (geom);

CREATE TABLE IF NOT EXISTS public.bedrock (
    id serial PRIMARY KEY,
    unit text NOT NULL,
    lithology text,
    geom geometry(MultiPolygon, 27700)
);
COMMENT ON TABLE public.bedrock IS 'Demo bedrock polygons (British National Grid)';

INSERT INTO public.bedrock (unit, lithology, geom) VALUES
    ('Gullane Formation', 'sandstone',
     ST_Multi(ST_Transform(ST_GeomFromText(
        'POLYGON((-3.25 55.92, -3.10 55.92, -3.10 56.00, -3.25 56.00, -3.25 55.92))',
        4326), 27700))),
    ('Ballagan Formation', 'mudstone',
     ST_Multi(ST_Transform(ST_GeomFromText(
        'POLYGON((-3.00 56.05, -2.80 56.05, -2.80 56.15, -3.00 56.15, -3.00 56.05))',
        4326), 27700)));

CREATE INDEX IF NOT EXISTS bedrock_geom_idx
    ON public.bedrock USING gist (geom);

ANALYZE public.boreholes;
ANALYZE public.bedrock;
