"""conditions.py — общие предикаты условий lifecycle."""
from typing import Optional
def safe(val,default=0.0): return val if val is not None else default

CONFIRMED_A_SNAPS=5; CONFIRMED_A_OI_MIN=5.0; CONFIRMED_A_CVD_MIN=55.0
CONFIRMED_A_FR_MAX=0.05; CONFIRMED_A_LLS_MAX=40.0
CONFIRMED_A_PC_TOLERANCE=0.5; CONFIRMED_A_OI_TOLERANCE=1.0; CONFIRMED_A_CVD_TOLERANCE=5.0
CONFIRMED_B_OI_MIN=2.0; CONFIRMED_B_CVD_MIN=50.0; CONFIRMED_B_CVD_MOM_MIN=5.0

def _trend(vals):
    clean=[v for v in vals if v is not None]
    if len(clean)<2: return "flat"
    diff=clean[-1]-clean[0]; base=abs(clean[0]) if abs(clean[0])>1 else 10.0
    if diff>base*0.05: return "up"
    if diff<-base*0.05: return "down"
    return "flat"

def check_trends_ok(snaps):
    oi_trend=_trend([s.get("oi_chg24_pct") for s in snaps])
    cvd_trend=_trend([s.get("cvd24") for s in snaps])
    met=oi_trend!="down" and cvd_trend!="down"
    return {"met":met,"oi_trend":oi_trend,"cvd_trend":cvd_trend,"deficit":0 if met else 1}

