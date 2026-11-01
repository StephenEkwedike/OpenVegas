"""Opt-in same-browser acquisition and retention; never a financial data source."""
from __future__ import annotations

import asyncio
import os
from uuid import NAMESPACE_URL, uuid5

from server.services.dependencies import get_db


def enabled():
    return os.getenv('OPENVEGAS_JOURNEY_ANALYTICS_ENABLED', '0') == '1'


async def record(browser_id, environment, user_id=None):
    async with get_db().transaction() as tx:
        await tx.execute("SET LOCAL statement_timeout = '2000ms'")
        # Retention is intentionally bounded; no historical IDs are retained forever.
        await tx.execute("DELETE FROM product_journeys WHERE first_seen < now()-interval '90 days'")
        await tx.execute('''INSERT INTO product_journeys(browser_id,environment)
                            VALUES ($1,$2) ON CONFLICT DO NOTHING''', browser_id, environment)
        row = await tx.fetchrow('''SELECT * FROM product_journeys
                                  WHERE browser_id=$1 AND environment=$2 FOR UPDATE''', browser_id, environment)
        if row is None:
            return False
        if user_id is not None and row['registered_at'] is None:
            # Only newly created authenticated accounts qualify, not ordinary logins.
            user = await tx.fetchrow('''SELECT created_at FROM auth.users WHERE id=$1
                AND created_at >= $2 AND now() <= $2 + interval '7 days'
                AND NOT EXISTS (SELECT 1 FROM product_journeys WHERE user_id=$1 AND environment=$3)''',
                user_id, row['first_seen'], environment)
            if user:
                await tx.execute('''UPDATE product_journeys SET user_id=$2, registered_at=now()
                                    WHERE browser_id=$1''', browser_id, user_id)
                await _event(tx, browser_id, 'ov_signup_completed')
        await _event(tx, browser_id, 'ov_first_open', first_only=True)
        await _event(tx, browser_id, 'ov_returned')
    return True


async def _event(tx, browser_id, event, first_only=False):
    # A single first-open row, and at most one return per UTC day.
    if event == 'ov_returned':
        await tx.execute('''INSERT INTO product_journey_events(id,browser_id,event)
            VALUES (gen_random_uuid(),$1,$2) ON CONFLICT DO NOTHING''', browser_id, event)
        return
    await tx.execute('''INSERT INTO product_journey_events(id,browser_id,event)
        SELECT $1,$2,$3 WHERE NOT $4 OR NOT EXISTS (
          SELECT 1 FROM product_journey_events WHERE browser_id=$2 AND event=$3)
        ON CONFLICT DO NOTHING''', uuid5(NAMESPACE_URL, f'{browser_id}:{event}'), browser_id, event, first_only)


async def export_pending(limit=10):
    """Bounded outbox drain. Failed deliveries remain pending; IDs deduplicate retries."""
    from server.routes.analytics import INGESTION_HOSTS, PROJECT_TOKEN, _send_activity
    token = os.getenv('PUBLIC_POSTHOG_PROJECT_TOKEN', '')
    host = os.getenv('PUBLIC_POSTHOG_HOST', 'https://us.i.posthog.com')
    if not enabled() or not PROJECT_TOKEN.fullmatch(token) or host not in INGESTION_HOSTS:
        return 0
    sent = 0
    async with get_db().transaction() as tx:
        rows = await tx.fetch('''SELECT e.*, j.environment FROM product_journey_events e
            JOIN product_journeys j USING(browser_id) WHERE e.exported_at IS NULL
            AND j.first_seen >= now()-interval '90 days'
            ORDER BY e.occurred_at LIMIT $1 FOR UPDATE OF e SKIP LOCKED''', min(max(limit, 1), 10))
        for row in rows:
            ok = await _send_activity(host, {'api_key': token, 'distinct_id': str(row['browser_id']),
                'event': row['event'], 'timestamp': row['occurred_at'].isoformat(),
                'properties': {'environment': row['environment'], 'surface': 'web', 'schema_version': 1,
                               '$insert_id': str(row['id']), '$process_person_profile': False,
                               '$geoip_disable': True, 'measurement': 'consented_same_browser'}})
            if not ok:
                break
            await tx.execute('UPDATE product_journey_events SET exported_at=now() WHERE id=$1', row['id'])
            sent += 1
    return sent


SUMMARY_SQL = '''
WITH cohort AS (
 SELECT * FROM product_journeys WHERE environment=$1
 AND first_seen >= now()-interval '90 days'
), measured AS (
 SELECT c.*,
  ((registered_at AT TIME ZONE 'UTC')::date + 2 <= (now() AT TIME ZONE 'UTC')::date) AS mature,
  EXISTS (SELECT 1 FROM product_journey_events e WHERE e.browser_id=c.browser_id
    AND e.event='ov_returned'
    AND e.event_day=(c.registered_at AT TIME ZONE 'UTC')::date + 1) AS retained
 FROM cohort c
)
SELECT count(*) FILTER (WHERE first_seen >= now()-interval '30 days') AS first_opens,
 count(*) FILTER (WHERE registered_at IS NOT NULL AND first_seen >= now()-interval '30 days') AS registrations,
 count(*) FILTER (WHERE registered_at >= now()-interval '30 days' AND mature) AS eligible_d1,
 count(*) FILTER (WHERE registered_at >= now()-interval '30 days' AND mature AND retained) AS returned_d1,
 (SELECT count(*) FROM product_journey_events e JOIN product_journeys j USING(browser_id)
  WHERE j.environment=$1 AND e.exported_at IS NULL) AS pending_exports
FROM measured
'''


def summarize(row):
    values = {key: int(row[key]) for key in ('first_opens','registrations','eligible_d1','returned_d1','pending_exports')}
    return {**values,
            'signup_rate': values['registrations']/values['first_opens'] if values['first_opens'] else None,
            'd1_rate': values['returned_d1']/values['eligible_d1'] if values['eligible_d1'] else None}


async def summary(environment):
    if not enabled():
        return {'status': 'disabled', 'environment': environment}
    try:
        async with asyncio.timeout(3):
            row = await get_db().fetchrow(SUMMARY_SQL, environment)
        return {'status': 'available', 'environment': environment, **summarize(row)}
    except Exception:
        return {'status': 'unavailable', 'environment': environment}
