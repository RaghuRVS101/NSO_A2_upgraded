#!/usr/bin/env bash
# ab_bench.sh <proxy_public_ip>
#
# Runs Apache Benchmark for node counts 1..5, pausing between each count
# so you can change servers.conf and let operate converge.
#
# Results are saved to results/ab-N<n>-run<r>.txt
# A summary table (mean / sd of Total column) is printed at the end.
#
# Prerequisites:
#   - ab installed  (macOS: brew install httpd)
#   - operate is running in another terminal window
#   - servers.conf is writable in this directory

set -euo pipefail

PROXY_IP="${1:-}"
if [[ -z "$PROXY_IP" ]]; then
    echo "usage: ./ab_bench.sh <proxy_public_ip>"
    exit 1
fi

URL="http://${PROXY_IP}:5000/"
REQUESTS=2000
CONCURRENCY=20
WARMUP_REQUESTS=200
RESULTS_DIR="results"
mkdir -p "$RESULTS_DIR"

python3_bin="$(cd "$(dirname "$0")" && pwd)/venv/bin/python3"
[[ -x "$python3_bin" ]] || python3_bin=python3

parse_ms() {
    # Extract mean and sd from the Total row of ab's "Connection Times (ms)" table.
    # Typical ab output:
    # Connection Times (ms)
    #               min  mean[+/-sd] median   max
    # Connect:        0    1   0.5      1      10
    # Processing:    10   15   2.1     14      30
    # Waiting:       10   14   2.0     13      29
    # Total:         10   16   2.3     15      35
    local file="$1"
    awk '/^Total:/ {mean=$2; sd=$3; sub("\\[\\+\\/-","",sd); sub("\\]","",sd); print mean"/"sd}' "$file"
}

declare -a SUMMARY_N SUMMARY_TOTAL

for N in 1 2 3 4 5; do
    echo ""
    echo "=============================================="
    echo " Setting servers.conf = $N"
    echo "=============================================="
    echo "$N" > servers.conf

    echo "Waiting 35s for operate to converge to $N nodes ..."
    sleep 35

    echo "Warm-up run ($WARMUP_REQUESTS requests) ..."
    ab -n "$WARMUP_REQUESTS" -c "$CONCURRENCY" "$URL" > /dev/null 2>&1 || true

    TOTAL_MEANS=()
    for RUN in 1 2; do
        OUTFILE="${RESULTS_DIR}/ab-N${N}-run${RUN}.txt"
        echo "Run $RUN / 2  (${REQUESTS} requests, concurrency ${CONCURRENCY}) ..."
        ab -n "$REQUESTS" -c "$CONCURRENCY" "$URL" > "$OUTFILE" 2>&1 || true
        TOTAL=$(parse_ms "$OUTFILE")
        echo "  Total mean/sd: $TOTAL ms"
        TOTAL_MEANS+=("$TOTAL")
    done

    SUMMARY_N+=("$N")
    # join the two run values with a space for later display
    SUMMARY_TOTAL+=("${TOTAL_MEANS[*]}")
done

echo ""
echo "=============================================="
echo " SUMMARY"
echo "=============================================="
printf "%-4s  %-12s  %-12s\n" "N" "Run1 tot(m/s)" "Run2 tot(m/s)"
printf "%-4s  %-12s  %-12s\n" "---" "------------" "------------"
for i in "${!SUMMARY_N[@]}"; do
    read -r r1 r2 <<< "${SUMMARY_TOTAL[$i]}"
    printf "%-4s  %-12s  %-12s\n" "${SUMMARY_N[$i]}" "${r1}" "${r2}"
done

echo ""
echo "Raw ab output files are in: ${RESULTS_DIR}/"
echo "Paste the Total mean/sd values into Table I of report/report.tex."
echo ""
echo "Restoring servers.conf = 3"
echo "3" > servers.conf
