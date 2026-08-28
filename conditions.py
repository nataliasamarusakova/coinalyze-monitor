"""conditions.py — общие предикаты условий lifecycle.

CONDITIONS_VERSION = 2 (было 1). Что изменилось и почему:

  И1  k-of-n вместо all() на пороговых условиях.
      Было: одного плохого снимка из пяти достаточно, чтобы обнулить сигнал.
      Замер: дребезг сигнала (True→False→True) падает с 16 до 1 случая
      при неизменной форвардной статистике (r60 +0.25 против +0.26).

  И3  Гейт плотности окна.
      Было: snaps[-5:] берёт 5 последних ЗАПИСЕЙ без проверки времени.
      Замер: 11.1% окон растянуты дольше 30 мин, 2.3% — дольше часа,
      максимум 90 мин. Такое окно давало полноценный сигнал.
      Теперь вердикт «недостаточно данных», а не «условие выполнено».

  И5  Трёхзначная логика: true / false / unknown.
      Было: safe(v, 0.0) — пропуск превращался в 0, то есть для CVD
      в «максимально медвежий», и предикат мгновенно ломал состояние.
      Теперь unknown не считается провалом, но уменьшает число валидных
      наблюдений; если валидных меньше k — «недостаточно данных».

  И2  Итоговое изменение за окно вместо шаговой монотонности.
      ТЕНЕВОЙ РЕЖИМ (USE_NET_CHANGE=False). Замер: три условия шаговой
      монотонности проваливаются с медианным дефицитом РОВНО 0.000 в 39%,
      27% и 23% окон и суммарно 679 раз оказываются единственной причиной
      отказа. Но включение поднимает покрытие 15.6% → 23% и дребезг 1 → 110,
      поэтому включать только вместе с И4 и после теневого прогона.

  И4  Непрерывная сила сигнала для триггера Шмитта (signal_strength).
      ТЕНЕВОЙ РЕЖИМ. Считается всегда, на решения не влияет.

Обратная совместимость: имена и форма возврата всех публичных функций
сохранены — unentered_tracker.py работает без изменений.
"""
from typing import Optional

CONDITIONS_VERSION = 3

# ═══════════════════════════════════════════════════════════════════════════
# РЕЖИМ ВНЕДРЕНИЯ
#
# CONDITIONS_VERSION = 3: боевой переход на k-of-n (4 из 5), гейт плотности
# окна и проверку итогового изменения за окно (Net Change) вместо жесткой
# шаговой монотонности. Устраняет ложные сбросы сигналов от шума округления.
# ═══════════════════════════════════════════════════════════════════════════

# ─── И1: k-of-n ─── БОЕВОЙ РЕЖИМ ─────────────────────────────────────────────
USE_KOFN = True
KOFN_K = 4                      # сколько снимков из CONFIRMED_A_SNAPS должны пройти порог

# ─── И3: гейт плотности окна ─── БОЕВОЙ РЕЖИМ ────────────────────────────────
USE_DENSITY_GATE = True
WINDOW_MAX_SPAN_MIN = 30.0      # весь набор снимков должен укладываться в это время
WINDOW_MAX_GAP_MIN = 10.0       # и не иметь разрыва больше этого между соседними

# ─── И5: трёхзначная логика ─── БОЕВОЙ (на текущих данных инертен) ──────────
MIN_VALID_IN_WINDOW = 4         # меньше валидных значений → «недостаточно данных»

# ─── И2: итоговое изменение вместо шаговой монотонности ─── БОЕВОЙ ───────────
USE_NET_CHANGE = True

# Масштабы для нормировки дефицита. Нужны только чтобы ранжировать промахи
# между условиями с разными единицами (проценты OI, пункты CVD, ставка FR).
# Это эвристика для сортировки, а не статистика.
DEFICIT_SCALE = {
    "oi_chg24": 5.0, "cvd": 55.0, "oi4h_positive": 1.0, "fr_lt005": 0.05,
    "lls_lt40": 40.0, "price_not_falling": 0.5,
    "oi_not_falling": 1.0, "cvd_not_falling": 5.0,
    "cvd_momentum_gt5": 5.0, "window_dense": 30.0,
}

