import json
import pandas as pd
import pyomo.environ as pyo
import pytest
from app.scenarios import original_scenario,Scenario
from app.state import reset,navigate
from src.shipment_policy import POLICIES,add_shipment_policy,shipment_schedule,historical_policy


def orders():
    return pd.DataFrame([dict(product=p,material=k,order_month='2026-10',arrival_month='2027-01',
        quantity=50000 if k=='RM' else 125000,purchase_expense_usd=250000 if k=='RM' else 50000)
        for p in ['B','C'] for k in ['RM','PM']])


@pytest.mark.parametrize('policy,count',[('independent_materials',4),('per_product',2),('all_products',1)])
def test_exact_grouping_and_pallets(policy,count):
    frame=shipment_schedule(orders(),policy)
    assert len(frame)==count and frame.material_order_lines.sum()==4
    assert frame.rm_pallets.sum()==100 and frame.pm_pallets.sum()==50
    assert frame.purchase_cost_usd.sum()==600000
    assert shipment_schedule(orders().assign(quantity=0),policy).empty


def test_month_and_product_separation_and_incompatible_arrivals():
    frame=orders();frame.loc[1,'order_month']='2026-11';frame.loc[1,'arrival_month']='2027-02'
    assert len(shipment_schedule(frame,'per_product'))==3
    frame=orders();frame.loc[frame['product']=='B','product']='A'
    assert len(shipment_schedule(frame,'per_product'))==2
    frame.loc[0,'arrival_month']='2027-02'
    with pytest.raises(ValueError,match='incompatible'):shipment_schedule(frame,'all_products')


def test_all_six_materials_share_one_batch_only_when_selected():
    frame=pd.concat([orders(),orders().query("product == 'B'").assign(product='A')],ignore_index=True)
    assert len(shipment_schedule(frame,'independent_materials'))==6
    assert len(shipment_schedule(frame,'per_product'))==3
    assert len(shipment_schedule(frame,'all_products'))==1


@pytest.mark.parametrize('policy,count',[('independent_materials',4),('per_product',2),('all_products',1)])
def test_real_milp_or_links_no_empty_groups(policy,count):
    from types import SimpleNamespace
    m=pyo.ConcreteModel();m.material_products=pyo.Set(initialize=['A','B','C']);m.materials=pyo.Set(initialize=['RM','PM']);m.months=pyo.Set(initialize=['Oct','Nov'])
    m.order_event=pyo.Var(m.material_products,m.materials,m.months,domain=pyo.Binary)
    for p,k,t in m.order_event:m.order_event[p,k,t].fix(int(p in ['B','C'] and t=='Oct'))
    m.additional_order_events=pyo.Expression(expr=sum(m.order_event.values()))
    add_shipment_policy(m,SimpleNamespace(rules={'shipment_consolidation_policy':policy}))
    # Maximize as well: OR links must prohibit gratuitous empty batches.
    m.obj=pyo.Objective(expr=m.international_shipments,sense=pyo.maximize)
    result=pyo.SolverFactory('appsi_highs').solve(m)
    assert str(result.solver.termination_condition)=='optimal'
    assert pyo.value(m.international_shipments)==count
    assert pyo.value(m.additional_order_events)==4


def test_schema_navigation_reset_and_legacy_compatibility():
    cfg=original_scenario();assert cfg.shipment_consolidation_policy=='independent_materials'
    cfg.shipment_consolidation_policy='per_product'
    restored=Scenario.from_json(cfg.to_json());assert restored==cfg
    assert cfg.to_case().rules['shipment_consolidation_policy']=='per_product'
    assert 'International Shipment Consolidation' in cfg.changes().parameter.values
    state={'scenario':cfg,'revision':0};navigate(state,4)
    assert state['scenario'].shipment_consolidation_policy=='per_product'
    reset(state);assert state['scenario'].shipment_consolidation_policy=='independent_materials'
    old=json.loads(cfg.to_json());old.pop('shipment_consolidation_policy')
    assert Scenario.from_json(json.dumps(old)).shipment_consolidation_policy=='independent_materials'
    cfg.shipment_consolidation_policy='unknown'
    with pytest.raises(ValueError):cfg.validate()
    assert historical_policy({},'Venus V7 / strict Scenario A')=='independent_materials'
    assert historical_policy({},'Unknown') is None


