#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# 0. HELP  &  ARGUMENT PARSING
###############################################################################
usage() { cat <<'EOF'
kube-images.sh — list container images/digests across contexts & namespaces

USAGE
  ./kube-images.sh [options]

OPTIONS
  -c, --context   <ctx|all|ask>   Context to inspect         (default ask)
  -n, --namespace <ns|all|ask>    Namespace filter           (default ask)
  -m, --mode      <image|id>      Show tag or digest         (default id)
  -f, --format    <table|json>    Output format              (default table)
  -s, --json-style <flat|pod>     JSON hierarchy (*only* when --format=json)
                                  flat = ctx → ns → [images]
                                  pod  = ctx → ns → pod → [images]   (default)
  -o, --output    <file>          JSON file path (when --format=json)
                                  (default images_by_ns.json)
  --kubeconfig    <path>          Kubeconfig file            (default \$KUBECONFIG or ~/.kube/config)
  -h, --help                      Show this help & exit
EOF
}

# ---------- defaults ----------
CONTEXT_CHOICE=""
NAMESPACE=""
OUTPUT_MODE="id"
OUTPUT_FORMAT="table"
JSON_STYLE="pod"              # flat | pod
OUTPUT_FILE="k8s_images.json"
KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/config}"

# ---------- parse args --------
while [[ $# -gt 0 ]]; do case "$1" in
  -c|--context)    CONTEXT_CHOICE="$2"; shift 2;;
  -n|--namespace)  NAMESPACE="$2";      shift 2;;
  -m|--mode)       OUTPUT_MODE="$2";    shift 2;;
  -f|--format)     OUTPUT_FORMAT="$2";  shift 2;;
  -s|--json-style) JSON_STYLE="$2";     shift 2;;
  -o|--output)     OUTPUT_FILE="$2";    shift 2;;
  --kubeconfig)    KUBECONFIG="$2";     shift 2;;
  -h|--help)       usage; exit 0;;
  *) echo "❌  Unknown option: $1"; usage; exit 1;;
esac; done

case "$OUTPUT_MODE"   in image|id)   ;; *) echo "❌  --mode image|id"; exit 1;; esac
case "$OUTPUT_FORMAT" in table|json) ;; *) echo "❌  --format table|json"; exit 1;; esac
case "$JSON_STYLE"    in flat|pod)   ;; *) echo "❌  --json-style flat|pod"; exit 1;; esac
command -v kubectl >/dev/null || { echo "❌  kubectl required but not installed"; exit 1; }
command -v jq >/dev/null || { echo "❌  jq required but not installed"; exit 1; }

###############################################################################
# 1. COLOUR HANDLING (declare first; keeps set -u happy)
###############################################################################
GREEN= CYAN= MAGENTA= YELLOW= BOLD= RESET=
if command -v tput &>/dev/null && [[ -t 1 && $OUTPUT_FORMAT == table ]]; then
  GREEN=$(tput setaf 2)  CYAN=$(tput setaf 6)  MAGENTA=$(tput setaf 5)
  YELLOW=$(tput setaf 3) BOLD=$(tput bold)     RESET=$(tput sgr0)
fi

###############################################################################
# 2. CONTEXT SELECTION
###############################################################################
mapfile -t ALL_CTX < <(kubectl config get-contexts --output=name --kubeconfig "$KUBECONFIG")

