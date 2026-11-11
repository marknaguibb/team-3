"""Business dashboards built exclusively from checked, newly solved run tables."""
from io import BytesIO
import json
from pathlib import Path
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from app.optimizer_adapter import read_run
from app.scenarios import engine_fingerprint
from app.theme import heading

COLORS=["#087f8c","#214e76","#edb458","#79b9ad","#6b7ca7","#d18374"]


def chart(frame,x,y,color=None,kind="bar",title="",barmode="stack"):
    if frame.empty: st.info("No rows for this view."); return
    if kind=="line": fig=px.line(frame,x=x,y=y,color=color,markers=True,title=title,color_discrete_sequence=COLORS)
    else: fig=px.bar(frame,x=x,y=y,color=color,title=title,barmode=barmode,color_discrete_sequence=COLORS)
    fig.update_layout(template="plotly_white",font_family="DM Sans",height=355,
        margin=dict(l=10,r=10,t=50,b=20),legend_title_text="",paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)")
    if isinstance(y,str) and ("fraction" in y or y=="capacity_utilization"): fig.update_yaxes(tickformat=".0%")
    elif "USD" in title: fig.update_yaxes(tickprefix="$",tickformat=",.0f")
    st.plotly_chart(fig,width="stretch")


def table(frame):
    formats={}
    for col in frame.select_dtypes(include="number").columns:
        if "usd" in col.lower(): formats[col]="${:,.2f}"
        elif "fraction" in col or col=="capacity_utilization": formats[col]="{:.2%}"
        else: formats[col]="{:,.2f}"
    st.dataframe(frame.style.format(formats,na_rep="—"),hide_index=True,width="stretch")


def excel_export(tables):
    out=BytesIO()
    with pd.ExcelWriter(out,engine="openpyxl") as writer:
        for name,frame in tables.items(): frame.to_excel(writer,sheet_name=name[:31],index=False)
    return out.getvalue()


def kpis(completion,t):
    demand=float(t["service"].demand_msu.sum())
    shipments=float(t["service"].served_msu.sum())
    production=float(t["production"].msu.sum())
    capacity=float(t["line_capacity"].capacity_msu.sum())
    return dict(cost=completion["costs"]["total_modeled"],service=shipments/demand if demand else None,
        operating_expenses=completion['costs'].get('operating_expenses',completion['costs']['total_modeled']),
        a_materials=completion['costs'].get('a_materials'),c_materials=completion['costs'].get('c_materials'),
        b_materials=completion['costs'].get('b_materials'),total_delivery_cost=completion['costs'].get('total_delivery_cost'),
        fulfilled=shipments,unserved=float(t["service"].unserved_msu.sum()),production=production,
        utilization=production/capacity if capacity else None,events=float((t["material_orders"].quantity>1e-5).sum()),
        peak=float(t["warehouse"].tracked_pallets.max()),holding=completion["costs"]["fp_holding"],
        ide=completion["costs"]["ide"],external=completion["costs"]["external_storage"])