CONFIG = {
    "conditions_version": CONDITIONS_VERSION,
    "use_kofn": USE_KOFN, "kofn_k": KOFN_K,
    "use_density_gate": USE_DENSITY_GATE,
    "window_max_span_min": WINDOW_MAX_SPAN_MIN, "window_max_gap_min": WINDOW_MAX_GAP_MIN,
    "min_valid_in_window": MIN_VALID_IN_WINDOW,
    "use_net_change": USE_NET_CHANGE,
    "live_verdict_equals": "v3 (kofn, density_gate, net_change)",
}

CONFIRMED_A_SNAPS=5; CONFIRMED_A_OI_MIN=5.0; CONFIRMED_A_CVD_MIN=55.0
CONFIRMED_A_FR_MAX=0.05; CONFIRMED_A_LLS_MAX=40.0
CONFIRMED_A_PC_TOLERANCE=0.5; CONFIRMED_A_OI_TOLERANCE=1.0; CONFIRMED_A_CVD_TOLERANCE=5.0
CONFIRMED_B_OI_MIN=2.0; CONFIRMED_B_CVD_MIN=50.0; CONFIRMED_B_CVD_MOM_MIN=5.0

def _k_for(n):
    """Сколько снимков должно выполнить условие. USE_KOFN=False → все."""
    return min(KOFN_K, n) if USE_KOFN else n

# ─── И3: качество окна ──────────────────────────────────────────────────────
def window_quality(snaps):
    """Плотность окна по времени. Без этого 'пять снимков подряд' может
    означать интервал до 90 минут с дырами внутри."""
    ts=sorted(s.get("ts") for s in snaps if s.get("ts") is not None)
    if len(ts)<2:
        return {"span_min":0.0,"max_gap_min":0.0,"dense":True,"ts_count":len(ts)}
    span=(ts[-1]-ts[0])/60.0
    max_gap=max((ts[i]-ts[i-1])/60.0 for i in range(1,len(ts)))
    dense=(span<=WINDOW_MAX_SPAN_MIN and max_gap<=WINDOW_MAX_GAP_MIN)
    return {"span_min":round(span,1),"max_gap_min":round(max_gap,1),
            "dense":dense,"ts_count":len(ts)}

# ─── И5 + И1: пороговое условие на окне ─────────────────────────────────────
def _norm(deficit, scale_key):
    """Нормировка дефицита для ранжирования между разнородными условиями."""
    if deficit is None: return None
    s=DEFICIT_SCALE.get(scale_key)
    if not s: return None
    return round(abs(deficit)/s,4)

def _threshold_cond(vals, pred, threshold, k, deficit_fn=None, worst_fn=None, scale_key=None):
    """Трёхзначное пороговое условие с k-of-n.
    vals может содержать None — это 'unknown', не провал."""
    valid=[v for v in vals if v is not None]
    unknown=len(vals)-len(valid)
    ok=sum(1 for v in valid if pred(v))
    enough_data=len(valid)>=min(MIN_VALID_IN_WINDOW,len(vals))
    met=enough_data and ok>=min(k,len(valid))
    worst=worst_fn(valid) if (worst_fn and valid) else None
    # [FIX] deficit=None, если его нельзя посчитать — раньше в таком случае
    # ставился 0, и условие выглядело как «не хватило нуля».
    deficit=None
    if not met and worst is not None and deficit_fn is not None:
        deficit=deficit_fn(worst)
    return {"met":met,"value":worst,"threshold":threshold,
            "deficit":deficit,"deficit_norm":_norm(deficit,scale_key),
            "met_count":ok,"total":len(vals),
            "unknown_count":unknown,"insufficient_data":not enough_data}

