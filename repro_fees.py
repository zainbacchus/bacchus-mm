from decimal import Decimal
from pathlib import Path
import tempfile

from bacchus_mm.fees import FeeSchedule, compute_fee
from bacchus_mm.risk import RiskManager, RiskParams
from bacchus_mm.exchange.kalshi import fill_signed_count

D = Decimal
sched = FeeSchedule()  # kalshi defaults 0.07 taker


def mk():
    return RiskManager(params=RiskParams(max_contracts_per_market=10,
                                         kill_switch_drawdown=D("10")),
                       state_dir=Path(tempfile.mkdtemp()))


print("=== (a) formula: rounding + minimum ===")
for c, p in [(1, D("0.5")), (1, D("0.01")), (1, D("0.99")), (10, D("0.5")),
             (2, D("0.5")), (1, D("0")), (1, D("1"))]:
    f = compute_fee(sched, c, p, True)
    raw = D("0.07") * c * p * (1 - p)
    print(f"  C={c} P={p}: raw={raw} -> fee={f}")

print("=== symmetry yes vs no price (P vs 1-P) ===")
for p in [D("0.5"), D("0.4"), D("0.13"), D("0.87")]:
    print(f"  P={p}: {compute_fee(sched,3,p,True)}  1-P={1-p}: {compute_fee(sched,3,1-p,True)}")

print("=== per-fill ceil vs per-order aggregate (computed-path overstatement) ===")
one_order = compute_fee(sched, 10, D("0.5"), True)
ten_fills = sum(compute_fee(sched, 1, D("0.5"), True) for _ in range(10))
print(f"  10@0.5 one fee={one_order}  as 10x1-fills sum={ten_fills}  diff={ten_fills-one_order}")

print("=== (d) round-trip buy then sell nets spread - 2*fee ===")
r = mk()
f_buy = compute_fee(sched, 2, D("0.50"), True)
f_sell = compute_fee(sched, 2, D("0.55"), True)
r.on_fill("MKT", +2, D("0.50"), f_buy)   # buy 2 yes @0.50
r.on_fill("MKT", -2, D("0.55"), f_sell)  # sell 2 yes @0.55
st = r.markets["MKT"]
print(f"  pos={st.position} cash={st.cash} fees={st.fees}")
expected = (D("0.55") - D("0.50")) * 2 - f_buy - f_sell
print(f"  pnl(pos flat)={st.cash}  expected spread-2fee={expected}  match={st.cash==expected}")

print("=== NO-side quadrants: buy NO / sell NO signs ===")
# buy NO at no=0.40 => yes_price 0.60, signed -count
sc = fill_signed_count("no", "buy", 3)
print(f"  fill_signed_count(no,buy,3)={sc} (expect -3)")
r2 = mk()
fee = compute_fee(sched, 3, D("0.60"), True)
r2.on_fill("N", sc, D("0.60"), fee)   # short 3 yes @0.60 (=bought 3 NO @0.40)
st2 = r2.markets["N"]
print(f"  pos={st2.position} cash={st2.cash} (cash = +3*0.60 - fee = {3*D('0.60')-fee})")

print("=== (c)+(settlement) fee reaches cumulative_pnl, settlement net-of-fee ===")
r3 = mk()
r3.on_mid("S", D("0.50"))
fee = compute_fee(sched, 5, D("0.50"), True)
r3.on_fill("S", +5, D("0.50"), fee)  # buy 5 yes @0.50, fee
print(f"  after fill: cash={r3.markets['S'].cash} pnl={r3.pnl()} cum={r3.cumulative_pnl}")
# settle YES=1.00
q, basis, realized = r3.on_settlement("S", D("1.00"))
print(f"  settle@1.00: q={q} basis={basis} realized={realized} cash={r3.markets['S'].cash}")
print(f"  post-settle pnl={r3.pnl()} cum={r3.cumulative_pnl}")
# realized should be 5*(1.00-0.50) - fee = 2.50 - fee
print(f"  expected realized = 2.50 - {fee} = {D('2.50')-fee}  match={realized==D('2.50')-fee}")

print("=== (c) fee-only can trip kill switch (dd>=10) ===")
r4 = mk()
r4.on_mid("K", D("0.50"))
# many taker fills paying fee, position round-trips to flat so only fees bleed
tot_fee = D("0")
for i in range(1000):
    fee = compute_fee(sched, 2, D("0.50"), True)
    tot_fee += 2*fee  # buy+sell both pay
    r4.on_fill("K", +2, D("0.50"), fee)
    r4.on_fill("K", -2, D("0.50"), fee)
print(f"  pos={r4.markets['K'].position} total fees paid={tot_fee} pnl={r4.pnl()}")
print(f"  drawdown={r4.drawdown()} should_halt={bool(r4.should_halt())}")