def test_full_small_milp_preserves_service_cost_and_all_material_rules(tmp_path):
    from dataclasses import asdict,fields
    from app.run_store import RunStore,make_record,snapshot_case,unpack_results
    from app.comparisons import changed_inputs,warnings
    from test_v7 import synthetic_case
    from test_v4 import diagnostic
    from src.solve import solve_lexicographic
    from src.results import extract_results,write_results
    from src.validation import audit_solution
    optima=[]
    records=[];store=RunStore(tmp_path/'synthetic.sqlite3')
    for policy,expected in [('independent_materials',2),('per_product',1),('all_products',1)]:
        data,_=synthetic_case()
        data.rules['shipment_consolidation_policy']=policy
        m=diagnostic(data,start=1)
        for d in m.days:m.daily_production['L2','B',d].unfix()
        for k in m.materials:m.material_order['B',k,data.months[0]].unfix()
        outcome=solve_lexicographic(m,time_limit=30)
        assert outcome.success,outcome.message
        result=extract_results(m)
        assert audit_solution(m)['passed']
        optima.append((result.service.unserved_msu.sum(),outcome.minimum_cost))
        assert len(result.international_shipment_schedule)==expected
        assert result.material_orders.order_event.sum()==2
        assert pyo.value(m.international_shipments)==expected
        stage=next(s for s in outcome.stages if s['name']=='international_order_events')
        assert stage['optimum']==expected and stage['termination']=='optimal'
        assert result.material_balances.ending.min()>=-1e-5
        assert result.warehouse.external_blocks.sum()==0
        assert result.material_orders.query('quantity > 0').arrival_month.unique().tolist()==['2027-01']
        write_results(result,outcome,tmp_path/policy)
        meta=json.loads((tmp_path/policy/'solve_status.json').read_text())
        assert meta['international_shipments']==expected and meta['material_order_lines']==2
        assert meta['shipment_consolidation_policy']==policy
        cfg=original_scenario();cfg.shipment_consolidation_policy=policy
        completion=dict(has_solution=True,monetary_optimal=True,costs=result.costs,outcome=asdict(outcome),
            material_policy=result.material_policy,shipment_consolidation_policy=policy,
            international_shipments=expected,material_order_lines=2)
        frames={f.name:getattr(result,f.name) for f in fields(result) if isinstance(getattr(result,f.name),pd.DataFrame)}
        record=make_record('Synthetic '+policy,json.loads(cfg.to_json()),snapshot_case(data),completion,
            frames,result.transition,audit_solution(m))
        store.save_run(record);loaded=store.get_run(record['run_id']);assert loaded==record
        assert len(unpack_results(loaded)[1]['international_shipment_schedule'])==expected
        records.append(record)
    assert optima[0]==pytest.approx(optima[1]) and optima[1]==pytest.approx(optima[2])
    assert changed_inputs(records[0],records[-1]).parameter.str.contains('International Shipment Consolidation').any()
    assert any('physical containers' in s for s in warnings(records[0],records[-1]))


def test_original_and_custom_policy_widgets_and_review(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from src.data_loader import ROOT
    from src.model import build_model
    captured=[]
    class Process:
        def poll(self):return None
    def start(cfg,*args,**kwargs):
        captured.append(cfg)
        return dict(process=Process(),directory=str(tmp_path),name=cfg.name)
    monkeypatch.setattr('app.jobs.start_job',start)
    at=AppTest.from_file(str(ROOT/'app/main.py'),default_timeout=60).run()
    at.session_state.screen='name_original';at.run()
    radio=next(r for r in at.radio if r.label=='International Shipment Consolidation Policy')
    radio.set_value('all_products').run()
    assert at.session_state.original_shipment_policy=='all_products' and not at.exception
    next(x for x in at.text_input if x.label=='Scenario Run Name').set_value('Consolidated original').run()
    next(x for x in at.button if x.label=='Run Optimization').click().run()
    assert captured[0].shipment_consolidation_policy=='all_products'
    assert len(build_model(captured[0].to_case()).shipment_batch)==12
    at.session_state.job=None
    at.session_state.screen='questionnaire';at.session_state.page=2;at.run()
    radio=next(r for r in at.radio if r.label=='International Shipment Consolidation Policy')
    radio.set_value('per_product').run()
    assert at.session_state.scenario.shipment_consolidation_policy=='per_product'
    assert len(build_model(at.session_state.scenario.to_case()).shipment_batch)==36
    at.session_state.page=6;at.run()
    assert any('Consolidate Within Each Product' in x.value for x in at.info)
    assert not any(r.label=='International Shipment Consolidation Policy' for r in at.radio)
    assert not at.exception


def test_dashboard_displays_actual_consolidated_solution(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from src.data_loader import ROOT
    from app.run_store import RunStore,make_record,snapshot_case,MODEL_VERSION,FINANCIAL_VERSION
    from app.scenarios import engine_fingerprint
    directory=ROOT/'reports/v8_all_products'
    status=json.loads((directory/'solve_status.json').read_text())
    monetary=next(s for s in status['stages'] if s['name']=='modeled_cost')
    assert monetary['termination']=='optimal'
    cfg=original_scenario();cfg.shipment_consolidation_policy='all_products'
    completion=dict(name='Actual consolidated Venus',has_solution=True,outcome=status,costs=status['costs'],
        monetary_optimal=monetary['termination']=='optimal',engine_fingerprint=engine_fingerprint(),model_version=MODEL_VERSION,
        material_policy=status['material_policy'],shipment_consolidation_policy='all_products',
        international_shipments=status['international_shipments'],material_order_lines=status['material_order_lines'])
    tables={p.stem:pd.read_csv(p,float_precision='round_trip') for p in directory.glob('*.csv')}
    transition=json.loads((directory/'transition_ide.json').read_text())
    audit=json.loads((directory/'solution_audit.json').read_text())
    record=make_record(completion['name'],json.loads(cfg.to_json()),snapshot_case(cfg.to_case()),
        completion,tables,transition,audit)
    monkeypatch.setenv('CAIRO_RUN_DB',str(tmp_path/'actual.sqlite3'))
    store=RunStore();store.save_run(record)
    at=AppTest.from_file(str(ROOT/'app/main.py'),default_timeout=60).run()
    at.session_state.screen='saved_results';at.session_state.selected_saved_id=record['run_id'];at.run()
    assert not at.exception
    assert next(x for x in at.metric if x.label=='International Shipments').value==str(status['international_shipments'])
    assert next(x for x in at.metric if x.label=='Material Order Lines').value==str(status['material_order_lines'])
    assert any('Consolidate All Products' in x.value for x in at.caption)
    assert any('Optimization status' in x.value for x in at.caption)
    assert not any('secondary objective remains unproven' in x.value for x in at.warning)
    cards=next(x.value for x in at.markdown if 'data-finance-key=' in x.value)
    assert cards.count('data-finance-key=')==5 and f"${status['costs']['total_delivery_cost']:,.2f}" in cards