prompt_ctx() {
  echo "Available contexts:"; for i in "${!ALL_CTX[@]}"; do
    printf "  [%d] %s\n" "$((i+1))" "${ALL_CTX[$i]}"; done; echo "  [A] all"
  read -rp "Choose context (number or A): " ans
  if [[ $ans =~ ^[Aa]$ ]]; then CTXS=("${ALL_CTX[@]}")
  elif [[ $ans =~ ^[0-9]+$ ]]; then
    (( ans>=1 && ans<=${#ALL_CTX[@]} )) || { echo "❌  Bad index"; exit 1; }
    CTXS=("${ALL_CTX[$((ans-1))]}")
  else echo "❌  Invalid selection"; exit 1; fi
}

if [[ -z $CONTEXT_CHOICE || $CONTEXT_CHOICE == ask ]]; then
  prompt_ctx
elif [[ $CONTEXT_CHOICE == all ]]; then
  CTXS=("${ALL_CTX[@]}")
else
  CTXS=("$CONTEXT_CHOICE")
fi
(( ${#CTXS[@]} )) || { echo "❌  No contexts selected"; exit 1; }

###############################################################################
# 3. NAMESPACE SELECTION (per‑context)
###############################################################################
declare -A CTX2NS

prompt_ns() {  # $1 ctx
  local ctx="$1"
  mapfile -t NS_ARR < <(
    kubectl get ns --kubeconfig "$KUBECONFIG" --context "$ctx" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
  echo "Namespaces in '$ctx':"; for i in "${!NS_ARR[@]}"; do
    printf "  [%d] %s\n" "$((i+1))" "${NS_ARR[$i]}"; done; echo "  [A] all"
  read -rp "Choose namespace(s) (n[,..] or A): " sel
  if [[ $sel =~ ^[Aa]$ ]]; then
    CTX2NS[$ctx]=all
  elif [[ $sel =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    local chosen=""; IFS=',' read -ra idx <<<"$sel"
    for n in "${idx[@]}"; do
      (( n>=1 && n<=${#NS_ARR[@]} )) || { echo "❌  Bad idx $n"; exit 1; }
      chosen+=" ${NS_ARR[$((n-1))]}"
    done; CTX2NS[$ctx]="${chosen# }"
  else
    echo "❌  Invalid selection"; exit 1
  fi
}

if [[ -z $NAMESPACE || $NAMESPACE == ask ]]; then
  for c in "${CTXS[@]}"; do prompt_ns "$c"; done
else
  for c in "${CTXS[@]}"; do CTX2NS[$c]="$NAMESPACE"; done
fi

###############################################################################
# 4. HELPERS
###############################################################################
jq_filter() {
  if [[ $OUTPUT_MODE == image ]]; then
    echo '.items[] as $p
          | $p.spec.containers[].image
          | [$p.metadata.namespace,$p.metadata.name,.] | @tsv'
  else
    echo '.items[] as $p
          | ($p.status.containerStatuses[]? | .imageID) // empty
          | [$p.metadata.namespace,$p.metadata.name,.] | @tsv'
  fi
}
strip_pull() { printf '%s\n' "${1#docker-pullable://}"; }

###############################################################################
# 5. COLLECT DATA
###############################################################################
declare -A SEEN
declare -a TRIPLES
max_img=0 max_pod=0 max_ns=0 max_ctx=0

for ctx in "${CTXS[@]}"; do
  ns_spec="${CTX2NS[$ctx]:-all}"
  rows=()
  if [[ $ns_spec == all ]]; then
    pods_json=$(kubectl get pods -A --kubeconfig "$KUBECONFIG" --context "$ctx" -o json)
    mapfile -t rows < <(jq -r "$(jq_filter)" <<<"$pods_json")
  else
    for ns in $ns_spec; do
      pods_json=$(kubectl get pods -n "$ns" --kubeconfig "$KUBECONFIG" --context "$ctx" -o json)
      mapfile -t tmp < <(jq -r "$(jq_filter)" <<<"$pods_json"); rows+=("${tmp[@]}")
    done
  fi
  for r in "${rows[@]}"; do
    IFS=$'\t' read -r ns pod raw <<<"$r"
    img=$(strip_pull "$raw")
    key="$img|$pod|$ns|$ctx"; [[ ${SEEN[$key]+x} ]] && continue; SEEN[$key]=1
    TRIPLES+=("$key")
    (( ${#img}>max_img )) && max_img=${#img}
    (( ${#pod}>max_pod )) && max_pod=${#pod}
    (( ${#ns} >max_ns  )) && max_ns=${#ns}
    (( ${#ctx}>max_ctx )) && max_ctx=${#ctx}
  done
done

###############################################################################
# 6. OUTPUT
###############################################################################
if [[ $OUTPUT_FORMAT == json ]]; then
  if [[ $JSON_STYLE == flat ]]; then
    # context ➜ namespace ➜ [images]
    json=$(printf '%s\n' "${TRIPLES[@]}" |
      awk -F'|' '{printf "{\"ctx\":\"%s\",\"ns\":\"%s\",\"img\":\"%s\"}\n",$4,$3,$1}' |
      jq -s '
        group_by(.ctx) |
        map({ (.[0].ctx):
              ( group_by(.ns)
                | map({ (.[0].ns): (map(.img)|unique) }) | add )}) | add')
  else
    # context ➜ namespace ➜ pod ➜ [images]
    json=$(printf '%s\n' "${TRIPLES[@]}" |
      awk -F'|' '{printf "{\"ctx\":\"%s\",\"ns\":\"%s\",\"pod\":\"%s\",\"img\":\"%s\"}\n",$4,$3,$2,$1}' |
      jq -s '
        group_by(.ctx) |
        map({ (.[0].ctx):
              ( group_by(.ns)
                | map({ (.[0].ns):
                        ( group_by(.pod)
                          | map({ (.[0].pod): (map(.img)|unique) }) | add )}) | add )}) | add')
  fi
  printf '%s\n' "$json" > "$OUTPUT_FILE"
  echo "👉  JSON written to $OUTPUT_FILE"
  exit 0
fi

# ----- table (default) -----
header_img=$([[ $OUTPUT_MODE == id ]] && echo "IMAGE ID (digest)" || echo "IMAGE")
printf "%s%-*s  %-*s  %-*s  %-*s%s\n" \
       "$BOLD" "$max_img" "$header_img" \
       "$max_pod" "POD" \
       "$max_ns"  "NAMESPACE" \
       "$max_ctx" "CONTEXT" \
       "$RESET"
printf '%s\n' "$(printf '─%.0s' $(seq 1 $((max_img+max_pod+max_ns+max_ctx+6))))"
for t in "${TRIPLES[@]}"; do
  IFS='|' read -r img pod ns ctx <<<"$t"
  printf "%s%-*s%s  %s%-*s%s  %s%-*s%s  %s%-*s%s\n" \
         "$GREEN" "$max_img" "$img" "$RESET" \
         "$CYAN"  "$max_pod" "$pod" "$RESET" \
         "$MAGENTA" "$max_ns" "$ns" "$RESET" \
         "$YELLOW" "$max_ctx" "$ctx" "$RESET"
done