def check_confirmed_path_a(snaps):
    if len(snaps)<CONFIRMED_A_SNAPS:
        return {"passed":False,"insufficient_data":True,"snaps_have":len(snaps),"snaps_need":CONFIRMED_A_SNAPS,"conditions":{}}
    recent=snaps[-CONFIRMED_A_SNAPS:]; conds={}
    vals_oi=[safe(s.get("oi_chg24_pct")) for s in recent]; met_oi=all(v>CONFIRMED_A_OI_MIN for v in vals_oi); worst_oi=min(vals_oi)
    conds["oi_chg24_gt5"]={"met":met_oi,"value":round(worst_oi,2),"threshold":CONFIRMED_A_OI_MIN,
        "deficit":round(CONFIRMED_A_OI_MIN-worst_oi,2) if not met_oi else 0,
        "met_count":sum(1 for v in vals_oi if v>CONFIRMED_A_OI_MIN),"total":CONFIRMED_A_SNAPS}
    vals_cvd=[safe(s.get("cvd24")) for s in recent]; met_cvd=all(v>CONFIRMED_A_CVD_MIN for v in vals_cvd); worst_cvd=min(vals_cvd)
    conds["cvd_gt55"]={"met":met_cvd,"value":round(worst_cvd,2),"threshold":CONFIRMED_A_CVD_MIN,
        "deficit":round(CONFIRMED_A_CVD_MIN-worst_cvd,2) if not met_cvd else 0,
        "met_count":sum(1 for v in vals_cvd if v>CONFIRMED_A_CVD_MIN),"total":CONFIRMED_A_SNAPS}
    vals_oi4=[safe(s.get("oi_chg4h_pct")) for s in recent]; met_oi4=all(v>0 for v in vals_oi4)
    conds["oi4h_positive"]={"met":met_oi4,"value":round(min(vals_oi4),3),"threshold":0,
        "deficit":round(0-min(vals_oi4),3) if not met_oi4 else 0,
        "met_count":sum(1 for v in vals_oi4 if v>0),"total":CONFIRMED_A_SNAPS}
    vals_fr=[s.get("fr_oiw") for s in recent]; met_fr=all(v is not None and v<CONFIRMED_A_FR_MAX for v in vals_fr)
    worst_fr=max([v for v in vals_fr if v is not None],default=None)
    conds["fr_lt005"]={"met":met_fr,"value":round(worst_fr,5) if worst_fr is not None else None,
        "threshold":CONFIRMED_A_FR_MAX,"deficit":round(worst_fr-CONFIRMED_A_FR_MAX,5) if (worst_fr is not None and not met_fr) else 0,
        "met_count":sum(1 for v in vals_fr if v is not None and v<CONFIRMED_A_FR_MAX),"total":CONFIRMED_A_SNAPS}
    vals_lls=[s.get("lls24") for s in recent]; met_lls=all(v is not None and v<CONFIRMED_A_LLS_MAX for v in vals_lls)
    worst_lls=max([v for v in vals_lls if v is not None],default=None)
    conds["lls_lt40"]={"met":met_lls,"value":round(worst_lls,2) if worst_lls is not None else None,
        "threshold":CONFIRMED_A_LLS_MAX,"deficit":round(worst_lls-CONFIRMED_A_LLS_MAX,2) if (worst_lls is not None and not met_lls) else 0,
        "met_count":sum(1 for v in vals_lls if v is not None and v<CONFIRMED_A_LLS_MAX),"total":CONFIRMED_A_SNAPS}
    met_pc=all(safe(recent[i].get("price_chg24"))>=safe(recent[i-1].get("price_chg24"))-CONFIRMED_A_PC_TOLERANCE for i in range(1,len(recent)))
    pc_net_up=safe(recent[-1].get("price_chg24"))>=safe(recent[0].get("price_chg24"))-CONFIRMED_A_PC_TOLERANCE
    conds["price_not_falling"]={"met":met_pc and pc_net_up,"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    met_oi_step=all(safe(recent[i].get("oi_chg24_pct"))>=safe(recent[i-1].get("oi_chg24_pct"))-CONFIRMED_A_OI_TOLERANCE for i in range(1,len(recent)))
    conds["oi_not_falling"]={"met":met_oi_step,"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    met_cvd_step=all(safe(recent[i].get("cvd24"))>=safe(recent[i-1].get("cvd24"))-CONFIRMED_A_CVD_TOLERANCE for i in range(1,len(recent)))
    conds["cvd_not_falling"]={"met":met_cvd_step,"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    trends=check_trends_ok(recent)
    conds["trends_ok"]={"met":trends["met"],"value":None,"threshold":None,"deficit":trends["deficit"],"met_count":None,"total":None}
    passed=all(c["met"] for c in conds.values())
    return {"passed":passed,"insufficient_data":False,"conditions":conds}

def check_confirmed_path_b(snaps,cvd_momentum):
    if len(snaps)<CONFIRMED_A_SNAPS:
        return {"passed":False,"insufficient_data":True,"snaps_have":len(snaps),"snaps_need":CONFIRMED_A_SNAPS,"conditions":{}}
    recent=snaps[-CONFIRMED_A_SNAPS:]; conds={}
    vals_oi=[safe(s.get("oi_chg24_pct")) for s in recent]; met_oi=all(v>CONFIRMED_B_OI_MIN for v in vals_oi); worst_oi=min(vals_oi)
    conds["oi_chg24_gt2"]={"met":met_oi,"value":round(worst_oi,2),"threshold":CONFIRMED_B_OI_MIN,
        "deficit":round(CONFIRMED_B_OI_MIN-worst_oi,2) if not met_oi else 0,
        "met_count":sum(1 for v in vals_oi if v>CONFIRMED_B_OI_MIN),"total":CONFIRMED_A_SNAPS}
    vals_cvd=[safe(s.get("cvd24")) for s in recent]; met_cvd=all(v>CONFIRMED_B_CVD_MIN for v in vals_cvd); worst_cvd=min(vals_cvd)
    conds["cvd_gt50"]={"met":met_cvd,"value":round(worst_cvd,2),"threshold":CONFIRMED_B_CVD_MIN,
        "deficit":round(CONFIRMED_B_CVD_MIN-worst_cvd,2) if not met_cvd else 0,
        "met_count":sum(1 for v in vals_cvd if v>CONFIRMED_B_CVD_MIN),"total":CONFIRMED_A_SNAPS}
    oi_growing_faster=False
    if len(recent)>=3:
        ov=[safe(s.get("oi_chg24_pct")) for s in recent[-3:]]; d1,d2=ov[1]-ov[0],ov[2]-ov[1]; oi_growing_faster=d2>d1 and d1>0
    conds["oi_growing_faster"]={"met":oi_growing_faster,"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    met_mom=cvd_momentum>CONFIRMED_B_CVD_MOM_MIN
    conds["cvd_momentum_gt5"]={"met":met_mom,"value":round(cvd_momentum,2),"threshold":CONFIRMED_B_CVD_MOM_MIN,
        "deficit":round(CONFIRMED_B_CVD_MOM_MIN-cvd_momentum,2) if not met_mom else 0,"met_count":None,"total":None}
    vals_oi4=[safe(s.get("oi_chg4h_pct")) for s in recent]; met_oi4=all(v>0 for v in vals_oi4)
    conds["oi4h_positive"]={"met":met_oi4,"value":round(min(vals_oi4),3),"threshold":0,
        "deficit":round(0-min(vals_oi4),3) if not met_oi4 else 0,
        "met_count":sum(1 for v in vals_oi4 if v>0),"total":CONFIRMED_A_SNAPS}
    vals_fr=[s.get("fr_oiw") for s in recent]; met_fr=all(v is not None and v<CONFIRMED_A_FR_MAX for v in vals_fr)
    worst_fr=max([v for v in vals_fr if v is not None],default=None)
    conds["fr_lt005"]={"met":met_fr,"value":round(worst_fr,5) if worst_fr is not None else None,
        "threshold":CONFIRMED_A_FR_MAX,"deficit":round(worst_fr-CONFIRMED_A_FR_MAX,5) if (worst_fr is not None and not met_fr) else 0,
        "met_count":sum(1 for v in vals_fr if v is not None and v<CONFIRMED_A_FR_MAX),"total":CONFIRMED_A_SNAPS}
    vals_lls=[s.get("lls24") for s in recent]; met_lls=all(v is not None and v<CONFIRMED_A_LLS_MAX for v in vals_lls)
    worst_lls=max([v for v in vals_lls if v is not None],default=None)
    conds["lls_lt40"]={"met":met_lls,"value":round(worst_lls,2) if worst_lls is not None else None,
        "threshold":CONFIRMED_A_LLS_MAX,"deficit":round(worst_lls-CONFIRMED_A_LLS_MAX,2) if (worst_lls is not None and not met_lls) else 0,
        "met_count":sum(1 for v in vals_lls if v is not None and v<CONFIRMED_A_LLS_MAX),"total":CONFIRMED_A_SNAPS}
    met_pc=all(safe(recent[i].get("price_chg24"))>=safe(recent[i-1].get("price_chg24"))-CONFIRMED_A_PC_TOLERANCE for i in range(1,len(recent)))
    pc_net_up=safe(recent[-1].get("price_chg24"))>=safe(recent[0].get("price_chg24"))-CONFIRMED_A_PC_TOLERANCE
    conds["price_not_falling"]={"met":met_pc and pc_net_up,"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    met_oi_step=all(safe(recent[i].get("oi_chg24_pct"))>=safe(recent[i-1].get("oi_chg24_pct"))-CONFIRMED_A_OI_TOLERANCE for i in range(1,len(recent)))
    conds["oi_not_falling"]={"met":met_oi_step,"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    met_cvd_step=all(safe(recent[i].get("cvd24"))>=safe(recent[i-1].get("cvd24"))-CONFIRMED_A_CVD_TOLERANCE for i in range(1,len(recent)))
    conds["cvd_not_falling"]={"met":met_cvd_step,"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    trends=check_trends_ok(recent)
    conds["trends_ok"]={"met":trends["met"],"value":None,"threshold":None,"deficit":trends["deficit"],"met_count":None,"total":None}
    passed=all(c["met"] for c in conds.values())
    return {"passed":passed,"insufficient_data":False,"conditions":conds}

EARLY_MOVE_SNAPS=3; EARLY_MOVE_CVD_TOLERANCE=3.0; EARLY_MOVE_VOL_FACTOR=0.95
def check_early_move(snaps):
    if len(snaps)<EARLY_MOVE_SNAPS:
        return {"passed":False,"insufficient_data":True,"snaps_have":len(snaps),"snaps_need":EARLY_MOVE_SNAPS,"conditions":{}}
    last3=snaps[-EARLY_MOVE_SNAPS:]; conds={}
    conds["price_up"]={"met":all(safe(last3[i].get("price_chg24"))>safe(last3[i-1].get("price_chg24")) for i in range(1,EARLY_MOVE_SNAPS)),"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    conds["oi_up"]={"met":all(safe(last3[i].get("oi_chg24_pct"))>safe(last3[i-1].get("oi_chg24_pct")) for i in range(1,EARLY_MOVE_SNAPS)),"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    conds["cvd_up"]={"met":all(safe(last3[i].get("cvd24"))>safe(last3[i-1].get("cvd24"))-EARLY_MOVE_CVD_TOLERANCE for i in range(1,EARLY_MOVE_SNAPS)),"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    conds["vol_up"]={"met":all(safe(last3[i].get("volume24"))>safe(last3[i-1].get("volume24"))*EARLY_MOVE_VOL_FACTOR for i in range(1,EARLY_MOVE_SNAPS)),"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    passed=all(c["met"] for c in conds.values())
    return {"passed":passed,"insufficient_data":False,"conditions":conds}

ACCUMULATION_SNAPS=3; ACCUMULATION_CVD_AVG_MIN=50.0; ACCUMULATION_PC_MAX=5.0; ACCUMULATION_FR_MAX=0.03
def check_accumulation(snaps):
    if len(snaps)<ACCUMULATION_SNAPS:
        return {"passed":False,"insufficient_data":True,"snaps_have":len(snaps),"snaps_need":ACCUMULATION_SNAPS,"conditions":{}}
    last3=snaps[-ACCUMULATION_SNAPS:]; conds={}
    conds["oi4h_positive"]={"met":all(safe(s.get("oi_chg4h_pct"))>0 for s in last3),"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    cvd_avg=sum(safe(s.get("cvd24")) for s in last3)/ACCUMULATION_SNAPS; met_cvd=cvd_avg>ACCUMULATION_CVD_AVG_MIN
    conds["cvd_avg_gt50"]={"met":met_cvd,"value":round(cvd_avg,2),"threshold":ACCUMULATION_CVD_AVG_MIN,
        "deficit":round(ACCUMULATION_CVD_AVG_MIN-cvd_avg,2) if not met_cvd else 0,"met_count":None,"total":None}
    pc=safe(last3[-1].get("price_chg24")); met_pc=pc<ACCUMULATION_PC_MAX
    conds["price_chg_lt5"]={"met":met_pc,"value":round(pc,2),"threshold":ACCUMULATION_PC_MAX,
        "deficit":round(pc-ACCUMULATION_PC_MAX,2) if not met_pc else 0,"met_count":None,"total":None}
    conds["fr_lt003"]={"met":all(s.get("fr_oiw") is not None and s.get("fr_oiw")<ACCUMULATION_FR_MAX for s in last3),"value":None,"threshold":None,"deficit":0,"met_count":None,"total":None}
    passed=all(c["met"] for c in conds.values())
    return {"passed":passed,"insufficient_data":False,"conditions":conds}

def closest_miss_for_confirmed(snaps,cvd_momentum):
    result_a=check_confirmed_path_a(snaps); result_b=check_confirmed_path_b(snaps,cvd_momentum)
    best_a=_closest_fail(result_a); best_b=_closest_fail(result_b)
    if best_a is None and best_b is None: return {"path":None,"condition":None,"deficit":0,"note":"оба пути пройдены"}
    if best_a is None: return {"path":"b",**best_b}
    if best_b is None: return {"path":"a",**best_a}
    if best_a["deficit"]<=best_b["deficit"]: return {"path":"a",**best_a}
    return {"path":"b",**best_b}

def _closest_fail(result):
    if result.get("passed"): return None
    if result.get("insufficient_data"): return {"condition":"insufficient_data","deficit":float("inf"),"value":None,"threshold":None}
    fails=[(name,c) for name,c in result["conditions"].items() if not c["met"]]
    if not fails: return None
    numeric_fails=[(n,c) for n,c in fails if c["deficit"] is not None and c["deficit"]!=float("inf")]
    other_fails=[(n,c) for n,c in fails if n not in dict(numeric_fails)]
    if numeric_fails:
        numeric_fails.sort(key=lambda x:x[1]["deficit"]); name,c=numeric_fails[0]
        return {"condition":name,"deficit":c["deficit"],"value":c["value"],"threshold":c["threshold"]}
    if other_fails:
        name,c=other_fails[0]
        return {"condition":name,"deficit":None,"value":c["value"],"threshold":c["threshold"]}
    return None