def _bool_cond(met, deficit=None, value=None, threshold=None, scale_key=None, extra=None):
    """[FIX] Раньше для булевых условий deficit жёстко ставился в 0 независимо
    от факта (conditions.py@v1:52,54,56). Из-за этого near-miss статистика
    показывала «медианный дефицит 0.000» у price/oi/cvd_not_falling, и это
    читалось как «провал из-за шума округления». В действительности дефицит
    для них просто не вычислялся. Теперь: None, если величину нарушения
    измерить нельзя, и реальная величина, если можно (см. step_violation)."""
    d={"met":bool(met),"value":value,"threshold":threshold,"deficit":deficit,
       "deficit_norm":_norm(deficit,scale_key),
       "met_count":None,"total":None,"unknown_count":0,"insufficient_data":False}
    if extra: d.update(extra)
    return d

def _worst_step_violation(vals, tol):
    """Максимальная величина нарушения шага: насколько сильнее чем на tol
    значение упало относительно предыдущего. 0.0 — нарушений нет."""
    v=[x for x in vals if x is not None]
    if len(v)<2: return 0.0
    return round(max(0.0,max((v[i-1]-tol)-v[i] for i in range(1,len(v)))),4)

def _net_violation(vals, tol):
    """Насколько итог окна ниже начала сверх допуска. 0.0 — нарушения нет."""
    v=[x for x in vals if x is not None]
    if len(v)<2: return 0.0
    return round(max(0.0,(v[0]-tol)-v[-1]),4)

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

# ─── И2: структура не сломана ───────────────────────────────────────────────
def _structure_conds(recent):
    """price/oi/cvd 'не падают'.

    USE_NET_CHANGE=False (по умолчанию): шаговая монотонность — как было.
    USE_NET_CHANGE=True: итоговое изменение за окно. Замер показал, что
    шаговый вариант проваливается с дефицитом ровно 0.000, то есть решается
    шумом округления, а не структурой рынка.
    """
    conds={}
    def series(key):
        return [s.get(key) for s in recent]
    def net_ok(key,tol):
        v=[x for x in series(key) if x is not None]
        if len(v)<2: return True
        return v[-1]>=v[0]-tol
    def step_ok(key,tol):
        v=[x for x in series(key) if x is not None]
        if len(v)<2: return True
        return all(v[i]>=v[i-1]-tol for i in range(1,len(v)))

    SPEC=(("price_not_falling","price_chg24",CONFIRMED_A_PC_TOLERANCE),
          ("oi_not_falling","oi_chg24_pct",CONFIRMED_A_OI_TOLERANCE),
          ("cvd_not_falling","cvd24",CONFIRMED_A_CVD_TOLERANCE))
    for name,key,tol in SPEC:
        sv=_worst_step_violation(series(key),tol)
        nv=_net_violation(series(key),tol)
        if USE_NET_CHANGE:
            met=net_ok(key,tol); deficit=nv if nv>0 else None
        else:
            # v1: для price требовались И шаговая монотонность, И итог по окну;
            # для oi/cvd — только шаговая.
            met=step_ok(key,tol) and (net_ok(key,tol) if name=="price_not_falling" else True)
            worst=max(sv,nv if name=="price_not_falling" else 0.0)
            deficit=worst if worst>0 else None
        conds[name]=_bool_cond(met,deficit=deficit,scale_key=name,
                               extra={"step_violation":sv,"net_violation":nv})
    return conds

