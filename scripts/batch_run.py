"""Batch run timing agent on active deals, write results to stdout."""
import asyncio
import sys
import json
import logging
import warnings
from collections import Counter
from datetime import date

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.WARNING)
warnings.filterwarnings('ignore')

from models.deal import DealInput
from pipeline.orchestrator import run_timing_estimation
from db.connection import get_pool, close_pool


async def main():
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute('DELETE FROM timing_predictions')
        deals = await c.fetch("""
            SELECT d.deal_pk, pt.ticker tgt,
                   CAST(d.deal_value_usd AS FLOAT) val,
                   d.closing_guidance_arbjournal aj
            FROM deals d
            LEFT JOIN parties pt ON d.deal_pk = pt.deal_pk
                AND pt.role = 'target'
            WHERE d.deal_status = 'Active'
              AND d.date_announced >= '2025-01-01'
              AND pt.ticker IS NOT NULL AND pt.ticker != ''
            ORDER BY d.deal_value_usd DESC NULLS LAST
            LIMIT 40
        """)

    results = []
    for i, d in enumerate(deals):
        pk = d['deal_pk']
        tgt = d['tgt'] or '?'
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
            sys.stderr.write(f'OK\n')
        except Exception as e:
            results.append({
                'tgt': tgt[:22], 'pk': pk,
                'p50': 'FAIL', 'flag': 'error',
                'gap': 0, 'unexpl': 0, 'aj': str(d['aj'] or '-')[:12],
            })
            sys.stderr.write(f'FAIL\n')

    await close_pool()

    ok = [r for r in results if r['p50'] != 'FAIL']
    fail = [r for r in results if r['p50'] == 'FAIL']
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
    asyncio.run(main())
