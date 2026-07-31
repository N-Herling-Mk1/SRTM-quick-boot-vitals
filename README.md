# srtm-quickboot-vitals

Easy-access board-reading boot page for the SRTM. Answers one question:

> Are the numbers in the WinCC panel real, right now?

It reads the SRTM data already flowing into InfluxDB via Prof. Cheu's
TIG stack (Telegraf + InfluxDB + Grafana) and renders live values
alongside the metrics that say whether those values can be trusted.

## It does not touch the TIG stack

Every query is a read. Nothing is written, no container is restarted,
no config is edited, Grafana is not involved. The stack it reads from is
production monitoring owned by someone else, running since 2026-05-08.

## Topology

![Development topology](docs/topology.svg)

Everything runs on bigmem3. InfluxDB is bound there, the Flask process holds
the API token there, and the working copy lives there. The Windows machine
contributes a browser and nothing else -- no local clone, so no drift between
where the code is edited and where it runs.

VS Code Remote-SSH auto-forwards port 5055 once Flask is listening, so a
manual `ssh -L` is only needed if you want the page open without VS Code
running.

## Why there is a Flask layer

A browser cannot query InfluxDB directly -- that would put the API token
into client-side JavaScript and require a CORS exception on a container
we do not own. `app.py` holds the token and serves plain JSON.

## The three clocks

| clock | measures | catches | blind to |
|---|---|---|---|
| `t_poll` | since the collector last wrote | dead pipeline, VPN drop, stopped container | a frozen board |
| `t_change` | since a value last differed | frozen board, wedged OPC UA server | nothing relevant |
| `t_wincc` | since the WinCC DP last changed | a stale panel over live data | **mk_1, not yet wired** |

`t_poll` alone is not enough: Telegraf writes on a 5s schedule whether or
not the value moved, so a frozen board produces a healthy-looking timer
forever. `t_change` is the real instrument.

## Canaries

Of the 161 published nodes only 6 are unambiguous liveness sensors --
monotonic counters that cannot legitimately stop:

    FF11_uptime  FF12_uptime  FF13_uptime
    IPMC_seq     IPMC_rawtime IPMC_time

Everything else is classified in `data/node_classification.csv`:

| class | n | alarmable |
|---|---|---|
| ANALOG | 101 | via sigma, once baselines exist |
| DISCRETE | 35 | no -- flat is correct |
| STATIC | 19 | no -- never changes by design |
| COUNTER | 6 | yes -- the canaries |

## Sigma is displayed, not alarmed

A frozen ANALOG channel has sigma exactly 0, which would widen the oracle
from 6 channels to 107. But sigma 0 is also correct for a genuinely quiet
rail, or one whose ADC LSB is coarse relative to its real noise. Alarming
needs a per-channel baseline derived from the ~3 months of history already
banked. Until that exists, sigma is shown and not judged.

## Run

On bigmem3 -- InfluxDB is bound to that host.

    pip install -r requirements.txt
    cp .env.example .env      # then fill in INFLUXDB_TOKEN
    ./run_vitals.sh

Tunnel from Windows:

    ssh -L 5055:localhost:5055 naherlin@eepp-bigmem3.physics.arizona.edu

Then open http://localhost:5055

## Known upstream defects (echeu/srtm-TIG)

- `.env` is committed with a live admin-scoped InfluxDB token in a public
  repo. Generate your own read-scoped token; do not reuse that one.
- All three images are `:latest`.
- `SRTM.FF11_cdrlol` and `SRTM.FF12_cdrlol` publish as fields `F11_cdrlol`
  and `F12_cdrlol` -- missing an F. `FF13_cdrlol` is correct. Confirmed
  present in live InfluxDB data, not just the config.
- README says browse `localhost:8086`; the host mapping is `8096`.

## Open

- `FF11_cdrenable` and `F11_cdrlol` read 255 (0xFF), which is what an I2C
  read returns from an unpopulated slot. Check `FF1x_present` before
  sigma alarming goes live, or absent hardware will alarm forever.
- `t_wincc` needs a WinCC OA CTRL manager or an RDB query. That is the
  expensive half and the reason it is not in mk_0.