def _shared_conds(recent, oi_min, cvd_min):
    """Условия, общие для path_a и path_b. Раньше были продублированы
    в двух функциях — расхождение между ними было вопросом времени."""
    n=len(recent); k=_k_for(n); conds={}
    conds["oi_chg24_gt%g"%oi_min]=_threshold_cond(
        [s.get("oi_chg24_pct") for s in recent], lambda v:v>oi_min, oi_min, k,
        deficit_fn=lambda w:round(oi_min-w,2), worst_fn=min, scale_key="oi_chg24")
    conds["cvd_gt%g"%cvd_min]=_threshold_cond(
        [s.get("cvd24") for s in recent], lambda v:v>cvd_min, cvd_min, k,
        deficit_fn=lambda w:round(cvd_min-w,2), worst_fn=min, scale_key="cvd")
    conds["oi4h_positive"]=_threshold_cond(
        [s.get("oi_chg4h_pct") for s in recent], lambda v:v>0, 0, k,
        deficit_fn=lambda w:round(-w,3), worst_fn=min, scale_key="oi4h_positive")
    conds["fr_lt005"]=_threshold_cond(
        [s.get("fr_oiw") for s in recent], lambda v:v<CONFIRMED_A_FR_MAX, CONFIRMED_A_FR_MAX, k,
        deficit_fn=lambda w:round(w-CONFIRMED_A_FR_MAX,5), worst_fn=max, scale_key="fr_lt005")
    conds["lls_lt40"]=_threshold_cond(
        [s.get("lls24") for s in recent], lambda v:v<CONFIRMED_A_LLS_MAX, CONFIRMED_A_LLS_MAX, k,
        deficit_fn=lambda w:round(w-CONFIRMED_A_LLS_MAX,2), worst_fn=max, scale_key="lls_lt40")
    conds.update(_structure_conds(recent))
    trends=check_trends_ok(recent)
    # [FIX] deficit тут был 0/1 — тоже фиктивное число. Направление тренда
    # информативнее, чем несуществующая величина.
    conds["trends_ok"]=_bool_cond(trends["met"],deficit=None,
        extra={"oi_trend":trends["oi_trend"],"cvd_trend":trends["cvd_trend"]})
    if USE_DENSITY_GATE:
        wq=window_quality(recent)
        over=round(max(0.0,wq["span_min"]-WINDOW_MAX_SPAN_MIN),1)
        conds["window_dense"]=_bool_cond(
            wq["dense"],deficit=over if over>0 else None,
            value=wq["span_min"],threshold=WINDOW_MAX_SPAN_MIN,scale_key="window_dense",
            extra={"max_gap_min":wq["max_gap_min"]})
    return conds

def _insufficient(snaps, have=None):
    return {"passed":False,"insufficient_data":True,
            "snaps_have":len(snaps) if have is None else have,
            "snaps_need":CONFIRMED_A_SNAPS,"conditions":{},
            "window":window_quality(snaps)}

def check_confirmed_path_a(snaps):
    if len(snaps)<CONFIRMED_A_SNAPS: return _insufficient(snaps)
    recent=snaps[-CONFIRMED_A_SNAPS:]
    conds=_shared_conds(recent,CONFIRMED_A_OI_MIN,CONFIRMED_A_CVD_MIN)
    insufficient=any(c.get("insufficient_data") for c in conds.values())
    passed=(not insufficient) and all(c["met"] for c in conds.values())
    return {"passed":passed,"insufficient_data":insufficient,"conditions":conds,
            "window":window_quality(recent)}

def check_confirmed_path_b(snaps,cvd_momentum):
    if len(snaps)<CONFIRMED_A_SNAPS: return _insufficient(snaps)
    recent=snaps[-CONFIRMED_A_SNAPS:]
    conds=_shared_conds(recent,CONFIRMED_B_OI_MIN,CONFIRMED_B_CVD_MIN)
    oi_growing_faster=False
    if len(recent)>=3:
        ov=[s.get("oi_chg24_pct") for s in recent[-3:]]
        if all(v is not None for v in ov):
            d1,d2=ov[1]-ov[0],ov[2]-ov[1]; oi_growing_faster=(d2>d1 and d1>0)
    conds["oi_growing_faster"]=_bool_cond(oi_growing_faster)
    met_mom=cvd_momentum>CONFIRMED_B_CVD_MOM_MIN
    conds["cvd_momentum_gt5"]=_bool_cond(
        met_mom,deficit=round(CONFIRMED_B_CVD_MOM_MIN-cvd_momentum,2) if not met_mom else 0,
        value=round(cvd_momentum,2),threshold=CONFIRMED_B_CVD_MOM_MIN)
    insufficient=any(c.get("insufficient_data") for c in conds.values())
    passed=(not insufficient) and all(c["met"] for c in conds.values())
    return {"passed":passed,"insufficient_data":insufficient,"conditions":conds,
            "window":window_quality(recent)}

