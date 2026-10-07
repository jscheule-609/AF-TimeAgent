"""Batch run timing agent on active deals, write results to stdout.

Re-predicts ALL Active deals on every run (upsert keeps one row per
deal_pk) so estimates tighten as milestones are observed.  Deals whose
status has flipped are excluded — the April-2026 batch predicted 28
deals that had already closed.  deal_outcome is not filtered (8 Active
deals with a NULL outcome went 5 months without a re-predict) and deals
without a target ticker run too, labelled no-ticker: the engine loads
by deal_pk.

    python -m scripts.batch_run            # table + batch40_results.json
    python -m scripts.batch_run --quiet    # cron: summary + failures only
"""
import argparse
import asyncio
import sys
import json
import logging
import warnings
from collections import Counter
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.WARNING)
warnings.filterwarnings('ignore')

from models.deal import DealInput
from pipeline.orchestrator import run_timing_estimation
from db.connection import get_pool, close_pool


async def main(quiet: bool = False):
    pool = await get_pool()
    async with pool.acquire() as c:
        deals = await c.fetch("""
            SELECT d.deal_pk, t.ticker tgt,
                   CAST(d.deal_value_usd AS FLOAT) val,
                   d.closing_guidance_arbjournal aj
            FROM deals d
            -- OQ-N20: v1 parties -> v2 deal_parties + party_entities;
            -- one ticker per deal even with several target parties
            LEFT JOIN LATERAL (
                SELECT pt.ticker
                FROM deal_parties dp_t
                JOIN party_entities pt ON pt.party_id = dp_t.party_id
                WHERE dp_t.deal_pk = d.deal_pk
                  AND dp_t.role_type = 'target'
                  AND pt.ticker IS NOT NULL AND pt.ticker != ''
                ORDER BY pt.party_id
                LIMIT 1
            ) t ON true
            WHERE d.deal_status = 'Active'
            ORDER BY d.deal_value_usd DESC NULLS LAST
        """)

    started = datetime.now(timezone.utc)
    no_ticker = [d['deal_pk'] for d in deals if not d['tgt']]
    results = []
    for i, d in enumerate(deals):
        pk = d['deal_pk']
        tgt = d['tgt'] or 'no-ticker'
        if not quiet:
            sys.stderr.write(f'[{i+1}/{len(deals)}] {tgt} ')
        try:
            r = await run_timing_estimation(DealInput(deal_pk=pk))
            gr = r.guidance_reconciliation
            results.append({
                'tgt': r.target[:22], 'pk': pk,
                'p50': str(r.p50_close_date),
                'p75': str(r.p75_close_date),
                'crit': r.critical_path_jurisdiction[:10],
                'aj': str(d['aj'] or '-')[:12],
                'flag': gr.flag if gr else '-',
                'gap': gr.gap_days if gr else 0,
                'unexpl': gr.unexplained_days if gr else 0,
                'n_adj': len(gr.adjustments) if gr else 0,
                'comps': r.comparable_deals_used,
                'risks': len(r.risk_flags),
            })
            if not quiet:
                sys.stderr.write(f'OK\n')
        except Exception as e:
            results.append({
                'tgt': tgt[:22], 'pk': pk,
                'p50': 'FAIL', 'flag': 'error',
                'gap': 0, 'unexpl': 0, 'aj': str(d['aj'] or '-')[:12],
                'err': str(e)[:200],
            })
            if not quiet:
                sys.stderr.write(f'FAIL\n')

    await close_pool()

    ok = [r for r in results if r['p50'] != 'FAIL']
    fail = [r for r in results if r['p50'] == 'FAIL']
    if quiet:
        secs = (datetime.now(timezone.utc) - started).total_seconds()
        print(f'{started:%Y-%m-%dT%H:%M:%SZ} batch_run: '
              f'{len(ok)}/{len(results)} ok, {len(fail)} failed, '
              f'{len(no_ticker)} no-ticker, {secs:.0f}s')
        if no_ticker:
            print(f'  no-ticker: {no_ticker}')
        for r in fail:
            print(f'  FAIL deal_pk={r["pk"]} {r["tgt"]}: {r["err"]}')
        return
    print(f'=== {len(ok)}/{len(results)} succeeded, {len(fail)} failed ===\n')

    print(f'{"Target":24} {"P50":12} {"AJ Guide":12} {"Flag":14} {"Gap":>6} {"Unexp":>6} {"Adj":>4} {"Crit":10}')
    print('-' * 96)
    for r in ok:
        print(f'{r["tgt"]:24} {r["p50"]:12} {r["aj"]:12} {r["flag"]:14} {r["gap"]:>+6d} {r.get("unexpl",0):>6d} {r.get("n_adj",0):>4d} {r.get("crit",""):10}')

    flags = Counter(r['flag'] for r in ok)
    print(f'\nReconciliation flags:')
    for f, n in flags.most_common():
        print(f'  {f:20} {n:>3} deals')

    gaps = [r['gap'] for r in ok if r['flag'] not in ('no_guidance', 'error', '-')]
    if gaps:
        gaps_s = sorted(gaps)
        print(f'\nGap stats: median={gaps_s[len(gaps_s)//2]:+d}d  mean={sum(gaps)/len(gaps):+.0f}d  range={min(gaps):+d} to {max(gaps):+d}')

    unexpl = [r for r in ok if r.get('unexpl', 0) > 30]
    if unexpl:
        print(f'\nDeals with unexplained gap >30d: {len(unexpl)}')
        for r in sorted(unexpl, key=lambda x: -x['unexpl']):
            print(f'  {r["tgt"]:24} unexpl={r["unexpl"]}d  gap={r["gap"]:+d}d')

    opps = [r for r in ok if r['flag'] == 'opportunity']
    if opps:
        print(f'\nOpportunities (model slower): {len(opps)}')
        for r in opps:
            print(f'  {r["tgt"]:24} model {abs(r["gap"])}d slower')

    with open('batch40_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Re-predict all Active deals')
    parser.add_argument('--quiet', action='store_true',
                        help='cron mode: summary line and failures only, '
                             'no table, no batch40_results.json')
    asyncio.run(main(quiet=parser.parse_args().quiet))
