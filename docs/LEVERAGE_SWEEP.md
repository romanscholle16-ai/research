# Leverage sweep (conservative; dev-period trades only; $300 start; ruin = equity<=floor)

| candidate | lev | best risk | P($1k/mo) | ruin | lose50 | median wealth | liq-paths | verdict |
|---|---|---|---|---|---|---|---|---|
| gp_rule ETHUSDT 1h stop6.0 | 5x | 0.5 | 0.20% | 0.0% | 34.2% | $451 | 0% | viable |
| gp_rule ETHUSDT 1h stop6.0 | 10x | 0.5 | 7.20% | 6.6% | 53.0% | $447 | 60% | viable |
| gp_rule ETHUSDT 1h stop6.0 | 25x | 0.5 | 4.40% | 9.4% | 58.4% | $296 | 100% | ruin/negative |
| gp_rule ETHUSDT 1h stop6.0 | 50x | 0.5 | 3.80% | 1.0% | 49.0% | $323 | 100% | viable |
| gp_rule ETHUSDT 1h stop6.0 | 100x | 0.02 | 0.00% | 0.0% | 0.0% | $291 | 100% | ruin/negative |
| gp_rule ETHUSDT 1h stop8.0 | 5x | 0.5 | 0.00% | 0.0% | 24.0% | $459 | 9% | viable |
| gp_rule ETHUSDT 1h stop8.0 | 10x | 0.5 | 0.40% | 1.2% | 33.0% | $483 | 39% | viable |
| gp_rule ETHUSDT 1h stop8.0 | 25x | 0.5 | 0.20% | 0.2% | 36.4% | $373 | 99% | viable |
| gp_rule ETHUSDT 1h stop8.0 | 50x | 0.2 | 0.00% | 0.0% | 1.0% | $346 | 100% | viable |
| gp_rule ETHUSDT 1h stop8.0 | 100x | 0.02 | 0.00% | 0.0% | 0.0% | $300 | 100% | ruin/negative |
| gp_rule SOLUSDT 1h stop6.0 | 5x | 0.5 | 11.20% | 4.6% | 45.6% | $488 | 22% | viable |
| gp_rule SOLUSDT 1h stop6.0 | 10x | 0.5 | 16.40% | 12.2% | 54.6% | $487 | 90% | viable |
| gp_rule SOLUSDT 1h stop6.0 | 25x | 0.5 | 6.40% | 19.6% | 66.0% | $208 | 100% | ruin/negative |
| gp_rule SOLUSDT 1h stop6.0 | 50x | 0.5 | 1.20% | 8.2% | 74.6% | $124 | 100% | ruin/negative |
| gp_rule SOLUSDT 1h stop6.0 | 100x | 0.02 | 0.00% | 0.0% | 0.0% | $284 | 100% | ruin/negative |
| gp_rule SOLUSDT 1h stop8.0 | 5x | 0.5 | 24.40% | 9.8% | 47.6% | $728 | 36% | viable |
| gp_rule SOLUSDT 1h stop8.0 | 10x | 0.5 | 19.20% | 10.0% | 46.0% | $547 | 99% | viable |
| gp_rule SOLUSDT 1h stop8.0 | 25x | 0.5 | 2.40% | 1.0% | 48.2% | $311 | 100% | viable |
| gp_rule SOLUSDT 1h stop8.0 | 50x | 0.5 | 3.00% | 0.0% | 26.0% | $411 | 100% | viable |
| gp_rule SOLUSDT 1h stop8.0 | 100x | 0.2 | 0.00% | 0.0% | 0.0% | $319 | 100% | viable |