EARLY_MOVE_SNAPS=3; EARLY_MOVE_CVD_TOLERANCE=3.0; EARLY_MOVE_VOL_FACTOR=0.95
def check_early_move(snaps):
    if len(snaps)<EARLY_MOVE_SNAPS: return _insufficient(snaps)
    last3=snaps[-EARLY_MOVE_SNAPS:]; conds={}
    def rising(key,tol=0.0,factor=1.0):
        v=[s.get(key) for s in last3]
        if any(x is None for x in v): return None
        return all(v[i]>v[i-1]*factor-tol for i in range(1,len(v)))
    for name,key,tol,factor in (("price_up","price_chg24",0.0,1.0),
                                ("oi_up","oi_chg24_pct",0.0,1.0),
                                ("cvd_up","cvd24",EARLY_MOVE_CVD_TOLERANCE,1.0),
                                ("vol_up","volume24",0.0,EARLY_MOVE_VOL_FACTOR)):
        r=rising(key,tol,factor)
        conds[name]=_bool_cond(bool(r))
        if r is None: conds[name]["insufficient_data"]=True
    if USE_DENSITY_GATE:
        wq=window_quality(last3)
        conds["window_dense"]=_bool_cond(wq["dense"],value=wq["span_min"],threshold=WINDOW_MAX_SPAN_MIN)
    insufficient=any(c.get("insufficient_data") for c in conds.values())
    passed=(not insufficient) and all(c["met"] for c in conds.values())
    return {"passed":passed,"insufficient_data":insufficient,"conditions":conds,
            "window":window_quality(last3)}

ACCUMULATION_SNAPS=3; ACCUMULATION_CVD_AVG_MIN=50.0; ACCUMULATION_PC_MAX=5.0; ACCUMULATION_FR_MAX=0.03
def check_accumulation(snaps):
    if len(snaps)<ACCUMULATION_SNAPS: return _insufficient(snaps)
    last3=snaps[-ACCUMULATION_SNAPS:]; conds={}
    k=_k_for(len(last3))
    conds["oi4h_positive"]=_threshold_cond(
        [s.get("oi_chg4h_pct") for s in last3], lambda v:v>0, 0, k,
        deficit_fn=lambda w:round(-w,3), worst_fn=min)
    cvd_vals=[s.get("cvd24") for s in last3 if s.get("cvd24") is not None]
    if cvd_vals:
        cvd_avg=sum(cvd_vals)/len(cvd_vals); met_cvd=cvd_avg>ACCUMULATION_CVD_AVG_MIN
        conds["cvd_avg_gt50"]=_bool_cond(met_cvd,
            deficit=round(ACCUMULATION_CVD_AVG_MIN-cvd_avg,2) if not met_cvd else 0,
            value=round(cvd_avg,2),threshold=ACCUMULATION_CVD_AVG_MIN)
    else:
        conds["cvd_avg_gt50"]=_bool_cond(False,threshold=ACCUMULATION_CVD_AVG_MIN)
        conds["cvd_avg_gt50"]["insufficient_data"]=True
    pc=last3[-1].get("price_chg24")
    if pc is None:
        conds["price_chg_lt5"]=_bool_cond(False,threshold=ACCUMULATION_PC_MAX)
        conds["price_chg_lt5"]["insufficient_data"]=True
    else:
        met_pc=pc<ACCUMULATION_PC_MAX
        conds["price_chg_lt5"]=_bool_cond(met_pc,
            deficit=round(pc-ACCUMULATION_PC_MAX,2) if not met_pc else 0,
            value=round(pc,2),threshold=ACCUMULATION_PC_MAX)
    conds["fr_lt003"]=_threshold_cond(
        [s.get("fr_oiw") for s in last3], lambda v:v<ACCUMULATION_FR_MAX, ACCUMULATION_FR_MAX, k,
        deficit_fn=lambda w:round(w-ACCUMULATION_FR_MAX,5), worst_fn=max)
    if USE_DENSITY_GATE:
        wq=window_quality(last3)
        conds["window_dense"]=_bool_cond(wq["dense"],value=wq["span_min"],threshold=WINDOW_MAX_SPAN_MIN)
    insufficient=any(c.get("insufficient_data") for c in conds.values())
    passed=(not insufficient) and all(c["met"] for c in conds.values())
    return {"passed":passed,"insufficient_data":insufficient,"conditions":conds,
            "window":window_quality(last3)}

