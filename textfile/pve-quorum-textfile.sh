#!/bin/sh
# pve-quorum-textfile: corosync votes as metrics, the Day 15 check.sh
# exit code turned into a time series. pve-exporter reports quorate yes or
# no; it does not report that one vote is already missing, and that is the
# state in which the next failure ends everything.
#
# Runs on the collector host from cron, like nas-textfile.sh. Arguments are
# LABEL=root@node1[,root@node2...], one per cluster. Any one node is enough
# (the output is cluster-wide); the others are tried in order when the
# first does not answer, because the moment a node is down is the moment
# the vote count matters. pvecm needs root, so the key logs in as root and
# is restricted on the node with
#   from="COLLECTOR_IP",command="/usr/bin/pvecm status",restrict
# so it can run exactly one command and nothing else (no pty, no
# forwarding). On PVE /root/.ssh/authorized_keys is a symlink into
# /etc/pve/priv, shared by every node, so one line covers the cluster.
# With no arguments, reads a `pvecm status` transcript on stdin (tests).
#
#   pve_quorum_expected_votes{cluster}  Expected votes
#   pve_quorum_total_votes{cluster}     Total votes
#   pve_quorum_quorum{cluster}          votes needed
#   pve_quorum_quorate{cluster}         1 when Flags contain Quorate
#   pve_quorum_qdevice_votes{cluster}   votes from the Qdevice line (0 when absent)
#   pve_quorum_node_votes{cluster,node} votes per member
#   pve_quorum_up{cluster}              1 when ssh and pvecm succeeded
#   pve_quorum_textfile_last_run_timestamp
set -u

SSH=${SSH:-ssh}
SSH_OPTS=${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=5}

parse() { # stdin: pvecm status  $1: label
  awk -v L="$1" '
    /^Expected votes:/ { exp_v = $3 }
    /^Total votes:/    { tot = $3 }
    /^Quorum:/         { q = $2 }
    /^Flags:/          { quorate = ($0 ~ /Quorate/) ? 1 : 0 }
    /^Name:/           { if (name == "") name = $2 }
    # membership rows: 0x00000001 1 A,V,NMW 192.168.2.9 (local)  /  0x00000000 1 Qdevice
    /^0x[0-9a-f]+[ \t]+[0-9]+/ {
      if ($3 == "Qdevice") qd = $2
      else { node = $4; votes[node] = $2 }
    }
    END {
      if (L == "") L = (name == "") ? "pve" : name
      if (exp_v == "") { printf "pve_quorum_up{cluster=\"%s\"} 0\n", L; exit }
      printf "pve_quorum_up{cluster=\"%s\"} 1\n", L
      printf "pve_quorum_expected_votes{cluster=\"%s\"} %d\n", L, exp_v
      printf "pve_quorum_total_votes{cluster=\"%s\"} %d\n", L, tot
      printf "pve_quorum_quorum{cluster=\"%s\"} %d\n", L, q
      printf "pve_quorum_quorate{cluster=\"%s\"} %d\n", L, quorate
      printf "pve_quorum_qdevice_votes{cluster=\"%s\"} %d\n", L, (qd == "") ? 0 : qd
      for (n in votes) printf "pve_quorum_node_votes{cluster=\"%s\",node=\"%s\"} %d\n", L, n, votes[n]
    }'
}

header() {
  cat <<'EOF'
# HELP pve_quorum_up 1 when pvecm status was read
# TYPE pve_quorum_up gauge
# HELP pve_quorum_expected_votes Expected votes from pvecm status
# TYPE pve_quorum_expected_votes gauge
# HELP pve_quorum_total_votes Total votes currently present
# TYPE pve_quorum_total_votes gauge
# HELP pve_quorum_quorum Votes needed for quorum
# TYPE pve_quorum_quorum gauge
# HELP pve_quorum_quorate 1 when the Quorate flag is set
# TYPE pve_quorum_quorate gauge
# HELP pve_quorum_qdevice_votes Votes contributed by the QDevice, 0 when absent
# TYPE pve_quorum_qdevice_votes gauge
# HELP pve_quorum_node_votes Votes per member node
# TYPE pve_quorum_node_votes gauge
EOF
}

header
if [ $# -eq 0 ]; then
  parse ""
else
  for arg in "$@"; do
    label=${arg%%=*}; targets=${arg#*=}
    out=""
    for target in $(printf '%s' "$targets" | tr ',' ' '); do
      # shellcheck disable=SC2086
      out=$($SSH $SSH_OPTS "$target" pvecm status 2>/dev/null) || out=""
      case $out in *"Expected votes:"*) break ;; esac
    done
    printf '%s\n' "$out" | parse "$label"
  done
fi
printf '# HELP pve_quorum_textfile_last_run_timestamp Unix time of the last run\n# TYPE pve_quorum_textfile_last_run_timestamp gauge\npve_quorum_textfile_last_run_timestamp %s\n' "$(date -u +%s)"
