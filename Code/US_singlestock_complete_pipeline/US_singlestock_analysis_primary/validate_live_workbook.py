"""Read-only verification against the exact audit directory of a published run."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import pandas as pd
from gspread.utils import rowcol_to_a1
import config
from src.sheets_reader import _client, _control_rows
from src.sheets_writer import (
    STRATEGY_SUMMARY_SHEETS, _strategy_summary_values, _control_panel_values,
    _metric_registry_values, verify_cell_values, validate_generated_topology,
    build_helper_table, helper_sheet_name, _sheet_values,
)
from src.shorts import short_sheet_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit-dir', default='outputs/strategy_live')
    args = parser.parse_args()
    directory = Path(args.audit_dir)
    run = json.loads((directory/'run_summary.json').read_text())
    snapshot = json.loads((directory/'source_snapshot.json').read_text())
    if run['dry_run']:
        raise ValueError('Select the audit directory of a completed live publication')
    config.apply_control_panel(snapshot['control_rows'])
    if config.MODEL_VERSION != run['model_version']:
        raise ValueError('The current code does not match the published model version')
    if json.loads(json.dumps(config.MODEL_CONTROLS)) != run['model_controls']:
        raise ValueError('Replay controls differ from the published run')
    frame = pd.read_parquet(directory/'full_audit.parquet')
    book = _client().open_by_key(run['source_spreadsheet_id'])
    metadata = book.fetch_sheet_metadata()
    industries = sorted(frame.Industry.unique())
    validate_generated_topology(metadata, industries)
    props = {s['properties']['title']: s['properties'] for s in metadata['sheets']}
    payloads = [('Control Panel', _control_panel_values(config.control_panel_rows(), config.strategy_weight_rows())),
                ('Metric Registry', _metric_registry_values(config.STRATEGY_WEIGHTS))]
    for name, title in zip(config.STRATEGY_WEIGHTS, STRATEGY_SUMMARY_SHEETS):
        payloads.append((title, _strategy_summary_values(frame, name, config.STRATEGY_WEIGHTS[name],
            config.STRATEGY_SUMMARY_TOP_N, config.MIN_STRATEGY_SCORE)))
    payloads.append(('Short', short_sheet_values(frame, config.DATA_AS_OF)))
    ranges = ["'" + title.replace("'", "''") + "'!A1:" + rowcol_to_a1(len(values), max(map(len, values)))
              for title, values in payloads]
    actual = book.values_batch_get(ranges, params={'valueRenderOption': 'UNFORMATTED_VALUE'})['valueRanges']
    if len(actual) != len(payloads):
        raise RuntimeError('Missing publication verification ranges')
    for (title, expected), returned in zip(payloads, actual):
        verify_cell_values(title, expected, returned.get('values', []))
        if props[title]['gridProperties'].get('frozenRowCount') != 3:
            raise RuntimeError(f'Header freeze missing: {title}')
    # Numerically compare complete rows from three widely separated helpers.
    helper_evidence = {}
    for industry in [industries[0], industries[len(industries)//2], industries[-1]]:
        expected = build_helper_table(frame[frame.Industry.eq(industry)], config.STRATEGY_WEIGHTS).head(2)
        values = [list(expected.columns)] + _sheet_values(expected.values.tolist())
        title = helper_sheet_name(industry)
        actual = book.worksheet(title).get('A3:'+rowcol_to_a1(len(values)+2, len(expected.columns)), value_render_option='UNFORMATTED_VALUE')
        verify_cell_values(title, values, actual)
        helper_evidence[industry] = expected.Ticker.tolist()
    # Confirm the source has not changed since the published calculation.
    source = book.worksheet(run['source_tab']).get_all_values()
    source_same = source[0] == snapshot['columns'] and source[1:] == snapshot['data']
    result = {'workbook_id': book.id, 'model_version': run['model_version'],
        'verified_full_ranges': ranges, 'helper_sample_tickers': helper_evidence,
        'source_matches_run_snapshot': source_same, 'industries': len(industries),
        'long_count': int(frame['Quantitative Candidate'].eq('QUANT LONG').sum()),
        'short_count': int(frame['Short Selected'].sum()),
        'allocated_cells': sum(x['gridProperties']['rowCount']*x['gridProperties']['columnCount'] for x in props.values())}
    (directory/'publication_verification.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    if not source_same:
        raise RuntimeError('Dataset changed after the published calculation; refresh required')


if __name__ == '__main__':
    main()