# ─── И4: непрерывная сила сигнала (теневой режим) ───────────────────────────
STRENGTH_MAX = 10.0
def signal_strength(snaps, cvd_momentum=0.0):
    """Сила сигнала 0…10 для триггера Шмитта. Взвешенная доля выполненных
    условий вместо бинарного вердикта. На решения НЕ влияет, пока
    monitor.USE_SCHMITT=False — пишется в журнал для теневого сравнения.

    Замер: медианная длина эпизода растёт с 3 до 5 окон при паре порогов
    9.5/8.0 и до 8 окон при 8.5/7.0, что напрямую снижает дребезг.
    """
    if len(snaps)<CONFIRMED_A_SNAPS: return 0.0
    recent=snaps[-CONFIRMED_A_SNAPS:]; n=len(recent)
    def frac(key,pred):
        v=[s.get(key) for s in recent if s.get(key) is not None]
        return (sum(1 for x in v if pred(x))/len(v)) if v else 0.0
    sc=0.0
    sc+=2.0*frac("oi_chg24_pct",lambda v:v>CONFIRMED_A_OI_MIN)
    sc+=2.0*frac("cvd24",lambda v:v>CONFIRMED_A_CVD_MIN)
    sc+=1.0*frac("oi_chg4h_pct",lambda v:v>0)
    sc+=1.0*frac("fr_oiw",lambda v:v<CONFIRMED_A_FR_MAX)
    sc+=1.0*frac("lls24",lambda v:v<CONFIRMED_A_LLS_MAX)
    def net(key,tol):
        v=[s.get(key) for s in recent if s.get(key) is not None]
        return len(v)<2 or v[-1]>=v[0]-tol
    sc+=1.5 if net("price_chg24",CONFIRMED_A_PC_TOLERANCE) else 0.0
    sc+=1.0 if net("oi_chg24_pct",CONFIRMED_A_OI_TOLERANCE) else 0.0
    sc+=0.5 if net("cvd24",CONFIRMED_A_CVD_TOLERANCE) else 0.0
    wq=window_quality(recent)
    if not wq["dense"]: sc*=0.7          # разреженное окно — доверия меньше
    return round(min(sc,STRENGTH_MAX),3)

# ─── диагностика ближайшего промаха ─────────────────────────────────────────
def closest_miss_for_confirmed(snaps,cvd_momentum):
    result_a=check_confirmed_path_a(snaps); result_b=check_confirmed_path_b(snaps,cvd_momentum)
    best_a=_closest_fail(result_a); best_b=_closest_fail(result_b)
    if best_a is None and best_b is None: return {"path":None,"condition":None,"deficit":0,"note":"оба пути пройдены"}
    if best_a is None: return {"path":"b",**best_b}
    if best_b is None: return {"path":"a",**best_a}
    if best_a["deficit"] is None: return {"path":"b",**best_b}
    if best_b["deficit"] is None: return {"path":"a",**best_a}
    if best_a["deficit"]<=best_b["deficit"]: return {"path":"a",**best_a}
    return {"path":"b",**best_b}

