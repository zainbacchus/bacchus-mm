# Calibration of the 15-minute markets at the maker's horizon (2026-08-06)

Question: does the favorite-longshot bias documented on Kalshi at large
(Buergi, Deng & Whelan 2026: makers on >=50c contracts earn +2.6% after fees)
exist at the 15-MINUTE horizon and at tail prices? The big calibration study
(arXiv 2602.19520) excluded prices beyond 5c/95c and never measured below the
1-hour horizon, so this exact regime was an open hole in the literature.

Method (research/calibration15.py, public API): 1,440 settled 15M windows
across all nine series, per-minute candlesticks. At each minute, tabulate the
two joinable trades (buy YES at the bid; buy NO at 1-ask) by the price paid,
then compare price to realized settle frequency. "edge" = realized minus
price, in cents per contract, for the BUYER at that price.

## EARLY window (minutes 1-7)

    p bucket       n  implied  realized  edge c/ct     se
 0.02-0.05     205    0.034     0.020      -1.49   0.97
 0.05-0.10     705    0.076     0.077      +0.03   1.00
 0.10-0.20    1446    0.153     0.149      -0.46   0.94
 0.20-0.35    3197    0.278     0.266      -1.21   0.78
 0.35-0.50    4597    0.423     0.438      +1.58   0.73
 0.50-0.65    4440    0.567     0.574      +0.71   0.74
 0.65-0.80    3170    0.714     0.745      +3.09   0.77
 0.80-0.90    1379    0.842     0.869      +2.66   0.91
 0.90-0.95     583    0.920     0.928      +0.76   1.07
 0.95-0.98     168    0.961     0.976      +1.51   1.18

## LATE window (minutes 8-14)

    p bucket       n  implied  realized  edge c/ct     se
 0.00-0.02    2377    0.008     0.006      -0.18   0.16
 0.02-0.05    1415    0.030     0.034      +0.37   0.48
 0.05-0.10    1386    0.072     0.080      +0.79   0.73
 0.10-0.20    1481    0.147     0.153      +0.66   0.94
 0.20-0.35    1436    0.268     0.293      +2.57   1.20
 0.35-0.50    1408    0.420     0.428      +0.84   1.32
 0.50-0.65    1373    0.570     0.585      +1.52   1.33
 0.65-0.80    1564    0.726     0.730      +0.38   1.12
 0.80-0.90    1539    0.848     0.855      +0.73   0.90
 0.90-0.95    1398    0.924     0.936      +1.21   0.65
 0.95-0.98    1486    0.965     0.972      +0.70   0.42
 0.98-1.00    2005    0.990     0.996      +0.57   0.15

## Reading it honestly

1. Overall positivity is mostly the half-spread: a joiner buys at the bid,
   which sits below a roughly calibrated mid. The signal is in the ASYMMETRY
   across buckets, not the average level.
2. The FLB tilt confirms at this horizon. Late window: buying favorites at
   0.98-1.00 earns +0.57c/ct (n=2005, se 0.15, ~4 sigma); 0.90-0.95 earns
   +1.21c (se 0.65). The longshot mirror (0.00-0.02) is negative. Early
   window: the 0.65-0.90 favorite bands earn +2.7 to +3.1c at ~4 sigma while
   0.10-0.35 longshot bands are negative.
3. Caveat: this tabulates PRICES BEING THERE, not fills. Real fills within a
   bucket are adversely selected, so live per-fill edge will be worse than
   these numbers. Direction and ordering are the evidence; live fills are
   the sizing.

## What it changed

fifteen.tilt_tail_threshold: 0.90 shipped 2026-08-06 (M1 in ROADMAP Phase D):
in the tails the bot no longer sells favorites or buys longshots. The early
window suggests the tilt could reach down toward 0.65; that is a candidate
for the first data review, not a day-one setting.
