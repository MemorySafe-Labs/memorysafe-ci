#!/bin/bash
# usage: in_quota.sh PERCENT CMD...  -- runs CMD (and its children) in a cgroup-v2 CPU quota (PERCENT of one core).
# Box only (needs sudo -n for the cgroup); never used on the user's Macs.
G=/sys/fs/cgroup/coldprobe
P=$1; shift
sudo -n mkdir -p $G && echo "$((P * 1000)) 100000" | sudo -n tee $G/cpu.max > /dev/null && sudo -n sh -c "echo $$ > $G/cgroup.procs" || { echo "cgroup quota unavailable" >&2; exit 3; }
exec "$@"