def render(directory=None, *, record=None):
    if record is None:
        completion,t,transition=read_run(directory)
        cfg=json.loads((Path(directory)/'scenario.json').read_text())
        audit=json.loads((Path(directory)/'solution_audit.json').read_text())
    else:
        from app.run_store import unpack_results
        completion,t,transition=unpack_results(record)
        cfg=record['configuration']; audit=record['results']['audit']
        left,right=st.columns([3,1])
        def inspect():
            st.session_state.selected_saved_id=record['run_id']
            st.session_state.screen='saved_parameters'
        right.button('View Configured Parameters',icon=':material/settings:',on_click=inspect,width='stretch')
    heading("VENUS / SOLVED SCENARIO",record['name'] if record else completion["name"],"Actual MILP output · strict Scenario A · 28-day planning months")
    explicit=completion.get('material_policy')=='explicit_abc_v7'
    if not completion["has_solution"]:
        st.error(completion["outcome"]["message"])
        table(pd.DataFrame(completion["outcome"]["stages"])); return
    if 'total_delivery_cost' in completion['costs']:
        from app.components.financial_cards import render as financial_cards
        financial_cards(completion['costs'],explicit_b=explicit)
    if completion["outcome"]["success"]:
        status="All seven optimization stages proven optimal."
    elif completion["monetary_optimal"]:
        status="Service and minimum monetary cost proven. A secondary objective remains unproven; this is a checked feasible plan."
    else:
        status="Feasible incumbent only. Service and/or monetary optimum remain unproven. Inspect the bounds and termination conditions below."
    st.caption("Optimization status · "+status)
    if record is not None:
        st.caption(f"Read-only saved run · {record['created_at']} · {record['scenario_type']} · {record['model_version']} · {record['financial_version']}")
    else:
        st.caption("Run: "+Path(directory).name+" · Newly executed optimizer; no saved baseline substituted.")
    if completion.get('save_error'): st.error('The solve is available, but saving failed: '+completion['save_error'])
    if record is None and completion["engine_fingerprint"]!=engine_fingerprint():
        st.info("This run used an earlier engine/source version. Its immutable results remain available; re-run for the current version.")
    required={'monthly_production_inventory','monthly_cost_breakdown','daily_schedule','productivity',
              'cash','cumulative_indicators','b_material_consumption','fixed_receipts'}
    if not required<=set(t):
        st.info('Some newer report fields were not saved with this historical run. Available original tables are shown without recalculation.')
        for name,frame in t.items():
            with st.expander(name.replace('_',' ').title()): table(frame)
        return
    k=kpis(completion,t)
    from src.shipment_policy import POLICIES, historical_policy
    policy=historical_policy(completion,record.get('model_version','') if record else completion.get('model_version',''))
    st.subheader('International shipment consolidation')
    st.caption(POLICIES[policy][0] if policy else 'Legacy / Unknown Policy')
    a,b=st.columns(2)
    a.metric('International Shipments',completion.get('international_shipments',int(k['events']) if policy=='independent_materials' else 'Unavailable'))
    b.metric('Material Order Lines',completion.get('material_order_lines',int(k['events'])))
    st.caption('Dispatch batches, not physical containers. A feasible count is not a proven minimum; inspect solver bounds.')
    if 'international_shipment_schedule' in t:
        with st.expander('International shipment schedule'):table(t['international_shipment_schedule'])
    complete='total_delivery_cost' in completion['costs']
    if not complete: st.info('Historical operating-cost-only model: additional A/C purchasing and B consumption were excluded. These are not comparable total delivery costs.')
    cards=[("Minimum modeled annual cost" if completion["monetary_optimal"] else "Feasible plan cost",f"${k['cost']:,.2f}"),
        ("Annual customer service",f"{k['service']:.2%}" if k["service"] is not None else "No demand"),
        ("Demand fulfilled",f"{k['fulfilled']:,.1f} MSU"),("Unserved demand",f"{k['unserved']:,.1f} MSU"),
        ("Total production",f"{k['production']:,.1f} MSU"),("Capacity utilization",f"{k['utilization']:.1%}" if k["utilization"] is not None else "N/A"),
        ("Material Order Lines",f"{k['events']:.0f}"),("Peak tracked warehouse",f"{k['peak']:,.1f} pallets"),
        ("External storage",f"${k['external']:,.2f}"),("FP holding expense",f"${k['holding']:,.2f}"),
        ("Product A IDE",f"${k['ide']:,.2f}"),("Solver proof", "7 / 7 stages" if completion["outcome"]["success"] else "Partial proof")]
    if complete: cards=[item for item in cards if item[0] not in ('Minimum modeled annual cost','Feasible plan cost','External storage','FP holding expense','Product A IDE')]
    for offset in range(0,len(cards),4):
        for col,(label,value) in zip(st.columns(4),cards[offset:offset+4]):
            help_text={"Product A IDE":"Inventory Disposition Expense: unused A RM/PM material value plus disposal at transition.",
                "FP holding expense":"Working-capital expense on monthly ending FP value. The inventory asset value is shown separately as cash.",
                "External storage":"3PL external warehouse rental, charged in full booking blocks.",
                "Demand fulfilled":"Customer shipments in MSU: thousands of statistical units.",
                "Total production":"Actual manufacturing in MSU, not theoretical available capacity."}.get(label)
            col.metric(label,value,help=help_text)
    tabs=st.tabs(["Overview","Manufacturing","Materials","Warehouse & Cash","Distribution","Financials","C:D & Proof","Exports"])
    monthly=t["monthly_production_inventory"].query("month != 'ANNUAL'")
    financial=t["monthly_cost_breakdown"]
    with tabs[0]:
        a,b=st.columns([3,2])
        with a: chart(monthly,"month",["total_demand_msu","total_production_msu","shipments_msu"],kind="line",title="Demand, manufacturing and shipments · MSU")
        with b:
            keys=('operating_expenses','a_materials','c_materials','b_materials') if complete else ('labor','fp_holding','transportation','service_failure','ide','external_storage')
            cost=pd.DataFrame([dict(category=name,USD=completion['costs'][name]) for name in keys])
            chart(cost,"category","USD",title="Modeled expense composition")
        table(monthly)
        st.caption("Inventory drawdowns are negative changes, not negative ending stock. Inventory snapshots are not summed as annual stock.")
        with st.expander("Accepted model assumptions"):
            st.write(('B RM/PM and their warehouse space are explicitly tracked; B opening quantities are adopted clarification assumptions. ' if explicit else 'Historical B materials are smoothly replenished and their physical stocks/space are unspecified. ')+
                'Packaging usability is unverified; no age tracking. Month-end warehouse occupancy is checked; intramonth peaks are not established. A finished goods may sell through after qualification. No overtime or unknown headcount limit is invented.')
    with tabs[1]:
        prod=t["production"].groupby(["month","product"],as_index=False).msu.sum()
        chart(prod,"month","msu","product",title="Monthly product production · MSU")
        a,b=st.columns(2)
        with a: chart(t["line_capacity"],"month","production_msu","line",title="Production by line · MSU",barmode="group")
        with b: chart(t["productivity"],"month","capacity_utilization","line",kind="line",title="Available-capacity utilization · fraction")
        start=transition["qualification_start"]
        st.info(f"Qualification starts {start['month']}, planning day {start['planning_day']}. Both lines stop on the three reported shutdown days.")
        table(pd.DataFrame(transition["shutdown_days"]))
        st.write("First C production:",transition["c_first_production"]," · Last A manufacturing:",transition["a_last_production"])
        table(t["daily_schedule"].query("line == 'L2' and changeover > 0.5")[["month","planning_day","configuration","changeover"]])
        with st.expander("Crews, training, upgrades and monthly line capacity"):
            table(t["staffing"]); table(t["productivity"])
    with tabs[2]:
        st.subheader("New international material orders")
        orders=t["material_orders"]
        table(orders[orders.order_event>0.5])
        checks=orders.copy()
        lots=record['effective_inputs']['rules'] if record else {k[5:]:v for k,v in cfg['values'].items() if k.startswith('rule.')}
        checks["configured_lot_size"]=checks.material.map({"RM":lots.get('rm_moq_kg'),"PM":lots.get('pm_moq_ea')})
        checks["lot_residual"]=checks.quantity-checks.integer_lots*checks.configured_lot_size
        with st.expander("Lot compliance and original scheduled receipts"):
            table(checks); table(t["fixed_receipts"])
        balances=t["material_balances"]
        a,b=st.columns(2)
        with a: chart(balances.query("material == 'RM'"),"month","ending","product",kind="line",title="Ending RM · kg")
        with b: chart(balances.query("material == 'PM'"),"month","ending","product",kind="line",title="Ending PM · EA")
        table(balances)
        st.subheader("Product A transition scrap and IDE")
        table(pd.DataFrame([dict(item=key,value=value) for key,value in transition.items() if any(word in key for word in ("scrap","disposal","ide","consumed_before"))]))
        st.caption('B material consumption is informational, not an additional expense. B procurement/inventories are in the tables above.' if explicit else 'Historical smooth-replenishment B consumption expense; no B stock/procurement decisions in this saved version.')
        table(t["b_material_consumption"])
    with tabs[3]:
        a,b=st.columns(2)
        with a: chart(t["inventory"],"month","msu","product",kind="line",title="Ending finished products · MSU")
        with b: chart(t["cash"],"month","ending_fp_value_usd",kind="line",title="Cash tied up in FP · USD balance")
        chart(t["warehouse"],"month",["fp_pallets","rm_pallets","pm_pallets"],title="Tracked warehouse requirements · pallets")
        table(t["warehouse"]); table(t["cash"])
        st.caption('Cash is a balance-sheet measure, not another expense. '+('All ABC material space is tracked.' if explicit else 'Historical B material space is excluded because physical inventory is unspecified.')+' Intramonth peaks remain unverified.')
    with tabs[4]:
        family=t["service"].copy(); family["route"]=family.hub+" / "+family.family
        chart(family,"month","served_msu","route",title="Shipments by hub and family · MSU")
        hubs=family.groupby("hub",as_index=False)[["demand_msu","served_msu","unserved_msu"]].sum()
        hubs["service_fraction"]=hubs.served_msu/hubs.demand_msu.where(hubs.demand_msu>0)
        table(hubs)
        a,b=st.columns(2)
        with a: chart(t["transport"],"month","dedicated_trucks","hub",title="Dedicated trucks")
        with b: chart(t["transport"],"month","spot_trucks","hub",title="Spot-market trucks")
        table(family); table(t["transport"])
        table(financial[["month","dedicated_transport_usd","spot_transport_usd","service_penalties_usd"]])
    with tabs[5]:
        chart(financial.query("month != 'ANNUAL'"),"month","total_modeled_cost_usd",title="Monthly modeled expenses · USD")
        table(financial)
        st.caption("Cost: modeled expenses above. Cash: inventory values in Warehouse & Cash. Service: delivered demand in Distribution. Productivity: capacity utilization and MSU per employed operator in Manufacturing.")
        st.info(('A/B/C new materials are charged when ordered. No separate B consumption expense.' if explicit else 'Historical A/C new materials are charged when ordered; B materials follow production.')+' Full IDE and the exact newly purchased A write-off overlap are disclosed. Inventory asset value is not another expense.')
    with tabs[6]:
        st.subheader("Cumulative C:D diagnostics",help="C:D compares cumulative capacity or available supply with cumulative customer demand. Both definitions are reported; neither adds a new feasibility constraint.")
        indicators=t["cumulative_indicators"]
        fig=px.line(indicators,x="month",y=["capacity_to_demand","supply_to_demand"],markers=True,color_discrete_sequence=COLORS)
        fig.add_hline(y=1,line_dash="dash",line_color="#d18374",annotation_text="1.0 reference")
        fig.update_layout(template="plotly_white",title="Two cumulative C:D diagnostics",yaxis_title="Ratio")
        st.plotly_chart(fig,width="stretch")
        flagged=indicators[indicators.capacity_to_demand_below_one | indicators.supply_to_demand_below_one]
        if not flagged.empty: st.warning("At least one cumulative ratio is below 1.0. These indicators do not impose extra optimization constraints.")
        st.caption("Capacity/demand uses available line-day capacity after downtime. Supply/demand uses opening FP once plus actual production. Neither is the same as customer shipments. Undefined zero-demand ratios remain blank.")
        table(indicators); table(pd.DataFrame(completion["outcome"]["stages"]))
        st.json(audit)
    with tabs[7]:
        for title,name in (("Production plan","production"),("Materials balances","material_balances"),("Procurement orders","material_orders"),
            ("Warehouse inventory","warehouse"),("Finished-product inventory","monthly_inventory_by_product"),
            ("Regional distribution","service"),("Financial breakdown","monthly_cost_breakdown"),("Cumulative indicators","cumulative_indicators")):
            st.download_button(title+" · CSV",t[name].to_csv(index=False),name+".csv","text/csv",key="export:"+name)
        st.download_button("Complete result workbook · Excel",excel_export(t),"venus_results.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.download_button("Scenario configuration · JSON",json.dumps(cfg,indent=2),"venus_scenario.json","application/json",key="result_config")
        if record:
            st.download_button('Complete historical snapshot · JSON',json.dumps(record,indent=2),'saved_run.json','application/json')
        st.download_button("Solver proof metadata · JSON",json.dumps(completion,indent=2),"solve_metadata.json","application/json")


def compare(history):
    heading("VENUS / COMPARE","Scenario comparison","Only actual completed runs appear here. Saved historical baselines are labelled separately.")
    candidates={f"{item['name']} · {key[:8]}":item["directory"] for key,item in history.items() if item.get("has_solution")}
    if len(candidates)<2: st.info("Optimize Original Venus and a modified scenario to compare two freshly executed plans."); return
    a,b=st.columns(2)
    left=a.selectbox("Reference scenario",list(candidates),key="compare_left")
    right=b.selectbox("Comparison scenario",list(candidates),index=1,key="compare_right")
    runs=[read_run(candidates[label]) for label in (left,right)]
    if ('total_delivery_cost' in runs[0][0]['costs'])!=('total_delivery_cost' in runs[1][0]['costs']):
        st.warning('Different financial definitions: historical operating-only cost versus expanded total delivery cost. The difference is not a cost saving.')
    ks=[kpis(c,t) for c,t,_ in runs]
    rows=[dict(metric=key,reference=ks[0][key],comparison=ks[1][key],difference=(ks[1][key]-ks[0][key]) if ks[0][key] is not None and ks[1][key] is not None else None) for key in ks[0]]
    table(pd.DataFrame(rows))
    services=[k["service"] for k in ks]
    different=(services[0] is None)!=(services[1] is None) or (
        services[0] is not None and services[1] is not None and abs(services[0]-services[1])>1e-7)
    if different:
        st.warning("Service levels differ. Cost differences include a service trade-off and are not like-for-like cost savings.")
    if not all(c["monetary_optimal"] for c,_,_ in runs): st.warning("One or both monetary optima are unproven. Compare feasible plans with this limitation.")
    for col,(completion,t,transition) in zip(st.columns(2),runs):
        with col:
            st.subheader(completion["name"])
            st.write("Qualification:",transition["qualification_start"])
            table(t["staffing"].query("hired_crew > 0.5"))
            table(t["material_orders"].query("order_event > 0.5"))
            table(t["production"].groupby("product",as_index=False).msu.sum())
    st.download_button("Export comparison CSV",pd.DataFrame(rows).to_csv(index=False),"scenario_comparison.csv","text/csv")