def _closest_fail(result):
    """[FIX] Ранжирование по НОРМИРОВАННОМУ дефициту. Раньше сортировка шла по
    сырому deficit, у которого разные единицы (проценты OI, пункты CVD, ставка
    FR), а у булевых условий он был жёстко 0 — поэтому они всегда оказывались
    «самым близким промахом». Условия без измеримого дефицита теперь честно
    отдаются с deficit=None и не претендуют на первое место."""
    if result.get("passed"): return None
    if result.get("insufficient_data") and not result.get("conditions"):
        return {"condition":"insufficient_data","deficit":float("inf"),
                "deficit_norm":float("inf"),"value":None,"threshold":None}
    fails=[(name,c) for name,c in result["conditions"].items() if not c["met"]]
    if not fails: return None
    measurable=[(n,c) for n,c in fails
                if isinstance(c.get("deficit_norm"),(int,float)) and c["deficit_norm"]>0]
    if measurable:
        measurable.sort(key=lambda x:x[1]["deficit_norm"]); name,c=measurable[0]
        return {"condition":name,"deficit":c["deficit"],"deficit_norm":c["deficit_norm"],
                "value":c["value"],"threshold":c["threshold"]}
    name,c=fails[0]
    return {"condition":name,"deficit":None,"deficit_norm":None,
            "value":c.get("value"),"threshold":c.get("threshold"),
            "note":"дефицит не измерим для этого условия"}

# ═══════════════════════════════════════════════════════════════════════════
# ТЕНЕВОЕ СРАВНЕНИЕ ВАРИАНТОВ ПРЕДИКАТА
# Считается каждый прогон, на решения НЕ влияет. Пишется в shadow_signals.jsonl.
# ═══════════════════════════════════════════════════════════════════════════
SHADOW_VARIANTS=(
    ("live",      dict(kofn=True,  dense=True,  net=True)),   # текущий боевой = v3
    ("kofn_only", dict(kofn=True,  dense=False, net=False)),  # только И1
    ("dense_only",dict(kofn=False, dense=True,  net=False)),  # только И3
    ("kofn_dense",dict(kofn=True,  dense=True,  net=False)),  # И1+И3
)

def shadow_variants(snaps, cvd_momentum):
    """Возвращает вердикты предиката при разных конфигурациях + силу сигнала.
    Восстанавливает исходные флаги в любом случае."""
    global USE_KOFN,USE_DENSITY_GATE,USE_NET_CHANGE
    saved=(USE_KOFN,USE_DENSITY_GATE,USE_NET_CHANGE)
    out={}
    try:
        for name,cfg in SHADOW_VARIANTS:
            USE_KOFN,USE_DENSITY_GATE,USE_NET_CHANGE=cfg["kofn"],cfg["dense"],cfg["net"]
            ra=check_confirmed_path_a(snaps)
            rb=check_confirmed_path_b(snaps,cvd_momentum)
            out[name]={"pass":bool(ra.get("passed") or rb.get("passed")),
                       "path":"a" if ra.get("passed") else ("b" if rb.get("passed") else None),
                       "insufficient":bool(ra.get("insufficient_data") and rb.get("insufficient_data")),
                       "fails_a":sum(1 for c in ra.get("conditions",{}).values() if not c["met"])}
    finally:
        USE_KOFN,USE_DENSITY_GATE,USE_NET_CHANGE=saved
    out["strength"]=signal_strength(snaps,cvd_momentum)
    recent=snaps[-CONFIRMED_A_SNAPS:] if len(snaps)>=CONFIRMED_A_SNAPS else snaps
    out["window"]=window_quality(recent)
    out["disagrees"]=any(out[n]["pass"]!=out["live"]["pass"] for n,_ in SHADOW_VARIANTS if n!="live")
    return out
