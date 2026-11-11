"""V7 editor -> model mapping, actual-result dashboard and versioned history."""
import json
from pathlib import Path
import pandas as pd
import pyomo.environ as pyo
import pytest
from streamlit.testing.v1 import AppTest
from src.data_loader import ROOT
from src.model import build_model
from app.scenarios import original_scenario,Scenario
from app.run_store import RunStore,make_record,snapshot_case,FINANCIAL_VERSION
from app.optimizer_adapter import read_run
from app.comparisons import warnings,financial_comparison,changed_inputs


def test_b_material_inputs_prefill_and_persist_across_navigation():
    at=AppTest.from_file(str(ROOT/'app/main.py'),default_timeout=60).run()
    next(b for b in at.button if b.label=='Configure scenario').click().run()
    next(x for x in at.selectbox if x.key=='section_navigation').select_index(2).run()
    assert not at.exception
    def field(key):return next(x for x in at.number_input if x.key.endswith(':'+key))
    assert field('stock.B.RM').value==200000 and field('stock.B.PM').value==750000
    assert field('bom.B.rm_kg_per_msu').value==600 and field('bom.B.pm_ea_per_msu').value==2000
    field('stock.B.RM').set_value(180000.).run();field('stock.B.PM').set_value(700000.).run()
    field('bom.B.rm_kg_per_msu').set_value(650.).run()
    next(x for x in at.selectbox if x.key=='section_navigation').select_index(4).run()
    next(x for x in at.selectbox if x.key=='section_navigation').select_index(2).run()
    assert field('stock.B.RM').value==180000 and field('stock.B.PM').value==700000
    cfg=Scenario.from_json(at.session_state.scenario.to_json());m=build_model(cfg.to_case())
    assert pyo.value(m.initial_material['B','RM'])==180000
    assert pyo.value(m.initial_material['B','PM'])==700000
    assert pyo.value(m.bom_quantity['B','RM'])==650
    assert any('now explicitly tracked' in x.value for x in at.info)


def test_actual_v7_financial_dashboard_and_persisted_snapshot():
    directory=ROOT/'reports/app_runs/v7_original_20261110'
    c,t,_=read_run(directory)
    assert c['has_solution'] and c['monetary_optimal']
    assert c['material_policy']=='explicit_abc_v7'
    record=RunStore().get_run(c['saved_run_id'])
    assert record['financial_version']==FINANCIAL_VERSION
    assert record['configuration']['schema_version']==3
    assert record['configuration']['values']['stock.B.RM']==200000
    assert t['material_orders'].query("product == 'B'").purchase_expense_usd.sum()==pytest.approx(c['costs']['b_materials'])
    assert all(k in t['warehouse'] for k in ('a_rm_pallets','b_rm_pallets','c_rm_pallets','a_pm_pallets','b_pm_pallets','c_pm_pallets','a_fp_pallets','b_fp_pallets','c_fp_pallets'))
    at=AppTest.from_file(str(ROOT/'app/main.py'),default_timeout=60).run()
    at.session_state.selected_saved_id=record['run_id'];at.session_state.screen='saved_results';at.run()
    assert not at.exception
    cards=next(x.value for x in at.markdown if 'data-finance-key=' in x.value)
    assert 'Additional Product B Material Cost' in cards and 'Excluding Material Purchases' in cards
    assert '$9,865,467.50' in cards
    assert not any('secondary objective remains unproven' in x.value for x in at.warning)
    assert any('secondary objective remains unproven' in x.value for x in at.caption)


def test_new_and_old_saved_financial_definitions_never_silently_mix(tmp_path):
    directory=ROOT/'reports/app_runs/v7_original_20261110'
    current=json.loads((directory/'completion.json').read_text())
    new=RunStore().get_run(current['saved_run_id'])
    olddir=ROOT/'reports/app_runs/v6_original_resumed'
    c,t,tr=read_run(olddir);cfg=json.loads((olddir/'scenario.json').read_text())
    old=make_record('Historical smooth B',cfg,snapshot_case(Scenario.from_json(json.dumps(cfg)).to_case()),
        c,t,tr,json.loads((olddir/'solution_audit.json').read_text()))
    assert new['financial_version']!=old['financial_version']
    assert any('V7 charges new B purchase orders' in s for s in warnings(old,new))
    assert financial_comparison(old,new).difference.isna().all()
    store=RunStore(tmp_path/'compat.sqlite3');store.save_run(old);store.save_run(new)
    assert store.get_run(old['run_id'])==old


def test_loading_old_parameter_json_upgrades_new_draft_only():
    from app.state import load_scenario
    old=original_scenario(schema_version=2);old.values['price.B.RM']=6
    state={'revision':0}
    load_scenario(state,old)
    assert state['scenario'].schema_version==3
    assert state['scenario'].values['stock.B.RM']==200000
    assert state['scenario'].values['price.B.RM']==6
    assert old.schema_version==2 and 'stock.B.RM' not in old.values
    assert 'NEW V7 run' in state['migration_notice']


def test_report_reproduction_uses_effective_snapshot_and_is_idempotent(tmp_path):
    import shutil
    from hashlib import sha256
    from src.reporting import generate_reports
    from src.data_loader import load_case
    source=ROOT/'reports/app_runs/v7_original_20261110'
    for path in source.iterdir():
        if path.is_file() and path.suffix in ('.csv','.json','.txt'):shutil.copyfile(path,tmp_path/path.name)
    raw=('production.csv','daily_material.csv','material_orders.csv','solve_status.json')
    before={name:sha256((tmp_path/name).read_bytes()).hexdigest() for name in raw}
    # Today's defaults may differ; captured effective inputs must win.
    data=load_case();data.rules['rm_usd_per_kg']=999
    generate_reports(data,tmp_path)
    first={name:(tmp_path/name).read_bytes() for name in ('summary.txt','ANALYSIS.md','b_material_balances.csv','monthly_cost_breakdown.csv')}
    generate_reports(data,tmp_path)
    assert all((tmp_path/name).read_bytes()==content for name,content in first.items())
    assert all(sha256((tmp_path/name).read_bytes()).hexdigest()==digest for name,digest in before.items())
    opening=pd.read_csv(tmp_path/'opening_warehouse.csv')
    b=opening.query("product == 'B'").set_index('category')
    assert b.loc['RM','pallets']==200 and b.loc['PM','pallets']==150 and b.loc['FP','pallets']==60
    assert opening.pallets.sum()==645
