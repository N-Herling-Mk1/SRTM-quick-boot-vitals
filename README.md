# SRTM-quick-boot-vitals
Read-only liveness dashboard for the ATLAS SRTM board. Taps an existing Telegraf/InfluxDB/Grafana pipeline to answer one question: are the numbers in the WinCC panel real right now? Three staleness clocks, per-channel mean/sigma, and six monotonic canary counters.
