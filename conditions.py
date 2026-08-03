"""conditions.py — проверочные функции условий lifecycle.
Общий модуль для unentered_tracker.py (и в будущем для monitor.py).

ВНИМАНИЕ: пороги должны совпадать с monitor.py. При изменении monitor.py
синхронизируй этот файл. Проверка консистентности — в test_conditions.py.

Эти функции проверяют условия на снимках, но НЕ воспроизводят полную машину
состояний (prev_state, порядок проверок, гистерезис). Для fail_point в
суженном scope v1 этого достаточно.
"""
from typing import Optional

MIN_SNAPS_LIFECYCLE = 5


def check_confirmed_path_a(recent):
    """Path A: все снимки OI>5 и CVD>55."""
    oi_vals = [s.get("oi_chg24_pct") or 0 for s in recent]
    cvd_vals = [s.get("cvd24") or 0 for s in recent]
    passed = all(v > 5 for v in oi_vals) and all(v > 55 for v in cvd_vals)
    return passed, oi_vals, cvd_vals


def check_confirmed_path_b(recent, cvd_momentum):
    """Path B: все снимки OI>2 и CVD>50, ускорение OI, cvd_momentum>5."""
    oi_vals = [s.get("oi_chg24_pct") or 0 for s in recent]
    cvd_vals = [s.get("cvd24") or 0 for s in recent]
    base = all(v > 2 for v in oi_vals) and all(v > 50 for v in cvd_vals)
    oi_growing_faster = False
    if len(oi_vals) >= 3:
        ov = oi_vals[-3:]
        d1 = ov[1] - ov[0]
        d2 = ov[2] - ov[1]
        oi_growing_faster = d2 > d1 and d1 > 0
    passed = base and oi_growing_faster and cvd_momentum > 5
    return passed, oi_vals, cvd_vals


def check_confirmed_common(recent):
    """Общие условия CONFIRMED: OI4h>0, FR<0.05, LLS<40 на всех снимках."""
    reasons = []
    all_oi4 = all((s.get("oi_chg4h_pct") or 0) > 0 for s in recent)
    all_fr = all(s.get("fr_oiw") is not None and s["fr_oiw"] < 0.05 for s in recent)
    all_lls = all(s.get("lls24") is not None and s["lls24"] < 40 for s in recent)
    if not all_oi4: reasons.append("OI4h ≤0")
    if not all_fr: reasons.append("FR ≥0.05")
    if not all_lls: reasons.append("LLS ≥40")
    return all_oi4 and all_fr and all_lls, reasons


def check_confirmed_price(recent):
    """Price не падает (all_pc + pc_net_up)."""
    all_pc = all(
        (recent[i].get("price_chg24") or 0) >= (recent[i-1].get("price_chg24") or 0) - 0.5
        for i in range(1, len(recent))
    )
    pc_net_up = (recent[-1].get("price_chg24") or 0) >= (recent[0].get("price_chg24") or 0) - 0.5
    passed = all_pc and pc_net_up
    return passed, (None if passed else "Price падает")


def check_confirmed_not_falling(recent):
    """OI и CVD не снижаются пошагово."""
    oi_nf = all(
        (recent[i].get("oi_chg24_pct") or 0) >= (recent[i-1].get("oi_chg24_pct") or 0) - 1
        for i in range(1, len(recent))
    )
    cvd_nf = all(
        (recent[i].get("cvd24") or 0) >= (recent[i-1].get("cvd24") or 0) - 5
        for i in range(1, len(recent))
    )
    return oi_nf, cvd_nf


def check_confirmed(recent, derived):
    """Полная проверка CONFIRMED на снимках.
    Возвращает (passed, path, failing_conditions, closest_miss).
    path: 'a'/'b'/None. closest_miss: близость к path_a и path_b отдельно."""
    if len(recent) < MIN_SNAPS_LIFECYCLE:
        return False, None, [f"мало снимков ({len(recent)}/{MIN_SNAPS_LIFECYCLE})"], None

    cvd_momentum = derived.get("cvd_momentum", 0)
    pa, oi_a, cvd_a = check_confirmed_path_a(recent)
    pb, oi_b, cvd_b = check_confirmed_path_b(recent, cvd_momentum)
    common_ok, common_reasons = check_confirmed_common(recent)
    price_ok, price_reason = check_confirmed_price(recent)
    oi_nf, cvd_nf = check_confirmed_not_falling(recent)
    trends_ok = derived.get("cvd_trend") != "down" and derived.get("oi_trend") != "down"

    failing = list(common_reasons)
    if price_reason: failing.append(price_reason)
    if not oi_nf: failing.append("OI снижается")
    if not cvd_nf: failing.append("CVD снижается")
    if not trends_ok: failing.append("тренд OI/CVD вниз")
    if not (pa or pb): failing.append("OI/CVD пороги (path_a/path_b)")

    passed = (pa or pb) and common_ok and price_ok and oi_nf and cvd_nf and trends_ok
    path = 'a' if pa else ('b' if pb else None)

    closest_miss = {}
    if not pa:
        closest_miss['path_a'] = {
            'oi_miss': round(max(0, 5 - min(oi_a)), 2) if oi_a else None,
            'cvd_miss': round(max(0, 55 - min(cvd_a)), 2) if cvd_a else None,
        }
    if not pb:
        closest_miss['path_b'] = {
            'oi_miss': round(max(0, 2 - min(oi_b)), 2) if oi_b else None,
            'cvd_miss': round(max(0, 50 - min(cvd_b)), 2) if cvd_b else None,
        }
    return passed, path, failing, closest_miss


def check_early_move(last3):
    """EARLY_MOVE: 3 снимка Price↑ OI↑ CVD↑ Vol↑."""
    if len(last3) < 3:
        return False, ["мало снимков"]
    reasons = []
    price_up = all((last3[i].get("price_chg24") or 0) > (last3[i-1].get("price_chg24") or 0) for i in range(1, 3))
    oi_up = all((last3[i].get("oi_chg24_pct") or 0) > (last3[i-1].get("oi_chg24_pct") or 0) for i in range(1, 3))
    cvd_up = all((last3[i].get("cvd24") or 0) > (last3[i-1].get("cvd24") or 0) - 3 for i in range(1, 3))
    vol_up = all((last3[i].get("volume24") or 0) > (last3[i-1].get("volume24") or 0) * 0.95 for i in range(1, 3))
    if not price_up: reasons.append("Price не растёт")
    if not oi_up: reasons.append("OI не растёт")
    if not cvd_up: reasons.append("CVD не растёт")
    if not vol_up: reasons.append("Volume не растёт")
    return price_up and oi_up and cvd_up and vol_up, reasons


def check_accumulation(last3):
    """ACCUMULATION: OI4h>0, CVD avg>50, Price<5, FR<0.03."""
    if len(last3) < 3:
        return False, ["мало снимков"]
    reasons = []
    oi4_pos = all((s.get("oi_chg4h_pct") or 0) > 0 for s in last3)
    cvd_avg = sum(s.get("cvd24") or 0 for s in last3) / 3
    pc = last3[-1].get("price_chg24") or 0
    fr_ok = all(s.get("fr_oiw") is not None and s["fr_oiw"] < 0.03 for s in last3)
    if not oi4_pos: reasons.append("OI4h ≤0")
    if cvd_avg <= 50: reasons.append(f"CVD avg {cvd_avg:.0f} ≤50")
    if pc >= 5: reasons.append(f"Price {pc:.1f}% ≥5")
    if not fr_ok: reasons.append("FR ≥0.03")
    return oi4_pos and cvd_avg > 50 and pc < 5 and fr_ok, reasons
